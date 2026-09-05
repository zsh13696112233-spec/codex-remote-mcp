package com.codexflow.configcenter.application;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.PreparedRun;
import com.codexflow.configcenter.domain.TaskLaunchStore;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import com.codexflow.configcenter.integration.dingtalk.DingTalkProactiveNotificationService;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** 协调工作流网关调用与事务性运行状态持久化的应用服务。 */
@Service
public class WorkflowRunService {

  private static final Logger LOGGER = LoggerFactory.getLogger(WorkflowRunService.class);

  private static final Set<String> ACTIVE_STATUSES =
      Set.of("submitting", "queued", "running", "cancelling");

  private final WorkflowRunStore store;
  private final TaskLaunchStore launches;
  private final GatewayClient gateway;
  private final DingTalkProactiveNotificationService dingtalkNotifications;

  /** 注入领域运行存储服务和工作流网关客户端。 */
  public WorkflowRunService(
      WorkflowRunStore store,
      TaskLaunchStore launches,
      GatewayClient gateway,
      DingTalkProactiveNotificationService dingtalkNotifications) {
    this.store = store;
    this.launches = launches;
    this.gateway = gateway;
    this.dingtalkNotifications = dingtalkNotifications;
  }

  /** 使用任务定义的最新配置创建并提交一次运行。 */
  public ObjectNode runLatest(String taskId) {
    return launchLatest(taskId, "web", "任务已从网页启动。");
  }

  /** 使用任务定义的最新配置执行一次定时运行。 */
  public ObjectNode runScheduled(String taskId) {
    return launchLatest(taskId, "schedule", "定时任务已启动。");
  }

  /** 使用历史运行的冻结快照创建并提交一次重试。 */
  public ObjectNode retry(String workflowId) {
    String taskId = store.taskDefinitionId(workflowId);
    releaseTerminalOccupant(taskId);
    TaskLaunchStore.LaunchReservation reservation = launches.reserveRetry(workflowId);
    return submitPrepared(reservation.prepared());
  }

  /** 请求网关取消运行，并持久化网关返回的最新状态。 */
  public ObjectNode cancel(String workflowId) {
    JsonNode response = gateway.post("/workflows/" + workflowId + "/cancel", null);
    ObjectNode result = store.recordGatewayStatus(workflowId, response, "cancelled");
    if (isTerminal(result.path("status").asText())) launches.release(workflowId);
    result.set("gatewayResponse", response);
    return result;
  }

  /** 查询任务运行历史，并尽力从网关刷新仍处于活动状态的记录。 */
  public List<ObjectNode> listRuns(String taskId) {
    List<ObjectNode> values = new ArrayList<>(store.listRuns(taskId));
    for (ObjectNode value : values) {
      refreshActiveRun(value);
    }
    return values;
  }

  public List<ObjectNode> listRunSummaries(String taskId, int page, int size) {
    List<ObjectNode> values = store.listRunSummaries(taskId, page, size);
    if (values.isEmpty()) return values;
    try {
      ObjectNode request = values.get(0).objectNode();
      var ids = request.putArray("workflowIds");
      values.forEach(value -> ids.add(value.path("workflowId").asText()));
      JsonNode live = gateway.post("/workflow-statuses", request).path("statuses");
      store.recordSummaryStatuses(live);
      values.forEach(
          value -> {
            JsonNode status = live.get(value.path("workflowId").asText());
            if (status != null && status.isTextual()) value.put("status", status.asText());
          });
    } catch (RuntimeException ignored) {
      // 列表只执行一次批量查询；离线时继续展示持久化状态。
    }
    return values;
  }

  public ObjectNode runDetail(String workflowId) {
    return store.runDetail(workflowId);
  }

  /** 分批补齐未登记的历史运行，避免等待用户再次运行才完成升级。 */
  public void synchronizeRuntimeScopes() {
    for (String taskId : store.pendingRuntimeScopeTasks()) {
      try {
        List<String> ids = store.pendingRuntimeScopes(taskId);
        if (ids.isEmpty()) continue;
        ObjectNode request =
            tools.jackson.databind.node.JsonNodeFactory.instance
                .objectNode()
                .put("taskDefinitionId", taskId);
        var array = request.putArray("workflowIds");
        ids.forEach(array::add);
        gateway.post("/workflow-task-bindings", request);
        store.markRuntimeScopes(ids);
      } catch (RuntimeException error) {
        LOGGER.warn("历史运行归属登记失败，下轮重试，taskDefinitionId={}。", taskId, error);
      }
    }
  }

  /** 获取网关当前公开的执行机列表。 */
  public JsonNode agents() {
    return gateway.get("/agents");
  }

  /** 获取工作流网关的就绪状态。 */
  public JsonNode ready() {
    return gateway.get("/readyz");
  }

  /** 尽力确认所有任务占用的实时状态，并释放已经进入终态的运行。 */
  public void reconcileActiveRuns() {
    for (TaskLaunchStore.ActiveLaunch active : launches.activeLaunches()) {
      try {
        JsonNode live = gateway.get("/workflows/" + active.workflowId());
        String liveStatus = live.path("status").asText();
        if (!liveStatus.isBlank()) {
          store.recordLiveStatus(active.workflowId(), liveStatus, live);
        }
        if (isTerminal(liveStatus)) launches.release(active.workflowId());
      } catch (GatewayFailure error) {
        if (error.getStatusCode() == 404) {
          try {
            if ("submitting".equals(store.runStatus(active.workflowId()))) {
              submitPrepared(store.getPrepared(active.workflowId()));
            }
          } catch (RuntimeException ignored) {
            // 提交结果仍不明确时保留占用，下一轮继续恢复。
          }
        }
      } catch (RuntimeException ignored) {
        // 数据库或网关不可用时保守保留占用，避免重复运行。
      }
    }
  }

