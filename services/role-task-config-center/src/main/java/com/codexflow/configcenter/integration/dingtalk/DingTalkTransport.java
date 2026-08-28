package com.codexflow.configcenter.integration.dingtalk;

import java.util.Map;
import java.util.function.Consumer;

/** 隔离钉钉官方 SDK，便于业务测试使用内存替身。 */
interface DingTalkTransport {

  void start(
      Consumer<DingTalkModels.Message> messageHandler,
      Consumer<DingTalkModels.CardAction> actionHandler);

  void stop();

  boolean connected();

  void testConnection(String clientId, String clientSecret);

  DingTalkModels.SendResult sendText(String conversationId, String replyToMessageId, String text);

  DingTalkModels.SendResult sendMarkdown(
      String conversationId, String replyToMessageId, String title, String markdown);

  DingTalkModels.SendResult sendCard(
      String conversationId, String replyToMessageId, Map<String, Object> cardData);

  void updateCard(String cardInstanceId, Map<String, Object> cardData);
}
