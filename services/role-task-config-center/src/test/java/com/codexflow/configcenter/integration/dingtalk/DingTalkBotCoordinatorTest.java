package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.application.WorkflowRunService;
import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.test.util.ReflectionTestUtils;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 统一入口不依赖群绑定；回答与每次提问关联。 */
class DingTalkBotCoordinatorTest {
  private static final String ID = "00000000-0000-4000-8000-000000000101";
  private static final String OTHER = "00000000-0000-4000-8000-000000000102";
  private final ObjectMapper json = new ObjectMapper();
  private final DingTalkStore store = mock(DingTalkStore.class);
  private final GatewayClient gateway = mock(GatewayClient.class);
  private final WorkflowRunService runs = mock(WorkflowRunService.class);
  private final WorkflowRunStore runStore = mock(WorkflowRunStore.class);
  private final DingTalkTransport transport = mock(DingTalkTransport.class);
  private DingTalkBotCoordinator bot;

  @BeforeEach
  void setup() {
    var properties = new DingTalkProperties();
    properties.setClientId("app");
    bot =
        new DingTalkBotCoordinator(
            properties,
            mock(DingTalkSettingsStore.class),
            transport,
            store,
            runs,
            runStore,
            gateway,
            mock(DingTalkProgressCard.class),
            json);
    when(gateway.get("/workflows/" + ID))
        .thenReturn(json.createObjectNode().put("status", "running"));
    when(runStore.taskDefinitionId(ID)).thenReturn("task");
    when(store.route(eq(ID), any()))
        .thenAnswer(
            call -> {
              DingTalkModels.Message message = call.getArgument(1);
              return new DingTalkModels.Binding(
                  ID,
                  message.conversationId(),
                  "GROUP",
                  message.conversationId(),
                  "群",
                  message.messageId(),
                  "chat",
                  "active",
                  0,
                  null,
                  false);
            });
    when(store.registerInbound(eq("app"), any(), any()))
        .thenReturn(new DingTalkModels.Inbound("question", ID, "assistant-id", "accepted"));
  }

  @Test
  void outboxDrainsSuccessorsInTheSameTickAndContinuesOtherScopesAfterFailure() {
    ReflectionTestUtils.setField(bot, "running", true);
    when(transport.connected()).thenReturn(true);
    var first =
        new DingTalkModels.Outbox(
            "first", ID, "group", null, "text", json.createObjectNode().put("text", "开始"));
    var other =
        new DingTalkModels.Outbox(
            "other", OTHER, "group", null, "text", json.createObjectNode().put("text", "其他任务"));
    var next =
        new DingTalkModels.Outbox(
            "next", OTHER, "group", null, "text", json.createObjectNode().put("text", "其他任务完成"));
    when(store.claimDue(50)).thenReturn(List.of(first, other));
    when(store.claimDue(48)).thenReturn(List.of(next));
    when(store.claimDue(47)).thenReturn(List.of());
    when(transport.sendText(any(), any(), any())).thenReturn(new DingTalkModels.SendResult("sent"));
    when(transport.sendText("group", null, "开始")).thenThrow(new IllegalStateException("临时失败"));
    bot.sendOutbox();
    var order = inOrder(store, transport);
    order.verify(store).claimDue(50);
    order.verify(transport).sendText("group", null, "开始");
    order.verify(store).markOutboxFailed(eq("first"), any());
    order.verify(transport).sendText("group", null, "其他任务");
    order.verify(store).markOutboxSent("other", "sent");
    order.verify(store).claimDue(48);
    order.verify(transport).sendText("group", null, "其他任务完成");
    order.verify(store).markOutboxSent("next", "sent");
    order.verify(store).claimDue(47);
  }

