package com.codexflow.configcenter.integration.dingtalk;

import tools.jackson.databind.JsonNode;

/** 卡片共用的正式 Jira 信息，不从正文提取。 */
final class DingTalkJiraContext {
  private DingTalkJiraContext() {}

  static String line(JsonNode context) {
    if (context.path("issueKey").asText().isBlank()) return "";
    String name = label(context.path("developerName").asText()).strip();
    String developer =
        switch (context.path("developerStatus").asText()) {
          case "resolved" -> name.isBlank() ? "姓名未提供" : name;
          case "multiple" -> name.isBlank() ? "多人（未提供姓名）" : name + "（多人）";
          case "empty" -> "未填写";
          case "unavailable" -> "获取失败";
          default -> "未获取";
        };
    return "**Jira：" + label(context.path("issueKey").asText()) + " ｜ 开发人：" + developer + "**";
  }

  private static String label(String value) {
    return DingTalkExecutionNotice.safe(value).replaceAll("[\\\\\\[\\]()`<>*_\\r\\n]", " ");
  }
}
