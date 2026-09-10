package com.codexflow.configcenter.integration.dingtalk;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import tools.jackson.databind.JsonNode;

/** 单轮等待卡片：正文快照独立保存，状态与按钮读取中央当前等待。 */
final class DingTalkWaitingCard {
  static final String TEMPLATE_ID = "59418790-6cad-43f9-b1d8-55cf9f18ed1a.schema";

  private DingTalkWaitingCard() {}

  static String state(JsonNode snapshot, String gateId) {
    JsonNode gate = snapshot.path("pendingAdvance");
    if (!gateId.equals(gate.path("gateId").asText())) return "closed";
    if ("held".equals(gate.path("state").asText())) return "held";
    if ("countdown".equals(gate.path("state").asText())
        && Instant.parse(gate.path("expiresAt").asText()).isAfter(Instant.now()))
      return "countdown";
    return "closed";
  }

  static Map<String, Object> render(String workflowId, JsonNode payload, JsonNode snapshot) {
    String gateId = payload.path("gateId").asText();
    String state = cardState(snapshot, payload);
    boolean restart = payload.path("restartControl").asBoolean();
    boolean stop = restart && "stop".equals(payload.path("controlType").asText());
    String status =
        switch (state) {
          case "held" -> "已保持等待";
          case "countdown" -> "等待确认";
          default -> "本轮等待已结束";
        };
    String rule =
        switch (state) {
          case "held" -> "不会自动继续；确认后请点击“继续执行”。";
          case "countdown" -> "默认两分钟无人操作自动继续。可右键引用此卡片提问；仅打开输入框不会暂停倒计时，消息送达并成功保持后才取消自动继续。";
          default -> "此卡片已不能继续任务，请查看最新状态。";
        };
    Map<String, Object> values = new LinkedHashMap<>();
    values.put("restartMode", Boolean.toString(restart && !stop));
    values.put("stopMode", Boolean.toString(stop));
    values.put("waitingMode", Boolean.toString(!restart));
    values.put("controlId", payload.path("actionId").asText(""));
    values.put("title", payload.path("title").asText("任务等待确认"));
    values.put(
        "markdown",
        "**"
            + status
            + "**\n\n"
            + payload.path("steps").asText()
            + "\n\n"
            + payload.path("text").asText()
            + "\n\n"
            + rule);
    values.put("workflowId", workflowId);
    values.put("gateId", gateId);
    values.put("confirmStatus", "closed".equals(state) ? "disabled" : "normal");
    // 初次展示及刷新不代表某次按钮成功；回调按本次网关结果单独返回成功判定。
    values.put("confirmRequestSucceeded", "false");
    if (restart) {
      String body = payload.path("text").asText().replace("如要继续，请另发一条仅包含“确认执行”的消息；10分钟内有效。", "");
      values.put("title", "返工确认");
      values.put(
          "markdown",
          "**"
              + ("closed".equals(state) ? "本次返工提议已失效或已处理" : "等待确认返工")
              + "**\n\n"
              + body.strip()
              + "\n\n确认有效期至："
              + payload.path("controlExpiresAt").asText()
              + "\n\n请由提议人点击“确认返工”或“取消返工”。取消或过期只撤销返工提议，不自动继续原流程。");
    }
    if (stop) {
      values.put("title", "停止确认");
      values.put("markdown", values.get("markdown").toString().replace("返工", "停止"));
    }
    return values;
  }

  static String cardState(JsonNode snapshot, JsonNode payload) {
    if (!payload.path("restartControl").asBoolean())
      return state(snapshot, payload.path("gateId").asText());
    JsonNode control = snapshot.path("pendingControl");
    if (!payload.path("actionId").asText().isBlank()
        && payload.path("actionId").asText().equals(control.path("actionId").asText())
        && payload.path("controlType").asText("restart_from").equals(control.path("type").asText())
        && "pending".equals(control.path("status").asText())
        && Instant.parse(control.path("expiresAt").asText()).isAfter(Instant.now())) return "held";
    return "closed";
  }

  static String steps(JsonNode snapshot) {
    JsonNode gate = snapshot.path("pendingAdvance");
    StringBuilder result = new StringBuilder();
    int index = 0;
    for (JsonNode node : snapshot.path("nodes")) {
      index++;
      String label = "第" + index + "步「" + node.path("displayName").asText("未命名") + "」";
      String id = node.path("id").asText();
      if (id.equals(gate.path("completedNodeId").asText()))
        result.append("已完成：").append(label).append("\n\n");
      if (id.equals(gate.path("nextNodeId").asText())) result.append("下一步：").append(label);
    }
    return DingTalkExecutionNotice.safe(result.toString());
  }

  static String completion(JsonNode snapshot) {
    String completed = snapshot.path("pendingAdvance").path("completedNodeId").asText();
    for (JsonNode node : snapshot.path("nodes"))
      if (!completed.isBlank() && completed.equals(node.path("id").asText())) {
        String response = node.path("response").asText();
        if (!response.isBlank()) return response + "\n\n请检查本步骤产出，确认后继续。";
      }
    return "步骤已完成，请检查产出后继续。";
  }
}
