package com.codexflow.configcenter.integration.feishu;

import com.lark.oapi.channel.LarkChannel;
import com.lark.oapi.channel.LarkChannelFactory;
import com.lark.oapi.channel.config.LarkChannelOptions;
import com.lark.oapi.channel.model.CardActionEvent;
import com.lark.oapi.channel.model.NormalizedMessage;
import com.lark.oapi.channel.model.SendInput;
import com.lark.oapi.channel.model.SendOptions;
import java.util.Map;
import java.util.concurrent.CompletionException;
import java.util.function.Consumer;
import org.springframework.stereotype.Component;

/** 基于飞书官方 Java SDK 长连接通道的生产实现。 */
@Component
class OfficialFeishuTransport implements FeishuTransport {

  private final FeishuProperties properties;
  private volatile LarkChannel channel;

  OfficialFeishuTransport(FeishuProperties properties) {
    this.properties = properties;
  }

  @Override
  public synchronized void start(
      Consumer<FeishuModels.Message> messageHandler,
      Consumer<FeishuModels.CardAction> actionHandler) {
    if (channel != null && channel.isConnected()) return;
    LarkChannel created = createChannel(properties.getAppId(), properties.getAppSecret());
    created.on("message", (NormalizedMessage value) -> messageHandler.accept(toMessage(value)));
    created.on("cardAction", (CardActionEvent value) -> actionHandler.accept(toAction(value)));
    created.connectSync();
    channel = created;
  }

  @Override
  public synchronized void stop() {
    if (channel == null) return;
    channel.disconnectSync();
    channel = null;
  }

  @Override
  public boolean connected() {
    LarkChannel current = channel;
    return current != null && current.isConnected();
  }

  @Override
  public void testConnection(String appId, String appSecret) {
    LarkChannel testChannel = createChannel(appId, appSecret);
    try {
      testChannel.connectSync();
    } finally {
      if (testChannel.isConnected()) testChannel.disconnectSync();
    }
  }

  @Override
  public FeishuModels.SendResult sendText(String chatId, String replyToMessageId, String text) {
    try {
      com.lark.oapi.channel.model.SendResult result =
          requiredChannel().send(chatId, SendInput.text(text), options(replyToMessageId)).join();
      return new FeishuModels.SendResult(result.getMessageId());
    } catch (CompletionException error) {
      throw transportFailure(error);
    }
  }

  @Override
  public FeishuModels.SendResult sendCard(
      String chatId, String replyToMessageId, Map<String, Object> card) {
    try {
      com.lark.oapi.channel.model.SendResult result =
          requiredChannel().send(chatId, SendInput.card(card), options(replyToMessageId)).join();
      return new FeishuModels.SendResult(result.getMessageId());
    } catch (CompletionException error) {
      throw transportFailure(error);
    }
  }

  @Override
  public void updateCard(String messageId, Map<String, Object> card) {
    try {
      requiredChannel().updateCard(messageId, card).join();
    } catch (CompletionException error) {
      throw transportFailure(error);
    }
  }

  private LarkChannel requiredChannel() {
    LarkChannel current = channel;
    if (current == null || !current.isConnected()) {
      throw new IllegalStateException("飞书长连接当前不可用。");
    }
    return current;
  }

  private static LarkChannel createChannel(String appId, String appSecret) {
    LarkChannelOptions.PolicyConfig policy = new LarkChannelOptions.PolicyConfig();
    policy.setDmMode("disabled");
    policy.setRequireMention(false);
    policy.setRespondToMentionAll(false);
    return LarkChannelFactory.createLarkChannel(
        LarkChannelOptions.newBuilder(appId, appSecret)
            .transport("websocket")
            .policy(policy)
            .build());
  }

  private static SendOptions options(String replyToMessageId) {
    SendOptions.Builder builder = SendOptions.newBuilder().replyInThread(true);
    if (replyToMessageId != null && !replyToMessageId.isBlank()) {
      builder.replyTo(replyToMessageId);
    }
    return builder.build();
  }

  private static FeishuModels.Message toMessage(NormalizedMessage value) {
    return new FeishuModels.Message(
        value.getMessageId(),
        value.getChatId(),
        value.getChatType(),
        value.getSenderId(),
        value.getContent(),
        value.isMentionedBot(),
        value.isMentionAll(),
        value.getRootId(),
        value.getThreadId(),
        value.getReplyToMessageId());
  }

  private static FeishuModels.CardAction toAction(CardActionEvent value) {
    String actionId = value.getActionName();
    if (actionId == null || actionId.isBlank()) actionId = value.getActionTag();
    return new FeishuModels.CardAction(
        value.getMessageId(),
        value.getChatId(),
        value.getOperatorId(),
        actionId,
        value.getActionValue() == null ? Map.of() : value.getActionValue());
  }

  private static RuntimeException transportFailure(CompletionException error) {
    Throwable cause = error.getCause() == null ? error : error.getCause();
    return new IllegalStateException("飞书消息发送失败：" + cause.getMessage(), cause);
  }
}
