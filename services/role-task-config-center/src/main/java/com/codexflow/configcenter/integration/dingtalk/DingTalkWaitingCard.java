package com.codexflow.configcenter.integration.dingtalk;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import tools.jackson.databind.JsonNode;

/** 单轮等待卡片：正文快照独立保存，状态与按钮读取中央当前等待。 */
final class DingTalkWaitingCard {
  static final String TEMPLATE_ID = "82792b55-07fb-4c4e-a579-60b16f3da560.schema";

  private DingTalkWaitingCard() {}

  static String state(JsonNode snapshot, String gateId) {
    JsonNode gate = snapshot.path("pendingAdvance");
    if (!gateId.equals(gate.path("gateId").asText())) return "closed";
    if (snapshot.path("discussionBusy").asBoolean() || "busy".equals(gate.path("state").asText()))
      return "busy";
    if ("held".equals(gate.path("state").asText())) return "held";
    if ("countdown".equals(gate.path("state").asText())
        && Instant.parse(gate.path("expiresAt").asText()).isAfter(Instant.now()))
      return "countdown";
    return "closed";
  }

  static Map<String, Object> render(String workflowId, JsonNode payload, JsonNode snapshot) {
    return render(workflowId, payload, snapshot, "http://127.0.0.1:8090/");
  }

  static long documentRevision(JsonNode snapshot, JsonNode payload) {
    for (JsonNode node : snapshot.path("nodes")) {
      if (node.path("id").asText().equals(payload.path("documentNodeId").asText()))
        return node.path("documents").isEmpty() ? 0 : node.path("resultRevision").asLong();
    }
    return 0;
  }

