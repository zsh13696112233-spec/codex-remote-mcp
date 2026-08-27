package com.codexflow.configcenter.integration.feishu;

import java.util.Map;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** 飞书 SDK 与业务层之间使用的稳定内部模型。 */
final class FeishuModels {

  private FeishuModels() {}

  record Message(
      String messageId,
      String chatId,
      String chatType,
      String senderOpenId,
      String content,
      boolean mentionedBot,
      boolean mentionAll,
      String rootId,
      String threadId,
      String replyToMessageId) {}

  record CardAction(
      String messageId,
      String chatId,
      String operatorOpenId,
      String actionId,
      Map<String, Object> value) {}

  record SendResult(String messageId) {}

  record StartReservation(String outcome, String workflowId, ObjectNode payload) {}

  record Binding(
      String workflowId,
      String chatId,
      String rootMessageId,
      String threadId,
      String status,
      long eventCursor,
      String progressMessageId,
      boolean waitingAssistant) {}

  record Inbound(String messageId, String workflowId, String workflowMessageId, String status) {}

  record Outbox(
      String id,
      String workflowId,
      String chatId,
      String replyToMessageId,
      String messageKind,
      JsonNode payload) {}
}
