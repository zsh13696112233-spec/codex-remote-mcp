package com.codexflow.configcenter.integration.dingtalk;

import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;

/** 将工作流聚合状态转换为钉钉互动卡片模板变量。 */
@Component
class DingTalkProgressCard {

  private static final int MAX_VISIBLE_NODES = 12;

  Map<String, Object> render(JsonNode snapshot, String notice) {
    return render(snapshot, notice, null);
  }

  Map<String, Object> render(JsonNode snapshot, String notice, String latestAssistantReply) {
    JsonNode gate = snapshot.path("pendingAdvance");
    boolean waiting = hasGate(gate);
    boolean held = waiting && "held".equals(gate.path("state").asText());
    Map<String, Object> values = new LinkedHashMap<>();
    values.put("title", abbreviate(plain(snapshot.path("name").asText("未命名任务")), 40));
    values.put("markdown", summary(snapshot, notice, true, latestAssistantReply));
    values.put("status", cardStatus(snapshot));
    values.put("flowStatus", flowStatus(snapshot));
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

  String renderText(JsonNode snapshot, String notice) {
    return summary(snapshot, notice, false, null);
  }

  String renderMarkdown(JsonNode snapshot, String notice) {
    return summary(snapshot, notice, true, null);
  }

  private static String summary(
      JsonNode snapshot, String notice, boolean markdown, String latestAssistantReply) {
    StringBuilder value = new StringBuilder();
    String name = plain(snapshot.path("name").asText("未命名任务"));
    JsonNode progress = snapshot.path("progress");
    int completed = progress.path("completed").asInt();
    int total = progress.path("total").asInt();
    int percent = total <= 0 ? 0 : Math.min(100, completed * 100 / total);

    value
        .append(markdown ? "**任务：** " : "任务：")
        .append(name)
        .append(markdown ? "\n\n**状态：** " : "\n状态：")
        .append(cardStatus(snapshot))
        .append(markdown ? "\n\n**执行进度：** " : "\n执行进度：")
        .append(progressBar(percent))
        .append(' ')
        .append(percent)
        .append("% · ")
        .append(completed)
        .append(" / ")
        .append(total);

    JsonNode current = currentNode(snapshot);
    if (current != null) {
      value.append(markdown ? "\n\n**当前步骤：** " : "\n当前步骤：").append(nodeLabel(current));
      String detail = nodeDetail(current);
      if (!detail.isBlank()) value.append("\n").append(detail);
    } else if ("queued".equals(snapshot.path("status").asText())) {
      value.append(markdown ? "\n\n**当前步骤：** 等待调度" : "\n当前步骤：等待调度");
    }

    JsonNode retryPolicy = snapshot.path("retryPolicy");
    if (retryPolicy.isObject() && retryPolicy.path("maxRetries").asInt() > 0) {
      value
          .append(markdown ? "\n\n**剩余返工：** " : "\n剩余返工：")
          .append(retryPolicy.path("remainingRetries").asInt())
          .append(" 次");
    }

    appendNodes(value, snapshot.path("nodes"), markdown);
    appendLatestOutput(value, snapshot.path("nodes"), markdown);

    JsonNode gate = snapshot.path("pendingAdvance");
    if (hasGate(gate)) {
      value.append(markdown ? "\n\n**等待确认**\n" : "\n等待确认\n");
      JsonNode next = findNode(snapshot.path("nodes"), gate.path("nextNodeId").asText());
      if (next != null) value.append("下一步：").append(nodeLabel(next)).append("\n");
      value.append(
          "held".equals(gate.path("state").asText())
              ? "已暂停自动流转。请点击“继续进入下一步”。"
              : "请点击“暂停”或“立即进入下一步”；否则将在 30 秒后自动继续。");
    }

    if (latestAssistantReply != null && !latestAssistantReply.isBlank()) {
      value
          .append(markdown ? "\n\n**最新助手回复**\n" : "\n最新助手回复\n")
          .append(plain(abbreviate(latestAssistantReply.trim(), 1200)));
    }

    String result = snapshot.path("response").asText();
    if (!result.isBlank() && isTerminal(snapshot.path("status").asText())) {
      value
          .append(markdown ? "\n\n**最终结果**\n" : "\n最终结果\n")
          .append(plain(abbreviate(result, 1600)));
    }
    if (!snapshot.path("error").asText().isBlank()) {
      value.append(markdown ? "\n\n**说明：** 任务执行未完成，请在监控中心查看详细信息。" : "\n说明：任务执行未完成，请在监控中心查看详细信息。");
    }
    if (notice != null && !notice.isBlank()) {
      value.append(markdown ? "\n\n---\n" : "\n").append(plain(notice));
    }
    return value.toString().trim();
  }

  private static void appendNodes(StringBuilder value, JsonNode nodes, boolean markdown) {
    if (!nodes.isArray() || nodes.isEmpty()) return;
    value.append(markdown ? "\n\n**步骤状态**\n" : "\n步骤状态\n");
    int visible = Math.min(nodes.size(), MAX_VISIBLE_NODES);
    for (int index = 0; index < visible; index++) {
      JsonNode node = nodes.get(index);
      value
          .append(statusIcon(node.path("status").asText()))
          .append(' ')
          .append(index + 1)
          .append(". ")
          .append(nodeLabel(node))
          .append(" · ")
          .append(statusText(node.path("status").asText()))
          .append('\n');
    }
    if (nodes.size() > visible)
      value.append("… 还有 ").append(nodes.size() - visible).append(" 个步骤\n");
  }

  private static void appendLatestOutput(StringBuilder value, JsonNode nodes, boolean markdown) {
    if (!nodes.isArray()) return;
    JsonNode latest = null;
    for (JsonNode node : nodes) {
      if ("completed".equals(node.path("status").asText())
          && !node.path("response").asText().isBlank()) latest = node;
    }
    if (latest == null) return;
    value
        .append(markdown ? "\n**最近产出 · " : "\n最近产出 · ")
        .append(nodeLabel(latest))
        .append(markdown ? "**\n" : "\n")
        .append(plain(abbreviate(latest.path("response").asText().trim(), 360)));
  }

  private static JsonNode currentNode(JsonNode snapshot) {
    JsonNode nodes = snapshot.path("nodes");
    JsonNode gate = snapshot.path("pendingAdvance");
    if (hasGate(gate)) {
      JsonNode next = findNode(nodes, gate.path("nextNodeId").asText());
      if (next != null) return next;
    }
    JsonNode currentIds = snapshot.path("currentNodes");
    if (currentIds.isArray()) {
      for (JsonNode id : currentIds) {
        JsonNode node = findNode(nodes, id.asText());
        if (node != null) return node;
      }
    }
    if (nodes.isArray()) {
      for (JsonNode node : nodes) {
        if (List.of("starting", "running").contains(node.path("status").asText())) return node;
      }
    }
    return null;
  }

  private static JsonNode findNode(JsonNode nodes, String id) {
    if (id.isBlank() || !nodes.isArray()) return null;
    for (JsonNode node : nodes) {
      if (id.equals(node.path("id").asText())) return node;
    }
    return null;
  }

  private static String nodeLabel(JsonNode node) {
    String displayName = plain(node.path("displayName").asText("步骤"));
    String roleName = plain(node.path("roleName").asText());
    return roleName.isBlank() || roleName.equals(displayName)
        ? displayName
        : displayName + " · " + roleName;
  }

  private static String nodeDetail(JsonNode node) {
    StringBuilder detail = new StringBuilder();
    String elapsed = elapsed(node.path("startedAt").asText(), node.path("finishedAt").asText());
    if (!elapsed.isBlank()) detail.append("已运行 ").append(elapsed);
    int attempts = node.path("attemptCount").asInt();
    if (attempts > 1) {
      if (!detail.isEmpty()) detail.append(" · ");
      detail.append("第 ").append(attempts).append(" 次执行");
    }
    return detail.toString();
  }

  private static String elapsed(String startedAt, String finishedAt) {
    Instant start = parseInstant(startedAt);
    if (start == null) return "";
    Instant end = parseInstant(finishedAt);
    if (end == null) end = Instant.now();
    long seconds = Math.max(0, Duration.between(start, end).getSeconds());
    if (seconds < 60) return seconds + " 秒";
    if (seconds < 3600) return seconds / 60 + " 分 " + seconds % 60 + " 秒";
    return seconds / 3600 + " 小时 " + seconds % 3600 / 60 + " 分";
  }

  private static Instant parseInstant(String value) {
    if (value == null || value.isBlank()) return null;
    try {
      return OffsetDateTime.parse(value).toInstant();
    } catch (DateTimeParseException ignored) {
      try {
        return Instant.parse(value);
      } catch (DateTimeParseException invalid) {
        return null;
      }
    }
  }

  private static String progressBar(int percent) {
    int filled = Math.min(10, Math.max(0, percent / 10));
    return "█".repeat(filled) + "░".repeat(10 - filled);
  }

  private static String cardStatus(JsonNode snapshot) {
    if (hasGate(snapshot.path("pendingAdvance"))) {
      return "held".equals(snapshot.path("pendingAdvance").path("state").asText())
          ? "⏸️ 已暂停"
          : "⏳ 等待确认";
    }
    return switch (snapshot.path("status").asText()) {
      case "queued" -> "⏳ 排队中";
      case "running" -> "🔵 执行中";
      case "completed" -> "✅ 已完成";
      case "failed" -> "❌ 执行失败";
      case "cancelled" -> "⏹️ 已停止";
      case "cancelling" -> "⏹️ 停止中";
      default -> "▫️ 准备中";
    };
  }

  private static String statusText(String status) {
    return switch (status) {
      case "queued" -> "排队中";
      case "starting" -> "启动中";
      case "running" -> "执行中";
      case "completed" -> "已完成";
      case "failed" -> "失败";
      case "cancelled" -> "已停止";
      case "timed_out" -> "超时";
      case "skipped" -> "已跳过";
      case "cancelling" -> "停止中";
      default -> "准备中";
    };
  }

  private static String flowStatus(JsonNode snapshot) {
    if (hasGate(snapshot.path("pendingAdvance"))) return "1";
    return switch (snapshot.path("status").asText()) {
      case "running", "cancelling" -> "4";
      case "completed" -> "3";
      case "failed", "cancelled" -> "5";
      default -> "1";
    };
  }

  private static String statusIcon(String status) {
    return switch (status) {
      case "completed" -> "✅";
      case "running", "starting" -> "🔄";
      case "failed", "cancelled", "timed_out" -> "❌";
      case "skipped" -> "⏭️";
      default -> "▫️";
    };
  }

  private static boolean hasGate(JsonNode gate) {
    return gate.isObject() && !gate.path("gateId").asText().isBlank();
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