  @Test
  void outboxStopsAfterFiftyDeliveriesEvenWhenMoreAreAvailable() {
    ReflectionTestUtils.setField(bot, "running", true);
    when(transport.connected()).thenReturn(true);
    var item =
        new DingTalkModels.Outbox(
            "id", ID, "group", null, "text", json.createObjectNode().put("text", "消息"));
    when(store.claimDue(anyInt())).thenReturn(List.of(item));
    when(transport.sendText(any(), any(), any())).thenReturn(new DingTalkModels.SendResult("sent"));
    bot.sendOutbox();
    verify(store, times(50)).claimDue(anyInt());
    verify(store, never()).claimDue(0);
    verify(store, times(50)).markOutboxSent("id", "sent");
  }

  private DingTalkModels.Message message(String text) {
    return new DingTalkModels.Message(
        "question",
        "other-group",
        "2",
        "user-2",
        text,
        true,
        false,
        null,
        "群",
        List.of(),
        "https://oapi.dingtalk.com/robot/sendBySession?session=test");
  }

  private JsonNode waiting() {
    var snapshot = json.createObjectNode().put("status", "running");
    snapshot
        .putObject("pendingAdvance")
        .put("gateId", "11111111111111111111111111111111")
        .put("state", "countdown")
        .put("expiresAt", java.time.Instant.now().plusSeconds(120).toString());
    return snapshot;
  }

  @Test
  void confirmContinueUsesObservedGateAndDoesNotCallAssistant() {
    when(gateway.get("/workflows/" + ID)).thenReturn(waiting());
    when(gateway.post(eq("/workflows/" + ID + "/input-observations"), any()))
        .thenReturn(json.createObjectNode().put("gateId", "11111111111111111111111111111111"));
    bot.safelyHandleMessage(message(ID + " 确认继续"));
    verify(gateway)
        .post("/workflows/" + ID + "/advance/11111111111111111111111111111111/confirm", null);
    verify(gateway, never()).post(eq("/workflows/" + ID + "/messages"), any());
  }

  @Test
  void oldWaitingQuoteCannotConfirmNewGate() {
    when(gateway.get("/workflows/" + ID)).thenReturn(waiting());
    when(store.quotedAdvance(any())).thenReturn("old-gate");
    bot.safelyHandleMessage(message(ID + " 确认继续"));
    verify(gateway, never()).post(anyString(), any());
    verify(store).enqueueReply(anyString(), eq(ID), any(), contains("引用的等待已结束"));
  }

  @Test
  void runningContinueIsRememberedAndCannotReleaseLaterWaitOnRetry() {
    when(gateway.post(eq("/workflows/" + ID + "/input-observations"), any()))
        .thenReturn(json.createObjectNode().put("controlAllowed", false));
    bot.safelyHandleMessage(message(ID + " 确认继续"));
    when(gateway.get("/workflows/" + ID)).thenReturn(waiting());
    bot.safelyHandleMessage(message(ID + " 确认继续"));
    verify(gateway, never()).post(contains("/confirm"), any());
    verify(store, atLeastOnce()).markInboundFinished(ID, "assistant-id", false);
  }

  @Test
  void deliveryReportsTimestampAndReceiptRetryDoesNotSendAgain() {
    when(gateway.get("/workflows/" + ID)).thenReturn(waiting());
    when(transport.sendText(any(), any(), any())).thenReturn(new DingTalkModels.SendResult("sent"));
    var payload =
        json.createObjectNode()
            .put("text", "等待确认")
            .put("gateId", "11111111111111111111111111111111");
    var item = new DingTalkModels.Outbox("notice", ID, "group", null, "text", payload);
    bot.deliver(item);
    var order = inOrder(transport, store, gateway);
    order.verify(transport).sendText("group", null, "等待确认");
    order.verify(store).markAdvanceDelivered(eq("notice"), eq("sent"), any());
    order
        .verify(gateway)
        .post(eq("/workflows/" + ID + "/advance/11111111111111111111111111111111/notified"), any());
    payload
        .put("deliveredAt", java.time.Instant.now().minusSeconds(1).toString())
        .put("sentMessageId", "sent");
    bot.deliver(item);
    verify(transport, times(1)).sendText(any(), any(), any());
    verify(gateway, times(2))
        .post(eq("/workflows/" + ID + "/advance/11111111111111111111111111111111/notified"), any());
  }

