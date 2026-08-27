package com.codexflow.configcenter.application;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.PreparedRun;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** 协调工作流网关调用与事务性运行状态持久化的应用服务。 */
@Service
public class WorkflowRunService {

  private static final Set<String> ACTIVE_STATUSES =
      Set.of("submitting", "queued", "running", "cancelling");

  private final WorkflowRunStore store;
  private final GatewayClient gateway;

  /** 注入领域运行存储服务和工作流网关客户端。 */
  public WorkflowRunService(WorkflowRunStore store, GatewayClient gateway) {
    this.store = store;
    this.gateway = gateway;
  }

  /** 使用任务定义的最新配置创建并提交一次运行。 */
  public ObjectNode runLatest(String taskId) {
    return submitPrepared(store.prepareLatest(taskId));
  }

  /** 使用历史运行的冻结快照创建并提交一次重试。 */
  public ObjectNode retry(String workflowId) {
    return submitPrepared(store.prepareRetry(workflowId));
  }

  /** 请求网关取消运行，并持久化网关返回的最新状态。 */
  public ObjectNode cancel(String workflowId) {
    JsonNode response = gateway.post("/workflows/" + workflowId + "/cancel", null);
    ObjectNode result = store.recordGatewayStatus(workflowId, response, "cancelled");
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

  /** 获取网关当前公开的执行机列表。 */
  public JsonNode agents() {
    return gateway.get("/agents");
  }

  /** 获取工作流网关的就绪状态。 */
  public JsonNode ready() {
    return gateway.get("/readyz");
  }

  /** 向网关提交已持久化的运行，并记录成功响应或失败原因。 */
  public ObjectNode submitPrepared(PreparedRun prepared) {
    try {
      JsonNode response = gateway.post("/workflows", prepared.payload());
      ObjectNode result = store.recordGatewayStatus(prepared.workflowId(), response, "queued");
      result.set("gatewayResponse", response);
      result.put("monitorUrl", store.monitorUrl(prepared.workflowId()));
      return result;
    } catch (RuntimeException error) {
      store.recordSubmissionFailure(prepared.workflowId(), error);
      throw error;
    }
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
    } catch (RuntimeException ignored) {
      // 网关状态刷新是尽力而为操作；失败时返回数据库中最近一次已知状态。
    }
  }
}
