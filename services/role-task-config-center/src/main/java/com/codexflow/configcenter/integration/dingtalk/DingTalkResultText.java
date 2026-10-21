package com.codexflow.configcenter.integration.dingtalk;

import java.util.regex.Pattern;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 只解开正式步骤协议，不把普通业务 JSON 当成平台结果。 */
final class DingTalkResultText {
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final Pattern RESULT_KEY =
      Pattern.compile("\\\"(?:jiraComment|jiraDeveloper)\\\"\\s*:");
  private static final Pattern OUTCOME_KEY = Pattern.compile("\\\"(?:outcome|summary)\\\"\\s*:");
  private static final String INVALID = "步骤结果暂时无法展示，请查看任务详情。";

  static final String CODEX_FAILURE = "Codex 上游模型服务过载";

  static boolean codexFailure(JsonNode snapshot) {
    if (snapshot.path("termination").isObject()) return false;
    if (CODEX_FAILURE.equals(snapshot.path("error").asText())) return true;
    for (JsonNode node : snapshot.path("nodes")) {
      if ("failed".equals(node.path("status").asText())
          && CODEX_FAILURE.equals(node.path("error").asText())) return true;
    }
    return false;
  }

  private DingTalkResultText() {}

  // null 表示普通文字；非 null 表示已识别的平台结果（含损坏的结果）。
  static String extract(String text) {
    return extract(text, true);
  }

  static String progress(String text) {
    return extract(text, false);
  }

  private static String extract(String text, boolean includeReason) {
    String candidate = text.strip().replace("\r\n", "\n");
    if (candidate.startsWith("```json\n") || candidate.startsWith("```\n")) {
      candidate = candidate.substring(candidate.indexOf('\n') + 1).strip();
      if (candidate.endsWith("```"))
        candidate = candidate.substring(0, candidate.length() - 3).strip();
    }
    for (int depth = 0; depth < 3; depth++) {
      if (!candidate.startsWith("{") && !candidate.startsWith("\"")) return null;
      boolean looksLikeResult =
          RESULT_KEY.matcher(candidate).find() && OUTCOME_KEY.matcher(candidate).find();
      try {
        JsonNode value =
            JSON.readerFor(JsonNode.class)
                .with(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
                .readValue(candidate);
        if (value.isTextual()) {
          candidate = value.asText().strip();
          continue;
        }
        if (!value.isObject()
            || !(value.has("jiraComment") || value.has("jiraDeveloper"))
            || !(value.has("outcome") || value.has("summary"))) return null;
        if (!value.path("summary").isTextual()
            || value.path("summary").asText().isBlank()
            || !java.util.Set.of("success", "blocked", "no_task")
                .contains(value.path("outcome").asText())) return INVALID;
        String summary = value.path("summary").asText();
        String reason = value.path("reason").asText();
        if (includeReason
            && !"success".equals(value.path("outcome").asText())
            && !reason.isBlank()
            && !summary.contains(reason)) summary += "\n原因：" + reason;
        return summary;
      } catch (tools.jackson.core.JacksonException error) {
        return looksLikeResult ? INVALID : null;
      }
    }
    return RESULT_KEY.matcher(candidate).find() && OUTCOME_KEY.matcher(candidate).find()
        ? INVALID
        : null;
  }

  static String display(String text) {
    String result = extract(text);
    return result == null ? text : DingTalkExecutionNotice.safe(result);
  }

  static String toolResult(JsonNode value) {
    String direct = extract(value.isTextual() ? value.asText() : value.toString());
    if (direct != null) return direct;
    if (value.path("isError").asBoolean() || value.hasNonNull("error")) return null;
    // 只解开明确承载步骤协议的文本内容；混合工具结果保留原有详情处理。
    JsonNode content = value.path("content");
    if (!content.isArray() || content.isEmpty()) return null;
    var summaries = new java.util.LinkedHashSet<String>();
    for (JsonNode part : content) {
      if (!"text".equals(part.path("type").asText())) return null;
      String summary = extract(part.path("text").asText());
      if (summary == null) return null;
      summaries.add(summary);
    }
    if (value.hasNonNull("structuredContent")) {
      String structured = extract(value.path("structuredContent").toString());
      if (structured == null) return null;
      summaries.add(structured);
    }
    return String.join("\n\n", summaries);
  }
}