  @Test
  void expiredWaitingNoticeIsNotSent() {
    var payload = json.createObjectNode().put("text", "等待确认").put("gateId", "old");
    bot.deliver(new DingTalkModels.Outbox("old-notice", ID, "group", null, "text", payload));
    verifyNoInteractions(transport);
    verify(store).markOutboxSuperseded("old-notice");
  }

  @Test
  void questionObservesWaitingBeforeForwardingAndReceivedIsNotContinue() {
    bot.safelyHandleMessage(message(ID + " 收到"));
    var order = inOrder(gateway);
    order.verify(gateway).post(eq("/workflows/" + ID + "/input-observations"), any());
    order.verify(gateway).post(eq("/workflows/" + ID + "/messages"), any());
    verify(gateway, never()).post(contains("/confirm"), any());
  }

  @Test
  void namedStartRepliesWithoutProgressPush() {
    when(store.reserveStart(eq("app"), any()))
        .thenReturn(new DingTalkModels.StartReservation("started", ID, json.createObjectNode()));
    bot.safelyHandleMessage(message("销售日报"));
    verify(runs).submitPrepared(any());
    verify(store).enqueueReply(anyString(), eq(ID), any(), contains("任务已启动"));
    verify(store, never()).enqueueCard(any(), any(), any());
    verify(store, never()).enqueueProgressMarkdown(any(), any(), any(), any());
  }

  @Test
  void busyDoesNotSubmitOrCancel() {
    when(store.reserveStart(eq("app"), any()))
        .thenReturn(new DingTalkModels.StartReservation("busy", ID, null));
    bot.safelyHandleMessage(message("销售日报"));
    verify(runs, never()).submitPrepared(any());
    verify(store).enqueueReply(anyString(), eq(ID), any(), contains("本次未启动"));
  }