  static Map<String, Object> render(
      String workflowId, JsonNode payload, JsonNode snapshot, String monitorUrl) {
    String gateId = payload.path("gateId").asText();
    String state = cardState(snapshot, payload);
    String status =
        switch (state) {
          case "busy" -> "正在处理";
          case "held" -> "等待人工处理";
          case "countdown" -> "等待确认";
          default -> "本轮等待已结束";
        };
    String rule =
        switch (state) {
          case "busy" -> "回复和修改完成后才能继续。";
          case "held" -> "不会自动继续；确认后请点击“继续执行”。";
          case "countdown" -> "默认两分钟无人操作自动继续。可右键引用此卡片提问；仅打开输入框不会暂停倒计时，消息送达并成功保持后才取消自动继续。";
          default -> "此卡片已不能继续任务，请查看最新状态。";
        };
    JsonNode gate = snapshot.path("pendingAdvance");
    String kind = payload.path("waitKind").asText("legacy");
    boolean policy = !"legacy".equals(kind);
    String label =
        "blocked".equals(kind)
            ? "重新执行"
            : "finish".equals(kind) ? "确认结束" : policy ? "进入下一步" : "继续执行";
    if (policy) {
      String dueAction = "blocked".equals(kind) ? "阻断结束" : "finish".equals(kind) ? "正常结束" : "进入下一步";
      rule =
          switch (state) {
            case "countdown" ->
                "无人回复将在 " + deadline(gate) + " 自动" + dueAction + "。回复此卡片可讨论，消息送达后取消倒计时。";
            case "held" -> "等待人工处理，不会自动流转或结束。请点击“" + label + "”，也可终止任务；如需讨论可回复此卡片。";
            case "busy" -> "正在处理，请等待处理结果。";
            default -> "本轮等待已结束，请查看最新状态。";
          };
    }
    boolean actionFirst = payload.path("actionFirst").asBoolean();
    String text = payload.path("text").asText();
    if (actionFirst) {
      if ("held".equals(state)) {
        rule =
            switch (kind) {
              case "blocked" -> "当前步骤无法继续。请回复此卡片讨论或补充信息，处理后点击「重新执行」。当前仍在等待，不代表开发完成或审核通过。";
              case "finish" -> "请查看最终结果，如需调整可回复此卡片，确认后点击「确认结束」。";
              case "advance" -> "请查看本步骤结果及交付文档，如需调整可回复此卡片，确认后点击「进入下一步」。";
              default -> rule;
            };
      }
      String reason = payload.path("blockedReason").asText();
      String focus = reason.isBlank() ? "" : "**阻碍原因：**" + reason + "\n\n";
      // 原因与回答随卡片冻结，操作指引按当前状态生成，不写入不可变正文。
      text =
          focus
              + "**当前操作：**"
              + rule
              + "\n\n**"
              + (payload.path("answer").asBoolean() ? "本次回答" : "本步执行详情")
              + "**\n\n"
              + text;
    }
    Map<String, Object> values = new LinkedHashMap<>();
    values.put("confirmLabel", label);
    values.put("confirmAction", "blocked".equals(kind) ? "advance_retry" : "advance_confirm");
    values.put("showStop", policy ? "true" : "false");
    values.put(
        "stopStatus",
        "closed".equals(state) || "busy".equals(gate.path("state").asText())
            ? "disabled"
            : "normal");
    values.put("showConfirm", "true");
    values.put("restartMode", "false");
    values.put("stopMode", "false");
    values.put("waitingMode", "true");
    values.put("controlId", payload.path("actionId").asText(""));
    values.put("title", payload.path("title").asText("任务等待确认"));
    values.put(
        "markdown",
        "**"
            + status
            + "**\n\n"
            + payload.path("steps").asText()
            + "\n\n"
            + text
            + (actionFirst ? "" : "\n\n" + rule));
    values.put("workflowId", workflowId);
    values.put("gateId", gateId);
    values.put("confirmStatus", List.of("closed", "busy").contains(state) ? "disabled" : "normal");
    // 初次展示及刷新不代表某次按钮成功；回调按本次网关结果单独返回成功判定。
    values.put("confirmRequestSucceeded", "false");
    StringBuilder documents = new StringBuilder();
    String base = java.net.URI.create(monitorUrl).resolve("document.html").toString();
    for (JsonNode node : snapshot.path("nodes")) {
      if (!node.path("id").asText().equals(payload.path("documentNodeId").asText())) continue;
      for (JsonNode doc : node.path("documents")) {
        if (doc.path("removed").asBoolean()) continue;
        String name = doc.path("name").asText("交付文档");
        for (String special : List.of("[", "]", "(", ")", "`", "<", ">", "\\", "*", "\n", "\r"))
          name = name.replace(special, " ");
        documents.append("- ");
        if (doc.path("primary").asBoolean()) documents.append("【主文档】");
        documents.append(name);
        if (doc.path("error").isTextual()) documents.append("（尚未同步）");
        if (!doc.path("available").asBoolean()) {
          documents.append("\n");
          continue;
        }
        documents
            .append(" [查看文档](")
            .append(base)
            .append("?workflowId=")
            .append(java.net.URLEncoder.encode(workflowId, java.nio.charset.StandardCharsets.UTF_8))
            .append("&documentId=")
            .append(
                java.net.URLEncoder.encode(
                    doc.path("id").asText(), java.nio.charset.StandardCharsets.UTF_8))
            .append(")\n");
      }
    }
    values.put("statusText", status);
    values.put("summary", text);
    values.put("steps", payload.path("steps").asText());
    values.put("notice", actionFirst ? "" : rule);
    values.put("documents", documents.toString());
    values.put("hasDocuments", documents.length() > 0 ? "true" : "false");
    if (!payload.path("documentTemplateId").asText().isBlank())
      values.put("_templateId", payload.path("documentTemplateId").asText());
    else if (documents.length() > 0)
      values.put("markdown", values.get("markdown") + "\n\n**交付文档**\n\n" + documents);
    String context = mentionContext(payload);
    if (!context.isEmpty()) {
      values.put("markdown", context + "\n\n" + values.get("markdown"));
      values.put("statusText", context + "\n\n" + values.get("statusText"));
      if (payload.path("mentionGroup").asBoolean()) {
        values.put("_cardAtUserId", payload.path("atUserId").asText());
        values.put("_cardAtDisplayName", payload.path("atDisplayName").asText());
      }
    }
    String jiraLine = DingTalkJiraContext.line(snapshot.path("jiraContext"));
    if (!jiraLine.isBlank()) {
      values.put("markdown", jiraLine + "\n\n" + values.get("markdown"));
      values.put("statusText", jiraLine + "\n\n" + values.get("statusText"));
    }
    String monitorLink = DingTalkMonitorLink.markdown(monitorUrl, workflowId);
    values.put("markdown", values.get("markdown") + "\n\n" + monitorLink);
    values.put("notice", values.get("notice") + "\n\n" + monitorLink);
    return values;
  }

