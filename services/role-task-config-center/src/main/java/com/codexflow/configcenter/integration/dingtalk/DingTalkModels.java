package com.codexflow.configcenter.integration.dingtalk;

import java.util.List;
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
      String replyToMessageId,
      String conversationTitle,
      List<String> imageCodes,
      String sessionWebhook,
      String quotedText,
      List<String> referenceIds,
      boolean quotedCard) {

    Message(
        String messageId,
        String conversationId,
        String conversationType,
        String senderUserId,
        String content,
        boolean mentionedBot,
        boolean mentionAll,
        String replyToMessageId,
        String conversationTitle,
        List<String> imageCodes,
        String sessionWebhook,
        String quotedText,
        List<String> referenceIds) {
      this(
          messageId,
          conversationId,
          conversationType,
          senderUserId,
          content,
          mentionedBot,
          mentionAll,
          replyToMessageId,
          conversationTitle,
          imageCodes,
          sessionWebhook,
          quotedText,
          referenceIds,
          false);
    }

    Message {
      var ids = new java.util.LinkedHashSet<String>();
      if (replyToMessageId != null && !replyToMessageId.isBlank()) ids.add(replyToMessageId);
      if (referenceIds != null)
        for (String id : referenceIds) if (id != null && !id.isBlank()) ids.add(id);
      referenceIds = List.copyOf(ids);
    }

    Message(
        String messageId,
        String conversationId,
        String conversationType,
        String senderUserId,
        String content,
        boolean mentionedBot,
        boolean mentionAll,
        String replyToMessageId,
        String conversationTitle,
        List<String> imageCodes,
        String sessionWebhook,
        String quotedText) {
      this(
          messageId,
          conversationId,
          conversationType,
          senderUserId,
          content,
          mentionedBot,
          mentionAll,
          replyToMessageId,
          conversationTitle,
          imageCodes,
          sessionWebhook,
          quotedText,
          List.of());
    }

    Message withReference(String id) {
      return new Message(
          messageId,
          conversationId,
          conversationType,
          senderUserId,
          content,
          mentionedBot,
          mentionAll,
          id,
          conversationTitle,
          imageCodes,
          sessionWebhook,
          quotedText,
          List.of(),
          quotedCard);
    }

    Message(
        String messageId,
        String conversationId,
        String conversationType,
        String senderUserId,
        String content,
        boolean mentionedBot,
        boolean mentionAll,
        String replyToMessageId,
        String conversationTitle,
        List<String> imageCodes,
        String sessionWebhook) {
      this(
          messageId,
          conversationId,
          conversationType,
          senderUserId,
          content,
          mentionedBot,
          mentionAll,
          replyToMessageId,
          conversationTitle,
          imageCodes,
          sessionWebhook,
          null);
    }

    Message(
        String messageId,
        String conversationId,
        String conversationType,
        String senderUserId,
        String content,
        boolean mentionedBot,
        boolean mentionAll,
        String replyToMessageId,
        String conversationTitle) {
      this(
          messageId,
          conversationId,
          conversationType,
          senderUserId,
          content,
          mentionedBot,
          mentionAll,
          replyToMessageId,
          conversationTitle,
          List.of(),
          null);
    }

    Message withContent(String text) {
      return new Message(
          messageId,
          conversationId,
          conversationType,
          senderUserId,
          text,
          mentionedBot,
          mentionAll,
          replyToMessageId,
          conversationTitle,
          imageCodes,
          sessionWebhook,
          quotedText,
          referenceIds,
          quotedCard);
    }

    Message(
        String messageId,
        String conversationId,
        String conversationType,
        String senderUserId,
        String content,
        boolean mentionedBot,
        boolean mentionAll,
        String replyToMessageId) {
      this(
          messageId,
          conversationId,
          conversationType,
          senderUserId,
          content,
          mentionedBot,
          mentionAll,
          replyToMessageId,
          null);
    }
  }

  record CardAction(
      String cardInstanceId,
      String conversationId,
      String operatorUserId,
      String actionId,
      Map<String, Object> value) {}

  record SendResult(String messageId) {}

  static String referenceFingerprint(String id) {
    if (id == null || id.isBlank()) return "无";
    try {
      byte[] digest =
          java.security.MessageDigest.getInstance("SHA-256")
              .digest(id.getBytes(java.nio.charset.StandardCharsets.UTF_8));
      return id.length() + ":" + java.util.HexFormat.of().formatHex(digest).substring(0, 16);
    } catch (java.security.NoSuchAlgorithmException error) {
      throw new IllegalStateException(error);
    }
  }

  record StartReservation(String outcome, String workflowId, ObjectNode payload) {}

  record Binding(
      String workflowId,
      String conversationId,
      String targetType,
      String targetExternalId,
      String targetName,
      String rootMessageId,
      String triggerSource,
      String status,
      long eventCursor,
      String progressCardInstanceId,
      boolean waitingAssistant) {

    Binding(
        String workflowId,
        String conversationId,
        String targetType,
        String targetExternalId,
        String targetName,
        String rootMessageId,
        String status,
        long eventCursor,
        String progressCardInstanceId,
        boolean waitingAssistant) {
      this(
          workflowId,
          conversationId,
          targetType,
          targetExternalId,
          targetName,
          rootMessageId,
          "dingtalk",
          status,
          eventCursor,
          progressCardInstanceId,
          waitingAssistant);
    }

    Binding(
        String workflowId,
        String conversationId,
        String rootMessageId,
        String status,
        long eventCursor,
        String progressCardInstanceId,
        boolean waitingAssistant) {
      this(
          workflowId,
          conversationId,
          "GROUP",
          conversationId,
          "群聊",
          rootMessageId,
          "dingtalk",
          status,
          eventCursor,
          progressCardInstanceId,
          waitingAssistant);
    }
  }

  record Inbound(String messageId, String workflowId, String workflowMessageId, String status) {}

  record Outbox(
      String id,
      String workflowId,
      String conversationId,
      String targetType,
      String targetExternalId,
      String replyToMessageId,
      String messageKind,
      JsonNode payload) {

    Outbox(
        String id,
        String workflowId,
        String conversationId,
        String replyToMessageId,
        String messageKind,
        JsonNode payload) {
      this(
          id,
          workflowId,
          conversationId,
          "GROUP",
          conversationId,
          replyToMessageId,
          messageKind,
          payload);
    }
  }
}
