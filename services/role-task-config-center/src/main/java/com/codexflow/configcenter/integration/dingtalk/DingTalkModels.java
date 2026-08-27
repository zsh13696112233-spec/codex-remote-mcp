package com.codexflow.configcenter.integration.dingtalk;

import java.util.Map;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** 钉钉 SDK 与业务层之间使用的稳定内部模型。 */
final class DingTalkModels {

  private DingTalkModels() {}

  record Message(
      String messageId,
      String conversationId,
      String conversationType,
      String senderUserId,
      String content,
      boolean mentionedBot,
      boolean mentionAll,
      String replyToMessageId) {}

  record CardAction(
      String cardInstanceId,
      String conversationId,
      String operatorUserId,
      String actionId,
      Map<String, Object> value) {}

  record SendResult(String messageId) {}

  record StartReservation(String outcome, String workflowId, ObjectNode payload) {}

  record Binding(
      String workflowId,
      String conversationId,
      String rootMessageId,
      String status,
      long eventCursor,
      String progressCardInstanceId,
      boolean waitingAssistant) {}

  record Inbound(String messageId, String workflowId, String workflowMessageId, String status) {}

  record Outbox(
      String id,
      String workflowId,
      String conversationId,
      String replyToMessageId,
      String messageKind,
      JsonNode payload) {}
}