  /** 向网关提交已持久化的运行，并记录成功响应或失败原因。 */
  public ObjectNode submitPrepared(PreparedRun prepared) {
    try {
      String taskId = store.taskDefinitionId(prepared.workflowId());
      ObjectNode payload = prepared.payload();
      if (taskId != null) {
        // 历史迁移只由后台推进；暂时保留原编号和运行槽，由对账继续补交。
        if (store.hasPendingRuntimeHistory(taskId, prepared.workflowId())) {
          throw new ConflictFailure("历史运行正在完成升级登记，本次运行已保留，登记完成后会自动继续提交。");
        }
        payload = payload.deepCopy();
        payload.put("taskDefinitionId", taskId);
      }
      JsonNode response = gateway.post("/workflows", payload);
      if (taskId != null) store.markRuntimeScopes(List.of(prepared.workflowId()));
      return recordAccepted(prepared.workflowId(), response);
    } catch (RuntimeException error) {
      try {
        JsonNode existing = gateway.get("/workflows/" + prepared.workflowId());
        return recordAccepted(prepared.workflowId(), existing);
      } catch (GatewayFailure lookupError) {
        if (lookupError.getStatusCode() == 404 && isDefinitiveRejection(error)) {
          store.recordSubmissionFailure(prepared.workflowId(), error);
          launches.release(prepared.workflowId());
        }
      } catch (RuntimeException ignored) {
        // 无法确认网关是否已经受理时保留 submitting 和运行槽，由后台对账恢复。
      }
      throw error;
    }
  }

  private ObjectNode launchLatest(String taskId, String source, String notice) {
    boolean notifyDingTalk = store.notifyDingTalk(taskId);
    if (notifyDingTalk) dingtalkNotifications.validate(taskId);
    releaseTerminalOccupant(taskId);
    TaskLaunchStore.LaunchReservation reservation = launches.reserveLatest(taskId);
    PreparedRun prepared = reservation.prepared();
    boolean notificationReserved = false;
    boolean submissionStarted = false;
    try {
      if (reservation.notifyDingTalk()) {
        dingtalkNotifications.reserve(taskId, prepared.workflowId(), source);
        notificationReserved = true;
      }
      submissionStarted = true;
      ObjectNode result = submitPrepared(prepared);
      if (notificationReserved) dingtalkNotifications.submitted(prepared.workflowId(), notice);
      return result;
    } catch (RuntimeException error) {
      String status = store.runStatus(prepared.workflowId());
      if (!submissionStarted) {
        store.recordSubmissionFailure(prepared.workflowId(), error);
        launches.release(prepared.workflowId());
      }
      if (notificationReserved && "submit_failed".equals(status)) {
        dingtalkNotifications.submissionFailed(prepared.workflowId());
      }
      throw error;
    }
  }

  private ObjectNode recordAccepted(String workflowId, JsonNode response) {
    ObjectNode result = store.recordGatewayStatus(workflowId, response, "queued");
    result.set("gatewayResponse", response);
    result.put("monitorUrl", store.monitorUrl(workflowId));
    return result;
  }

  private static boolean isDefinitiveRejection(RuntimeException error) {
    if (!(error instanceof GatewayFailure failure)) return false;
    int status = failure.getStatusCode();
    return status >= 400 && status < 500 && status != 408;
  }

  /** 若当前占用工作流已经终止则释放；查询失败或仍活动时保持占用。 */
  private void releaseTerminalOccupant(String taskId) {
    String active = launches.activeWorkflowId(taskId).orElse(null);
    if (active == null) return;
    try {
      String status = gateway.get("/workflows/" + active).path("status").asText();
      if (isTerminal(status)) launches.release(active);
      else throw activeConflict();
    } catch (ConflictFailure error) {
      throw error;
    } catch (RuntimeException error) {
      throw activeConflict();
    }
  }

  private static ConflictFailure activeConflict() {
    return new ConflictFailure("当前任务仍在运行，请等待完成后再次运行。");
  }

  private static boolean isTerminal(String status) {
    return Set.of("completed", "failed", "cancelled", "submit_failed").contains(status);
  }

  /** 对活动运行执行一次尽力而为的状态刷新，网关不可用时保留数据库状态。 */
  private void refreshActiveRun(ObjectNode value) {
    String status = value.path("status").asText();
    if (!ACTIVE_STATUSES.contains(status)) return;
    try {
      String workflowId = value.path("workflowId").asText();
      JsonNode live = gateway.get("/workflows/" + workflowId);
      String liveStatus = live.path("status").asText(status);
      value.put("status", liveStatus);
      value.set("live", live);
      store.recordLiveStatus(workflowId, liveStatus, live);
      if (isTerminal(liveStatus)) launches.release(workflowId);
    } catch (RuntimeException ignored) {
      // 网关状态刷新是尽力而为操作；失败时返回数据库中最近一次已知状态。
    }
  }
}
