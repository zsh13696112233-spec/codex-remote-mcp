package com.codexflow.configcenter.integration.feishu;

import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 将工作流聚合状态转换为一张可更新的飞书进度卡。 */
@Component
class FeishuProgressCard {

  private final ObjectMapper objectMapper;

  FeishuProgressCard(ObjectMapper objectMapper) {
    this.objectMapper = objectMapper;
  }

  @SuppressWarnings("unchecked")
  Map<String, Object> render(JsonNode snapshot, String notice) {
    ObjectNode card = objectMapper.createObjectNode();
    card.put("schema", "2.0");
    card.putObject("config").put("update_multi", true).put("wide_screen_mode", true);
    ObjectNode header = card.putObject("header");
    header.put("template", headerTemplate(snapshot.path("status").asText()));
    header.putObject("title").put("tag", "plain_text").put("content", "任务进度");

    ArrayNode elements = card.putObject("body").putArray("elements");
    elements.addObject().put("tag", "markdown").put("content", summary(snapshot, notice));
    JsonNode gate = snapshot.path("pendingAdvance");
    if (gate.isObject() && !gate.path("gateId").asText().isBlank()) {
      ArrayNode actions = elements.addObject().put("tag", "action").putArray("actions");
      if ("held".equals(gate.path("state").asText())) {
        actions.add(button("继续进入下一步", "primary", "advance_confirm", snapshot, gate));
      } else {
        actions.add(button("立即进入下一步", "primary", "advance_confirm", snapshot, gate));
        actions.add(button("暂停", "default", "advance_hold", snapshot, gate));
      }
    }
    return objectMapper.convertValue(card, Map.class);
  }

  private ObjectNode button(
      String label, String type, String action, JsonNode snapshot, JsonNode gate) {
    ObjectNode button = objectMapper.createObjectNode();
    button.put("tag", "button");
    button.put("type", type);
    button.put("name", action);
    button.putObject("text").put("tag", "plain_text").put("content", label);
    button
        .putObject("value")
        .put("action", action)
        .put("workflowId", snapshot.path("workflowId").asText())
        .put("gateId", gate.path("gateId").asText());
    return button;
  }

  private static String summary(JsonNode snapshot, String notice) {
    StringBuilder value = new StringBuilder();
    value
        .append("**任务：** ")
        .append(plain(snapshot.path("name").asText("未命名任务")))
        .append("\n\n**状态：** ")
        .append(statusText(snapshot.path("status").asText()));
    JsonNode progress = snapshot.path("progress");
    value
        .append("\n\n**进度：** ")
        .append(progress.path("completed").asInt())
        .append(" / ")
        .append(progress.path("total").asInt());
    JsonNode nodes = snapshot.path("nodes");
    if (nodes.isArray()) {
      value.append("\n\n");
      for (JsonNode node : nodes) {
        value
            .append(statusIcon(node.path("status").asText()))
            .append(' ')
            .append(plain(node.path("displayName").asText("步骤")))
            .append(" · ")
            .append(statusText(node.path("status").asText()))
            .append('\n');
      }
    }
    JsonNode gate = snapshot.path("pendingAdvance");
    if (gate.isObject()) {
      if ("held".equals(gate.path("state").asText())) {
        value.append("\n已暂停自动流转，请手动继续。\n");
      } else {
        value.append("\n将在 30 秒等待期结束后自动进入下一步。\n");
      }
    }
    String result = snapshot.path("response").asText();
    if (!result.isBlank() && isTerminal(snapshot.path("status").asText())) {
      value.append("\n**最终结果：**\n").append(plain(abbreviate(result, 2000))).append('\n');
    }
    String error = snapshot.path("error").asText();
    if (!error.isBlank()) {
      value.append("\n**说明：** ").append(plain(abbreviate(error, 500))).append('\n');
    }
    if (notice != null && !notice.isBlank()) {
      value.append("\n").append(plain(notice)).append('\n');
    }
    return value.toString().trim();
  }

  private static String statusText(String status) {
    return switch (status) {
      case "queued" -> "排队中";
      case "running" -> "运行中";
      case "completed" -> "已完成";
      case "failed" -> "失败";
      case "cancelled" -> "已停止";
      case "cancelling" -> "停止中";
      default -> "准备中";
    };
  }

  private static String statusIcon(String status) {
    return switch (status) {
      case "completed" -> "✅";
      case "running", "starting" -> "🔄";
      case "failed", "cancelled", "timed_out" -> "❌";
      default -> "▫️";
    };
  }

  private static String headerTemplate(String status) {
    return switch (status) {
      case "completed" -> "green";
      case "failed", "cancelled" -> "red";
      case "running" -> "blue";
      default -> "grey";
    };
  }

  private static boolean isTerminal(String status) {
    return List.of("completed", "failed", "cancelled").contains(status);
  }

  private static String plain(String value) {
    return value.replace("<", "＜").replace(">", "＞");
  }

  private static String abbreviate(String value, int length) {
    return value.length() <= length ? value : value.substring(0, length) + "\n…（内容已截断）";
  }
}
