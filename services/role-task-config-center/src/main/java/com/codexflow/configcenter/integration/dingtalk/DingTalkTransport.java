package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.domain.DingTalkTargetDirectory;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

/** 隔离钉钉官方 SDK，便于业务测试使用内存替身。 */
interface DingTalkTransport {

  default byte[] downloadImage(String downloadCode) {
    throw new UnsupportedOperationException("当前通道不支持图片下载。");
  }

  default DingTalkModels.SendResult sendReply(
      String target, String targetType, tools.jackson.databind.JsonNode payload) {
    return "PERSON".equals(targetType)
        ? sendPersonText(target, payload.path("text").asText())
        : sendText(target, null, payload.path("text").asText());
  }

  void start(
      Consumer<DingTalkModels.Message> messageHandler,
      java.util.function.Function<DingTalkModels.CardAction, Map<String, Object>> actionHandler);

  void stop();

  boolean connected();

  void testConnection(String clientId, String clientSecret);

  DingTalkModels.SendResult sendText(String conversationId, String replyToMessageId, String text);

  DingTalkModels.SendResult sendMarkdown(
      String conversationId, String replyToMessageId, String title, String markdown);

  default DingTalkModels.SendResult sendPersonText(String userId, String text) {
    throw new UnsupportedOperationException("当前钉钉通道不支持个人消息。");
  }

  default DingTalkModels.SendResult sendPersonMarkdown(
      String userId, String title, String markdown) {
    throw new UnsupportedOperationException("当前钉钉通道不支持个人消息。");
  }

  default List<DingTalkTargetDirectory.RemotePerson> listPeople(
      String clientId, String clientSecret) {
    throw new UnsupportedOperationException("当前钉钉通道不支持通讯录同步。");
  }

  default DingTalkTargetDirectory.RemoteDirectory listDirectory(
      String clientId, String clientSecret) {
    return new DingTalkTargetDirectory.RemoteDirectory(
        List.of(), listPeople(clientId, clientSecret));
  }

  DingTalkModels.SendResult sendCard(
      String conversationId, String replyToMessageId, Map<String, Object> cardData);

  void updateCard(String cardInstanceId, Map<String, Object> cardData);

  default DingTalkModels.SendResult sendWaitingCard(
      String cardId,
      String targetType,
      String targetId,
      String atUserId,
      Map<String, Object> cardData) {
    throw new UnsupportedOperationException("当前通道不支持等待卡片。");
  }
}
