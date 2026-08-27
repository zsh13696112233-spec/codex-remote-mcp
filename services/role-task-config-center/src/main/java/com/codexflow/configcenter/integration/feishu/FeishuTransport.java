package com.codexflow.configcenter.integration.feishu;

import java.util.Map;
import java.util.function.Consumer;

/** 隔离飞书官方 SDK，便于业务测试使用内存替身。 */
interface FeishuTransport {

  void start(
      Consumer<FeishuModels.Message> messageHandler,
      Consumer<FeishuModels.CardAction> actionHandler);

  void stop();

  boolean connected();

  void testConnection(String appId, String appSecret);

  FeishuModels.SendResult sendText(String chatId, String replyToMessageId, String text);

  FeishuModels.SendResult sendCard(
      String chatId, String replyToMessageId, Map<String, Object> card);

  void updateCard(String messageId, Map<String, Object> card);
}