  private static String mentionContext(JsonNode payload) {
    if (!payload.path("cardMention").asBoolean() || payload.path("atUserId").asText().isBlank())
      return "";
    String name = payload.path("atDisplayName").asText("提问人");
    String person =
        payload.path("mentionGroup").asBoolean()
            ? "<a atId=\""
                + org.springframework.web.util.HtmlUtils.htmlEscape(
                    payload.path("atUserId").asText())
                + "\">"
                + org.springframework.web.util.HtmlUtils.htmlEscape(name)
                + "</a>"
            : plainMarkdown(name);
    if (payload.path("answer").asBoolean()) {
      String question = payload.path("question").asText();
      return "**回复 "
          + person
          + "**"
          + (question.isBlank() ? "" : "\n\n**你的问题：**\n" + plainMarkdown(question))
          + (payload.path("questionHasImages").asBoolean() ? "\n\n（附图片）" : "");
    }
    return person
        + ("advance_retry".equals(payload.path("resultAction").asText())
            ? "，本步骤重新执行已有结果。"
            : "，你确认执行的步骤已有结果。");
  }

  private static String plainMarkdown(String text) {
    String escaped = text;
    for (String token :
        List.of("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", ".", "!", "|")) {
      escaped = escaped.replace(token, "\\" + token);
    }
    return org.springframework.web.util.HtmlUtils.htmlEscape(escaped);
  }

  private static String deadline(JsonNode gate) {
    return java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")
            .withZone(java.time.ZoneId.of("Asia/Shanghai"))
            .format(Instant.parse(gate.path("expiresAt").asText()))
        + "（北京时间）";
  }

  static String cardState(JsonNode snapshot, JsonNode payload) {
    if (payload.path("restartControl").asBoolean()) return "closed";
    return state(snapshot, payload.path("gateId").asText());
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
        result
            .append("blocked".equals(gate.path("kind").asText()) ? "遇到阻碍：" : "已完成：")
            .append(label)
            .append("\n\n");
      if (id.equals(gate.path("nextNodeId").asText())) result.append("下一步：").append(label);
    }
    return DingTalkExecutionNotice.safe(result.toString());
  }

  static String completion(JsonNode snapshot) {
    String completed = snapshot.path("pendingAdvance").path("completedNodeId").asText();
    for (JsonNode node : snapshot.path("nodes")) {
      if (!completed.isBlank() && completed.equals(node.path("id").asText())) {
        String response = DingTalkResultText.display(node.path("response").asText());
        if (!response.isBlank()) return response;
      }
    }
    return "blocked".equals(snapshot.path("pendingAdvance").path("kind").asText())
        ? "当前步骤尚未完成。"
        : "步骤已完成。";
  }

  static String blockedReason(JsonNode snapshot) {
    JsonNode gate = snapshot.path("pendingAdvance");
    if (!"blocked".equals(gate.path("kind").asText())) return "";
    for (JsonNode node : snapshot.path("nodes")) {
      if (node.path("id").asText().equals(gate.path("completedNodeId").asText()))
        return DingTalkExecutionNotice.safe(node.path("error").asText("").strip());
    }
    return "";
  }
}
