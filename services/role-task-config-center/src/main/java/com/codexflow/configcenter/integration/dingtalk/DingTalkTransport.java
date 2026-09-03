package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.domain.DingTalkTargetDirectory;
import java.util.List;
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
}
