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
import tools.jackson.databind.node.ObjectNode;

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
  void stopButtonsUseControlMessagesWithoutRestartReservationOrAdvance() {
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    when(store.binding(ID)).thenReturn(Optional.of(binding));
    var snapshot = json.createObjectNode();
    snapshot
        .putObject("pendingControl")
        .put("actionId", "stop-action")
        .put("type", "stop")
        .put("status", "pending")
        .put("actorId", "app:user")
        .put("expiresAt", java.time.Instant.now().plusSeconds(600).toString());
    when(gateway.get("/workflows/" + ID)).thenReturn(snapshot);
    for (boolean confirm : new boolean[] {true, false}) {
      var action =
          new DingTalkModels.CardAction(
              "wait-stop",
              null,
              "user",
              confirm ? "stop_confirm" : "stop_cancel",
              java.util.Map.of("workflowId", ID, "controlId", "stop-action"));
      var source =
          new DingTalkModels.Message(
              "stop-button", "group", "2", "user", confirm ? "确认执行" : "取消操作", true, false, null);
      when(store.controlCardMessage(action, "app", ID, "stop-action", confirm))
          .thenReturn(Optional.of(source));
      assertThat(bot.safelyHandleAction(action).toString())
          .contains("confirmRequestSucceeded=true");
      verify(gateway)
          .post(
              eq("/workflows/" + ID + "/messages"),
              argThat(
                  body ->
                      source.content().equals(body.path("text").asText())
                          && "stop-action".equals(body.path("expectedActionId").asText())));
    }
    verify(store, never()).acquireForRestart(any(), any());
    verify(gateway, never()).post(contains("/advance/"), any());
  }

  @Test
  void restartButtonsSubmitBoundActionWithoutAdvancingGate() {
    String actionId = "restart-action";
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    when(store.binding(ID)).thenReturn(Optional.of(binding));
    var snapshot = json.createObjectNode();
    var pending =
        snapshot
            .putObject("pendingControl")
            .put("actionId", actionId)
            .put("type", "restart_from")
            .put("status", "pending")
            .put("actorId", "app:user")
            .put("expiresAt", java.time.Instant.now().plusSeconds(600).toString());
    when(gateway.get("/workflows/" + ID)).thenReturn(snapshot);
    for (boolean confirm : new boolean[] {true, false}) {
      var action =
          new DingTalkModels.CardAction(
              "wait-card",
              null,
              "user",
              confirm ? "restart_confirm" : "restart_cancel",
              java.util.Map.of("workflowId", ID, "controlId", actionId));
      var source =
          new DingTalkModels.Message(
              "button", "group", "2", "user", confirm ? "确认执行" : "取消操作", true, false, null);
      when(store.controlCardMessage(action, "app", ID, actionId, confirm))
          .thenReturn(Optional.of(source));
      var result = bot.safelyHandleAction(action);
      assertThat(result.toString()).contains("confirmRequestSucceeded=true");
      verify(gateway)
          .post(
              eq("/workflows/" + ID + "/messages"),
              argThat(
                  body ->
                      actionId.equals(body.path("expectedActionId").asText())
                          && source.content().equals(body.path("text").asText())
                          && "app:user".equals(body.path("actorId").asText())));
      pending.put("actionId", "new-action");
      assertThat(bot.safelyHandleAction(action).toString())
          .contains("confirmRequestSucceeded=false");
      pending.put("actionId", actionId);
    }
    verify(store, times(1)).acquireForRestart("app", ID);
    verify(gateway, never()).post(contains("/advance/"), any());
  }

  @Test
  void processLockRetryRepeatsOnlyDatabaseTransaction() {
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    doThrow(new org.springframework.dao.CannotAcquireLockException("test"))
        .doNothing()
        .when(store)
        .recordProcess(eq(ID), isNull(), eq(1L), anyString(), eq(false), eq(false));
    var event = json.createObjectNode().put("type", "node.started");
    assertThat((Boolean) ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 1L))
        .isTrue();
    verify(store, times(2))
        .recordProcess(eq(ID), isNull(), eq(1L), anyString(), eq(false), eq(false));
    verify(gateway, times(1)).get("/workflows/" + ID);
    verifyNoInteractions(transport);
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
        .post(
            eq("/workflows/" + ID + "/input-observations"),
            argThat(body -> body.has("hold") && !body.path("hold").asBoolean()));
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
    when(transport.sendReply(any(), any(), any()))
        .thenReturn(new DingTalkModels.SendResult("sent"));
    var payload =
        json.createObjectNode()
            .put("text", "等待确认")
            .put("gateId", "11111111111111111111111111111111");
    var item = new DingTalkModels.Outbox("notice", ID, "group", null, "reply", payload);
    bot.deliver(item);
    var order = inOrder(transport, store, gateway);
    order.verify(transport).sendReply(eq("group"), any(), eq(payload));
    order.verify(store).markAdvanceDelivered(eq("notice"), eq("sent"), any());
    order
        .verify(gateway)
        .post(eq("/workflows/" + ID + "/advance/11111111111111111111111111111111/notified"), any());
    payload
        .put("deliveredAt", java.time.Instant.now().minusSeconds(1).toString())
        .put("sentMessageId", "sent");
    bot.deliver(item);
    verify(transport, times(1)).sendReply(any(), any(), any());
    verify(transport, never()).sendText(any(), any(), any());
    verify(gateway, times(2))
        .post(eq("/workflows/" + ID + "/advance/11111111111111111111111111111111/notified"), any());
  }

  @Test
  void waitingSuppressesSupervisorAtConsumptionAndDeliveryButKeepsActualNodeStart() {
    when(gateway.get("/workflows/" + ID)).thenReturn(waiting());
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    var event =
        json.createObjectNode().put("source", "supervisor").put("type", "appserver.item/completed");
    event
        .putObject("payload")
        .putObject("message")
        .putObject("params")
        .putObject("item")
        .put("type", "agentMessage")
        .put("phase", "commentary")
        .put("text", "开始开发");
    ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 1L);
    verify(store)
        .recordEvent(
            eq("app"), eq(ID), eq(1L), anyString(), isNull(), isNull(), isNull(), eq(false));
    verify(store, never())
        .recordProcess(any(), any(), anyLong(), any(), anyBoolean(), anyBoolean(), any());
    var payload = json.createObjectNode().put("text", "开始开发");
    payload.putObject("executionEvent").put("source", "supervisor");
    bot.deliver(new DingTalkModels.Outbox("queued", ID, "group", null, "text", payload));
    verify(store).markOutboxSuperseded("queued");
    verifyNoInteractions(transport);
    when(gateway.get("/workflows/" + ID))
        .thenReturn(json.createObjectNode().put("status", "running"));
    ReflectionTestUtils.invokeMethod(
        bot, "consumeEvent", binding, json.createObjectNode().put("type", "node.started"), 2L);
    verify(store).recordProcess(eq(ID), isNull(), eq(2L), contains("已开始执行"), eq(false), eq(false));
  }

  @Test
  void heldEventUsesOneCombinedNotice() {
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    var snapshot = waiting();
    ((ObjectNode) snapshot.path("pendingAdvance")).put("state", "held");
    when(gateway.get("/workflows/" + ID)).thenReturn(snapshot);
    var event = json.createObjectNode().put("type", "step.advance.held");
    event
        .putObject("payload")
        .put("gateId", snapshot.path("pendingAdvance").path("gateId").asText());
    ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 1L);
    verify(store).recordWaitingCard(ID, 1L, snapshot, false);
    verify(store, never())
        .recordProcess(any(), any(), anyLong(), any(), anyBoolean(), anyBoolean());
  }

  @Test
  void waitingCardReceiptRetryDoesNotResendAndHeldAnswerDoesNotRestartTimer() {
    var snapshot = waiting();
    ((ObjectNode) snapshot.path("pendingAdvance")).put("state", "held");
    when(gateway.get("/workflows/" + ID)).thenReturn(snapshot);
    String gate = snapshot.path("pendingAdvance").path("gateId").asText();
    var payload = json.createObjectNode().put("gateId", gate).put("text", "回答正文");
    when(transport.sendWaitingCard(any(), any(), any(), any(), any()))
        .thenReturn(new DingTalkModels.SendResult("platform-message"));
    bot.deliver(new DingTalkModels.Outbox("answer", ID, "group", null, "waiting_card", payload));
    verify(store).markAdvanceDelivered(eq("answer"), eq("platform-message"), any());
    verify(store).markOutboxSent("answer", "platform-message");
    verify(transport).sendWaitingCard(eq("wait-answer"), eq("GROUP"), eq("group"), isNull(), any());
    verify(gateway, never()).post(contains("/notified"), any());
    payload
        .put("invitation", true)
        .put("deliveredAt", java.time.Instant.now().toString())
        .put("sentMessageId", "platform-message");
    bot.deliver(new DingTalkModels.Outbox("receipt", ID, "group", null, "waiting_card", payload));
    verify(transport, times(1)).sendWaitingCard(any(), any(), any(), any(), any());
    verify(store).markOutboxSent("receipt", "platform-message");
    verify(gateway).post(eq("/workflows/" + ID + "/advance/" + gate + "/notified"), any());
  }

  @Test
  void waitingCardConfirmationValidatesCardAndDirectlyConfirmsWithoutHolding() {
    var action =
        new DingTalkModels.CardAction(
            "wait-card",
            "other-group",
            "user",
            "advance_confirm",
            java.util.Map.of("workflowId", ID, "gateId", "11111111111111111111111111111111"));
    when(store.binding(ID))
        .thenReturn(
            java.util.Optional.of(
                new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false)));
    when(store.ownsWaitingCard(action, ID, "11111111111111111111111111111111")).thenReturn(true);
    var result = bot.safelyHandleAction(action);
    assertThat(
            json.valueToTree(result)
                .path("cardData")
                .path("cardParamMap")
                .path("confirmRequestSucceeded")
                .asText())
        .isEqualTo("true");
    verify(gateway)
        .post("/workflows/" + ID + "/advance/11111111111111111111111111111111/confirm", null);
    verify(gateway, never()).post(contains("/hold"), any());
    verify(gateway, never()).post(contains("/input-observations"), any());
    verify(store).refreshWaitingCards(eq(ID), any());
    when(store.ownsWaitingCard(action, ID, "11111111111111111111111111111111")).thenReturn(false);
    var rejected = bot.safelyHandleAction(action);
    assertThat(
            json.valueToTree(rejected)
                .path("cardData")
                .path("cardParamMap")
                .path("confirmRequestSucceeded")
                .asText())
        .isEqualTo("false");
    verify(gateway, times(1)).post(contains("/confirm"), any());
  }

  @Test
  void expiredWaitingAnswerIsStillDeliveredWithDisabledButton() {
    var payload =
        json.createObjectNode().put("gateId", "old").put("text", "迟到的正式回答").put("answer", true);
    bot.deliver(new DingTalkModels.Outbox("answer", ID, "group", null, "waiting_card", payload));
    var data = ArgumentCaptor.forClass(java.util.Map.class);
    verify(transport)
        .sendWaitingCard(eq("wait-answer"), eq("GROUP"), eq("group"), isNull(), data.capture());
    assertThat(data.getValue()).containsEntry("confirmStatus", "disabled");
    assertThat(data.getValue().get("markdown").toString()).contains("迟到的正式回答");
  }

  @Test
  void completedStepMergesOnlyIntoItsCurrentWaitingGate() {
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    var snapshot = (ObjectNode) waiting();
    ((ObjectNode) snapshot.path("pendingAdvance")).put("completedNodeId", "first");
    when(gateway.get("/workflows/" + ID)).thenReturn(snapshot);
    var event = json.createObjectNode().put("type", "node.completed").put("nodeId", "first");
    assertThat((Boolean) ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 1L))
        .isTrue();
    verify(store).recordWaitingCard(ID, 1L, snapshot, true);
    verify(store, never())
        .recordProcess(any(), any(), anyLong(), any(), anyBoolean(), anyBoolean());
    // Automatic/final completion and a completion from another gate keep the ordinary notice.
    event.put("nodeId", "other");
    ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 2L);
    verify(store).recordProcess(eq(ID), isNull(), eq(2L), contains("已完成"), eq(false), eq(false));
    snapshot.remove("pendingAdvance");
    event.put("nodeId", "first");
    ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 3L);
    verify(store).recordProcess(eq(ID), isNull(), eq(3L), contains("已完成"), eq(false), eq(false));
  }

  @Test
  void continueAcknowledgementIsRetainedUntilNextStepHasStarted() {
    var payload = json.createObjectNode().put("nextNodeId", "second");
    var snapshot = json.createObjectNode();
    var node = snapshot.putArray("nodes").addObject().put("id", "second").put("status", "pending");
    assertThat(
            DingTalkBotCoordinator.redundantAdvanceNotice(
                "step.advance.confirmed", payload, snapshot))
        .isFalse();
    node.put("startedAt", "2026-09-10T07:00:00Z");
    assertThat(
            DingTalkBotCoordinator.redundantAdvanceNotice(
                "step.advance.confirmed", payload, snapshot))
        .isTrue();
    assertThat(
            DingTalkBotCoordinator.redundantAdvanceNotice(
                "step.advance.resumed", payload, snapshot))
        .isTrue();
    assertThat(DingTalkBotCoordinator.redundantAdvanceNotice("node.started", payload, snapshot))
        .isFalse();
    assertThat(DingTalkBotCoordinator.redundantAdvanceNotice("node.failed", payload, snapshot))
        .isFalse();
  }

  @Test
  void expiredCompletionCardStillDeliversItsResult() {
    var payload =
        json.createObjectNode()
            .put("gateId", "old")
            .put("text", "本步骤完整产出")
            .put("retainResult", true);
    bot.deliver(new DingTalkModels.Outbox("result", ID, "group", null, "waiting_card", payload));
    var data = ArgumentCaptor.forClass(java.util.Map.class);
    verify(transport)
        .sendWaitingCard(eq("wait-result"), eq("GROUP"), eq("group"), isNull(), data.capture());
    assertThat(data.getValue()).containsEntry("confirmStatus", "disabled");
    assertThat(data.getValue().get("markdown").toString()).contains("本步骤完整产出");
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
  void cardBodyCanRouteQuestionButCannotGuessWhichWaitToConfirm() {
    when(store.conversation(eq("app"), any()))
        .thenReturn(
            Optional.of(new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false)));
    when(gateway.get("/workflows/" + ID)).thenReturn(waiting());
    var cardQuote =
        new DingTalkModels.Message(
            "card-question",
            "group",
            "2",
            "user",
            "为什么",
            true,
            false,
            "native-id",
            null,
            java.util.List.of(),
            null,
            "任务名称\n工作流编号：" + ID,
            java.util.List.of("native-id"),
            true);
    bot.safelyHandleMessage(cardQuote);
    verify(gateway).post(eq("/workflows/" + ID + "/messages"), any());
    bot.safelyHandleMessage(cardQuote.withContent("继续"));
    verify(gateway, never()).post(contains("/confirm"), any());
    verify(store).enqueueReply(any(), eq(ID), any(), contains("当前等待卡片"));
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