  @Test
  void unicodeSpacesAroundMentionAndWorkflowIdStillRouteQuestion() {
    String[] separators = {"\u2005", "\u2002", "\u2003", "\u2009", "\u3000", "\u00a0"};
    for (String space : separators) {
      bot.safelyHandleMessage(message("@sop测试机器人" + space + ID + space + "这是你创建的吗"));
    }
    var request = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway, times(separators.length))
        .post(eq("/workflows/" + ID + "/messages"), request.capture());
    for (JsonNode body : request.getAllValues()) {
      assertThat(body.path("text").asText()).isEqualTo("这是你创建的吗");
    }
    verify(store, never()).reserveStart(any(), any());
  }

  @Test
  void adjacentRichTextMentionAndIdStillUploadImageAndForwardCaption() {
    var parser = new OfficialDingTalkTransport(new DingTalkProperties(), json);
    var incoming =
        parser.toMessage(
            """
        {"msgId":"question","conversationId":"other-group","conversationType":"2",
         "senderStaffId":"user-2","isInAtList":true,"msgtype":"richText",
         "content":{"richText":[{"text":"@sop测试机器人"},{"text":"%s"},
          {"type":"picture","downloadCode":"image-code"},{"text":"这个文件是你新建的吗"}]}}
        """
                .formatted(ID));
    assertThat(incoming.content()).startsWith("@sop测试机器人" + ID);
    byte[] image = new byte[] {1, 2, 3};
    when(transport.downloadImage("image-code")).thenReturn(image);
    when(gateway.uploadImage(ID, image))
        .thenReturn(json.createObjectNode().put("imageId", "image-1"));
    bot.safelyHandleMessage(incoming);
    var request = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway).post(eq("/workflows/" + ID + "/messages"), request.capture());
    assertThat(request.getValue().path("text").asText()).isEqualTo("这个文件是你新建的吗");
    assertThat(request.getValue().path("imageIds").get(0).asText()).isEqualTo("image-1");
    verify(store, never()).enqueueReply(any(), any(), any(), any());
  }

  @Test
  void richTextIdImageCaptionReachesAssistantWithOriginalImage() {
    var parser = new OfficialDingTalkTransport(new DingTalkProperties(), json);
    var incoming =
        parser.toMessage(
            """
        {"msgId":"question","conversationId":"other-group","conversationType":"2",
         "senderStaffId":"user-2","isInAtList":true,"msgtype":"richText",
         "content":{"richText":[{"text":"@sop测试机器人 %s"},
          {"type":"picture","downloadCode":"image-code"},{"text":"这是你写的吗"}]}}
        """
                .formatted(ID));
    byte[] image = new byte[] {1, 2, 3};
    when(transport.downloadImage("image-code")).thenReturn(image);
    when(gateway.uploadImage(ID, image))
        .thenReturn(json.createObjectNode().put("imageId", "image-1"));
    bot.safelyHandleMessage(incoming);
    var request = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway).post(eq("/workflows/" + ID + "/messages"), request.capture());
    assertThat(request.getValue().path("text").asText()).isEqualTo("这是你写的吗");
    assertThat(request.getValue().path("imageIds").get(0).asText()).isEqualTo("image-1");
    verify(store, never()).reserveStart(any(), any());
  }

  @Test
  void richTextQuoteImageAndCaptionReachTheReferencedWorkflow() {
    var parser = new OfficialDingTalkTransport(new DingTalkProperties(), json);
    var incoming =
        parser.toMessage(
            """
        {"msgId":"question","conversationId":"other-group","conversationType":"2",
         "senderStaffId":"user-2","isInAtList":true,"msgtype":"richText",
         "content":{"isReplyMsg":true,"repliedMsg":{"msgId":"task-message",
           "content":{"text":"原任务进度"}},"richText":[
           {"type":"picture","downloadCode":"image-code"},{"text":"怎么有这么多文件"}]}}
        """);
    when(store.conversation(
            eq("app"), argThat(message -> "task-message".equals(message.replyToMessageId()))))
        .thenReturn(
            Optional.of(
                new DingTalkModels.Binding(ID, "other-group", "root", "active", 0, null, false)));
    byte[] image = new byte[] {1, 2, 3};
    when(transport.downloadImage("image-code")).thenReturn(image);
    when(gateway.uploadImage(ID, image))
        .thenReturn(json.createObjectNode().put("imageId", "image-1"));

    bot.safelyHandleMessage(incoming);

    var request = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway).post(eq("/workflows/" + ID + "/messages"), request.capture());
    assertThat(request.getValue().path("text").asText()).isEqualTo("怎么有这么多文件");
    assertThat(request.getValue().path("imageIds"))
        .isEqualTo(json.createArrayNode().add("image-1"));
    verify(store, never()).reserveStart(any(), any());
    verify(store, never()).enqueueReply(any(), any(), any(), any());
  }

  @Test
  void explicitIdRoutesQuestionFromAnotherGroup() {
    bot.safelyHandleMessage(message(ID + " 目前进度"));
    var request = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway).post(eq("/workflows/" + ID + "/messages"), request.capture());
    assertThat(request.getValue().path("text").asText()).isEqualTo("目前进度");
    assertThat(request.getValue().path("actorId").asText()).isEqualTo("app:user-2");
    verify(store).ensureConversation(eq("app"), eq(ID), eq("task"), any(), eq("running"));
  }

  @Test
  void quotedReplyCanOmitId() {
    when(store.conversation(eq("app"), any()))
        .thenReturn(
            Optional.of(
                new DingTalkModels.Binding(ID, "other-group", "root", "active", 0, null, false)));
    bot.safelyHandleMessage(message("为什么"));
    verify(gateway).post(eq("/workflows/" + ID + "/messages"), any());
    verify(store, never()).reserveStart(any(), any());
  }

  @Test
  void unresolvedQuotedTextDoesNotStartNamedTask() {
    bot.safelyHandleMessage(
        new DingTalkModels.Message(
            "question",
            "other-group",
            "2",
            "user-2",
            "销售日报",
            true,
            false,
            null,
            "群",
            List.of(),
            "https://oapi.dingtalk.com/robot/sendBySession?session=test",
            "无法关联的旧回答"));
    verify(store, never()).reserveStart(any(), any());
    verify(gateway, never()).post(any(), any());
    verify(store).enqueueReply(anyString(), isNull(), any(), contains("工作流编号"));
  }

  @Test
  void conflictingIdAndQuoteCannotExecute() {
    when(store.conversation(eq("app"), any()))
        .thenReturn(
            Optional.of(
                new DingTalkModels.Binding(
                    OTHER, "other-group", "root", "active", 0, null, false)));
    bot.safelyHandleMessage(message(ID + " 取消操作"));
    verify(gateway, never()).post(any(), any());
    verify(store).enqueueReply(anyString(), isNull(), any(), contains("不一致"));
  }

  @Test
  void blankMessageDoesNotChooseRecentRun() {
    bot.safelyHandleMessage(message(""));
    verify(store, never()).active(any(), any());
    verify(store, never()).reserveStart(any(), any());
    verify(store).enqueueReply(anyString(), isNull(), any(), contains("完整任务定义名称"));
  }

  @Test
  void imageAtStartIsRejected() {
    var m = message("销售日报");
    bot.safelyHandleMessage(
        new DingTalkModels.Message(
            m.messageId(),
            m.conversationId(),
            "2",
            "user-2",
            m.content(),
            true,
            false,
            null,
            "群",
            List.of("code"),
            m.sessionWebhook()));
    verify(store, never()).reserveStart(any(), any());
    verify(transport, never()).downloadImage(any());
  }

  @Test
  void imageQuestionUploadsAndPassesIds() {
    when(transport.downloadImage("code")).thenReturn(new byte[] {1, 2});
    when(gateway.uploadImage(eq(ID), any()))
        .thenReturn(json.createObjectNode().put("imageId", "a".repeat(64)));
    var m = message(ID + " 看图");
    bot.safelyHandleMessage(
        new DingTalkModels.Message(
            m.messageId(),
            m.conversationId(),
            "2",
            "user-2",
            m.content(),
            true,
            false,
            null,
            "群",
            List.of("code"),
            m.sessionWebhook()));
    var request = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway).post(eq("/workflows/" + ID + "/messages"), request.capture());
    assertThat(request.getValue().path("imageIds").get(0).asText()).isEqualTo("a".repeat(64));
  }

  @Test
  void failedImageDoesNotSubmitTextOnly() {
    when(transport.downloadImage("bad")).thenThrow(new IllegalArgumentException("图片无效"));
    var m = message(ID + " 看图");
    bot.safelyHandleMessage(
        new DingTalkModels.Message(
            m.messageId(),
            m.conversationId(),
            "2",
            "user-2",
            m.content(),
            true,
            false,
            null,
            "群",
            List.of("bad"),
            m.sessionWebhook()));
    var order = inOrder(gateway, transport);
    order.verify(gateway).post(eq("/workflows/" + ID + "/input-observations"), any());
    order.verify(transport).downloadImage("bad");
    verify(gateway, never()).post(eq("/workflows/" + ID + "/messages"), any());
  }

  @Test
  void confirmationMustBelongToSender() {
    var snapshot = json.createObjectNode().put("status", "running");
    snapshot.putObject("pendingControl").put("actionId", "action").put("actorId", "app:user-1");
    when(gateway.get("/workflows/" + ID)).thenReturn(snapshot);
    bot.safelyHandleMessage(message(ID + " 确认执行"));
    verify(gateway, never()).post(eq("/workflows/" + ID + "/messages"), any());
    verify(store).enqueueReply(anyString(), eq(ID), any(), contains("提议人"));
  }

  @Test
  void confirmationCarriesExactAction() {
    var snapshot = json.createObjectNode().put("status", "running");
    snapshot
        .putObject("pendingControl")
        .put("actionId", "action")
        .put("actorId", "app:user-2")
        .put("type", "stop");
    when(gateway.get("/workflows/" + ID)).thenReturn(snapshot);
    bot.safelyHandleMessage(message(ID + " 确认执行"));
    var request = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway).post(eq("/workflows/" + ID + "/messages"), request.capture());
    assertThat(request.getValue().path("expectedActionId").asText()).isEqualTo("action");
  }

  @Test
  void dingtalkProgressEventsReplyToLaunchConversation() {
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    var event = json.createObjectNode().put("type", "node.completed");
    ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 1L);
    verify(store).recordProcess(eq(ID), isNull(), eq(1L), contains("已完成"), eq(false), eq(false));
    verify(store, never()).enqueueCard(any(), any(), any());
  }

  @Test
  void durableProgressUsesOpenApiTextEvenWhenMentionReplyFails() {
    var result = new DingTalkModels.SendResult("real-id");
    when(transport.sendText(eq("launch-group"), any(), any())).thenReturn(result);
    var body =
        new DingTalkModels.Outbox(
            "body",
            ID,
            "launch-group",
            "root",
            "text",
            json.createObjectNode().put("text", "任务完成"));
    var mention =
        new DingTalkModels.Outbox(
            "mention",
            ID,
            "launch-group",
            "root",
            "reply",
            json.createObjectNode().put("text", "查看结果").put("sessionWebhook", "expired"));
    when(transport.sendReply(any(), any(), any())).thenThrow(new IllegalStateException("会话已过期"));
    bot.deliver(body);
    bot.deliver(mention);
    verify(transport).sendText("launch-group", "root", "任务完成");
    verify(store).markOutboxSent("body", "real-id");
    verify(store).markOutboxFailed(eq("mention"), any());
    verify(store, never()).markOutboxFailed(eq("body"), any());
  }

  @Test
  void assistantToolProgressKeepsQuestionId() {
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    var event =
        json.createObjectNode().put("type", "appserver.item/started").put("source", "assistant");
    event
        .putObject("payload")
        .put("messageId", "question-b")
        .putObject("message")
        .putObject("params")
        .putObject("item")
        .put("type", "webSearch");
    ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 2L);
    verify(store)
        .recordProcess(eq(ID), eq("question-b"), eq(2L), contains("搜索资料"), eq(false), eq(false));
  }

  @Test
  void assistantEventUsesPerMessageReply() {
    var binding =
        new DingTalkModels.Binding(ID, "original-group", "root", "active", 0, null, false);
    var event = json.createObjectNode().put("type", "chat.assistant.completed");
    event
        .putObject("payload")
        .put("messageId", "question-id")
        .put("text", "回答")
        .put("actionId", "action");
    ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 1L);
    verify(store).completeReply(ID, "question-id", 1L, "回答", "action");
    verify(store, never()).enqueueCard(any(), any(), any());
  }

  @Test
  void normalGroupMessageWithoutMentionIsIgnored() {
    bot.safelyHandleMessage(
        new DingTalkModels.Message("id", "group", "2", "user", "销售日报", false, false, null));
    verify(store, never()).reserveStart(any(), any());
  }

  @Test
  void singleChatDoesNotNeedMention() {
    when(store.reserveStart(eq("app"), any()))
        .thenReturn(new DingTalkModels.StartReservation("started", ID, json.createObjectNode()));
    bot.safelyHandleMessage(
        new DingTalkModels.Message("id", "private", "1", "user", "销售日报", false, false, null));
    verify(runs).submitPrepared(any());
  }
}
