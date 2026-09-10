package com.codexflow.configcenter.integration.dingtalk;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import tools.jackson.databind.JsonNode;

/** 单轮等待卡片：正文快照独立保存，状态与按钮读取中央当前等待。 */
final class DingTalkWaitingCard {
  static final String TEMPLATE_ID = "330ed542-3c7b-4d3b-b7bd-9ee608602e1b.schema";

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
    String state = state(snapshot, gateId);
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
    return values;
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
