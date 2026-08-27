package com.codexflow.configcenter.integration.dingtalk;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;

/** 将工作流聚合状态转换为钉钉互动卡片模板变量。 */
@Component
class DingTalkProgressCard {

  Map<String, Object> render(JsonNode snapshot, String notice) {
    JsonNode gate = snapshot.path("pendingAdvance");
    boolean waiting = gate.isObject() && !gate.path("gateId").asText().isBlank();
    boolean held = waiting && "held".equals(gate.path("state").asText());
    Map<String, Object> values = new LinkedHashMap<>();
    values.put("title", "任务进度");
    values.put("markdown", summary(snapshot, notice));
    values.put("status", statusText(snapshot.path("status").asText()));
    values.put("workflowId", snapshot.path("workflowId").asText());
    values.put("gateId", waiting ? gate.path("gateId").asText() : "");
    values.put("showConfirm", Boolean.toString(waiting));
    values.put("showHold", Boolean.toString(waiting && !held));
    values.put("confirmText", held ? "继续进入下一步" : "立即进入下一步");
    values.put("holdText", "暂停");
    values.put("confirmAction", "advance_confirm");
    values.put("holdAction", "advance_hold");
    return values;
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
      value.append(
          "held".equals(gate.path("state").asText())
              ? "\n已暂停自动流转，请手动继续。\n"
              : "\n将在 30 秒等待期结束后自动进入下一步。\n");
    }
    String result = snapshot.path("response").asText();
    if (!result.isBlank() && isTerminal(snapshot.path("status").asText())) {
      value.append("\n**最终结果：**\n").append(plain(abbreviate(result, 2000))).append('\n');
    }
    String error = snapshot.path("error").asText();
    if (!error.isBlank()) {
      value.append("\n**说明：** ").append(plain(abbreviate(error, 500))).append('\n');
    }
    if (notice != null && !notice.isBlank()) value.append("\n").append(plain(notice)).append('\n');
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
