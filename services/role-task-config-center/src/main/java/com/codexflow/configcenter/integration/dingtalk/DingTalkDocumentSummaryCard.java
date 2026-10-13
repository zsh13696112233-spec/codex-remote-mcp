package com.codexflow.configcenter.integration.dingtalk;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import tools.jackson.databind.JsonNode;

/** 全自动结束结果与交付文档；不承载流程控制。 */
final class DingTalkDocumentSummaryCard {
  private DingTalkDocumentSummaryCard() {}

  static boolean applicable(JsonNode snapshot) {
    return "automatic".equals(snapshot.path("advanceMode").asText())
        && snapshot.path("documents").isArray()
        && !snapshot.path("documents").isEmpty();
  }

  static Map<String, Object> render(
      String workflowId, JsonNode snapshot, String notice, String monitorUrl, String templateId) {
    String base = monitorUrl.replaceAll("[?#].*$", "").replaceAll("/+$", "") + "/document.html";
    StringBuilder documents = new StringBuilder();
    for (JsonNode doc : snapshot.path("documents")) {
      documents
          .append("- ")
          .append(label(doc.path("name").asText("交付文档")))
          .append(" · 第")
          .append(doc.path("stepNumber").asInt())
          .append("步「")
          .append(label(doc.path("stepName").asText()))
          .append("」");
      boolean failed = doc.path("error").isTextual();
      if (failed) documents.append("（最新版本未同步）");
      if (doc.path("available").asBoolean()) {
        documents
            .append(failed ? " [查看上次成功版本](" : " [查看文档](")
            .append(base)
            .append("?workflowId=")
            .append(encode(workflowId))
            .append("&documentId=")
            .append(encode(doc.path("id").asText()))
            .append(")");
      }
      documents.append("\n");
    }
    Map<String, Object> data = new LinkedHashMap<>();
    data.put("_templateId", templateId);
    data.put("title", label(snapshot.path("name").asText("任务")) + " · 结束结果");
    data.put(
        "statusText",
        switch (snapshot.path("status").asText()) {
          case "cancelled" -> "已取消";
          case "failed" -> snapshot.path("termination").isObject() ? "因阻断结束" : "执行失败";
          default -> snapshot.path("termination").isObject() ? "无待处理任务，已结束" : "已完成";
        });
    data.put("summary", notice);
    data.put("documents", documents.toString());
    data.put("hasDocuments", "true");
    data.put("steps", "");
    data.put("notice", "文档为本次运行交付时保存的正文；同一文档仅列出最后交付版本。");
    data.put("showConfirm", "false");
    data.put("showStop", "false");
    data.put("workflowId", workflowId);
    return data;
  }

  private static String encode(String value) {
    return URLEncoder.encode(value, StandardCharsets.UTF_8);
  }

  private static String label(String value) {
    return DingTalkExecutionNotice.safe(value).replaceAll("[\\\\\\[\\]()`<>*_\\r\\n]", " ");
  }

  static final class NotSentFailure extends RuntimeException {
    final boolean retryable;

    NotSentFailure(boolean retryable) {
      super(retryable ? "文档汇总卡片暂未发送，等待重试。" : "文档汇总卡片未发送，请检查模板及接收目标配置。");
      this.retryable = retryable;
    }
  }
}
