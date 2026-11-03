package com.codexflow.configcenter.application;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.JiraRequestStore;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** 采集后派发；后台异常保留原请求编号，沿用运行提交对账。 */
@Service
public class JiraPoolService {
  private final JiraRequestStore requests;
  private final GatewayClient gateway;
  private final WorkflowRunService workflows;

  private final WorkflowRunStore runs;
  private final BoundedWork worker = new BoundedWork("jira-pool", 1, 1);
  private static final org.slf4j.Logger LOGGER =
      org.slf4j.LoggerFactory.getLogger(JiraPoolService.class);

  public JiraPoolService(
      JiraRequestStore requests,
      GatewayClient gateway,
      WorkflowRunService workflows,
      WorkflowRunStore runs) {
    this.requests = requests;
    this.gateway = gateway;
    this.workflows = workflows;
    this.runs = runs;
  }

  public ObjectNode enqueue(String taskId, String source, String token) {
    ObjectNode queued = requests.enqueue(taskId, source, token);
    queued.remove("spec");
    return queued;
  }

  public ObjectNode status(String id) {
    ObjectNode value = requests.status(id);
    value.remove("spec");
    return value;
  }

  public JsonNode board(String group, Long before, String project, String task, String state) {
    String filters =
        "&project=" + encode(project) + "&task=" + encode(task) + "&state=" + encode(state);
    ObjectNode result =
        (ObjectNode)
            gateway.get(
                "/jira-pool?groupId="
                    + group
                    + (before == null ? "" : "&before=" + before)
                    + filters);
    for (JsonNode item : result.path("items")) {
      if (item.hasNonNull("workflow_id"))
        ((ObjectNode) item).put("monitorUrl", runs.monitorUrl(item.path("workflow_id").asText()));
      for (JsonNode execution : item.path("executions"))
        ((ObjectNode) execution)
            .put("monitorUrl", runs.monitorUrl(execution.path("workflowId").asText()));
    }
    result.set("schedules", requests.scheduleOverview(group));
    return result;
  }

  private static String encode(String value) {
    return java.net.URLEncoder.encode(value, java.nio.charset.StandardCharsets.UTF_8);
  }

  @Scheduled(fixedDelay = 30_000)
  void tick() {
    worker.submit(
        "dispatch",
        () -> {
          for (String id : requests.pending()) advance(id);
        });
  }

  @jakarta.annotation.PreDestroy
  void close() {
    worker.close();
  }

  void advance(String id) {
    try {
      ObjectNode request = requests.status(id);
      if (!request.hasNonNull("workflowId")) requests.validate(id);
      ObjectNode spec = (ObjectNode) request.path("spec");
      ObjectNode collection =
          spec.deepCopy().put("requestId", id).put("source", request.path("source").asText());
      JsonNode result = gateway.post("/jira-collections", collection);
      String state = result.path("state").asText();
      if (state.equals("failed") || state.equals("cancelled")) {
        requests.state(id, state, result.path("reason").asText());
        return;
      }
      if (!state.equals("ready")) {
        requests.state(id, "waiting", "正在查找任务或等待采集机器。");
        return;
      }
      if (!request.hasNonNull("reservationId")) {
        if (result.path("pending").asInt() == 0) {
          requests.state(id, "empty", "没有待分配工单。");
          return;
        }
        if (gateway
                .post(
                    "/jira-capacity",
                    spec.deepCopy().put("workflowId", java.util.UUID.randomUUID().toString()))
                .path("capacity")
                .asInt()
            == 0) {
          requests.state(id, "waiting", "工单已采集，等待空闲机器。");
          return;
        }
      }
      ObjectNode check = requests.checkPayload(id);
      if (check != null) {
        JsonNode checked = gateway.checkSchedule(check);
        boolean passed = checked.path("status").asText().equals("passed");
        boolean retryable =
            checked.path("status").asText().equals("skipped")
                && checked.path("retryable").asBoolean(false);
        String reason = checked.path("reason").asText();
        if (reason.isBlank() || reason.length() > 512) reason = "执行前检查未通过，本批不派发。";
        requests.checkFinished(id, passed, retryable, reason);
        if (!passed) {
          if (!retryable) gateway.delete("/jira-collections/" + id);
          return;
        }
      }
      String reservation = requests.reservation(id);
      JsonNode reservationResult =
          gateway.post(
              "/jira-reservations",
              spec.objectNode().put("requestId", id).put("reservationId", reservation));
      JsonNode binding = reservationResult.path("reservation");
      if (!binding.isObject()) {
        JsonNode latest = gateway.get("/jira-collections/" + id);
        requests.state(
            id,
            !reservationResult.has("state") && latest.path("pending").asInt() == 0
                ? "empty"
                : "waiting",
            "等待核验、空闲监督机或可领取工单。");
        return;
      }
      var prepared = requests.prepare(id, (ObjectNode) binding);
      if (request.hasNonNull("workflowId")) workflows.submitPrepared(prepared.prepared());
      else
        workflows.submitReservation(prepared, request.path("source").asText(), "已领取 Jira 工单并启动任务。");
      boolean remaining =
          request.path("source").asText().equals("schedule")
              && gateway.get("/jira-collections/" + id).path("pending").asInt() > 0;
      requests.dispatched(id, remaining);
    } catch (ConflictFailure error) {
      ObjectNode current = requests.status(id);
      if (current.hasNonNull("workflowId")
          && !runs.runStatus(current.path("workflowId").asText()).equals("submit_failed")) {
        requests.state(id, "dispatching", "运行已创建，正在等待提交确认。");
      } else {
        gateway.delete("/jira-collections/" + id);
        requests.state(id, "cancelled", "配置已变化或提交失败，旧来源停止派发。");
      }
    } catch (RuntimeException error) {
      LOGGER.warn("Jira 派发尚未完成，requestId={}，异常类型={}", id, error.getClass().getSimpleName());
    }
  }
}
