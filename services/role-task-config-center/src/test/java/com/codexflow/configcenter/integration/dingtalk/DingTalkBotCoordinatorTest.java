package com.codexflow.configcenter.integration.dingtalk;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.codexflow.configcenter.application.WorkflowRunService;
import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Consumer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 不连接真实钉钉，验证消息、卡片动作、发送失败和长连接生命周期。 */
class DingTalkBotCoordinatorTest {

  private static final String WORKFLOW_1 = "00000000-0000-4000-8000-000000000101";
  private static final String WORKFLOW_2 = "00000000-0000-4000-8000-000000000102";
  private static final String WORKFLOW_3 = "00000000-0000-4000-8000-000000000103";
  private static final String GATE_1 = "00000000000040008000000000000201";
  private final ObjectMapper objectMapper = new ObjectMapper();
  private DingTalkProperties properties;
  private TestTransport transport;
  private DingTalkSettingsStore settings;
  private DingTalkStore store;
  private WorkflowRunService workflowRuns;
  private WorkflowRunStore runStore;
  private GatewayClient gateway;
  private DingTalkProgressCard cards;
  private DingTalkBotCoordinator coordinator;

  @BeforeEach
  void setUp() {
    properties = new DingTalkProperties();
    properties.setEnabled(true);
    properties.setClientId("cli_test");
    properties.setClientSecret("test-secret");
    properties.setCardTemplateId("test.schema");
    transport = new TestTransport();
    settings = mock(DingTalkSettingsStore.class);
    store = mock(DingTalkStore.class);
    workflowRuns = mock(WorkflowRunService.class);
    runStore = mock(WorkflowRunStore.class);
    gateway = mock(GatewayClient.class);
    cards = mock(DingTalkProgressCard.class);
    coordinator =
        new DingTalkBotCoordinator(
            properties,
            settings,
            transport,
            store,
            workflowRuns,
            runStore,
            gateway,
            cards,
            objectMapper);
  }

  @Test
  void topLevelMentionStartsOnceAndCreatesProgressCard() {
    ObjectNode payload = objectMapper.createObjectNode().put("workflowId", WORKFLOW_1);
    when(store.conversation(eq("cli_test"), any())).thenReturn(Optional.empty());
    when(store.reserveStart(eq("cli_test"), any()))
        .thenReturn(new DingTalkModels.StartReservation("started", WORKFLOW_1, payload));
    when(gateway.get("/workflows/" + WORKFLOW_1)).thenReturn(snapshot("running"));
    when(cards.render(any(), any(), any())).thenReturn(Map.of("schema", "2.0"));

    coordinator.safelyHandleMessage(message("start-1", "运行", true, null));

    verify(workflowRuns).submitPrepared(any());
    verify(store).markSubmitted(WORKFLOW_1);
    verify(store).enqueueCard(eq("start-card:" + WORKFLOW_1), eq(WORKFLOW_1), any());

    coordinator.safelyHandleMessage(message("ignored", "运行", false, null));
    verify(store, never()).reserveStart(eq("cli_test"), eq(message("ignored", "运行", false, null)));
  }

  @AfterEach
  void close() {
    coordinator.stop();
    coordinator.closeWorkers();
  }

  @Test
  void topLevelMentionWithBlankSdkReplyContextStillStarts() {
    ObjectNode payload = objectMapper.createObjectNode().put("workflowId", WORKFLOW_1);
    DingTalkModels.Message message =
        new DingTalkModels.Message(
            "start-blank-context", "chat-1", "2", "user-1", "运行", true, false, "");
    when(store.conversation("cli_test", message)).thenReturn(Optional.empty());
    when(store.reserveStart("cli_test", message))
        .thenReturn(new DingTalkModels.StartReservation("started", WORKFLOW_1, payload));
    when(gateway.get("/workflows/" + WORKFLOW_1)).thenReturn(snapshot("running"));
    when(cards.render(any(), any(), any())).thenReturn(Map.of("schema", "2.0"));

    coordinator.safelyHandleMessage(message);

    verify(workflowRuns).submitPrepared(any());
    verify(store).markSubmitted(WORKFLOW_1);
    verify(store).enqueueCard(eq("start-card:" + WORKFLOW_1), eq(WORKFLOW_1), any());
  }

  @Test
  void busyWebRunWithoutDingTalkBindingRepliesWithoutForeignKeyReference() {
    DingTalkModels.Message message = message("busy-web-run", "运行", true, null);
    when(store.conversation("cli_test", message)).thenReturn(Optional.empty());
    when(store.reserveStart("cli_test", message))
        .thenReturn(new DingTalkModels.StartReservation("busy", WORKFLOW_1, null));
    when(store.binding(WORKFLOW_1)).thenReturn(Optional.empty());

    coordinator.safelyHandleMessage(message);

    verify(store)
        .enqueueTargetText(
            "start-result:busy-web-run",
            null,
            "chat-1",
            "GROUP",
            "chat-1",
            "busy-web-run",
            "当前绑定已有任务运行，任务编号：" + WORKFLOW_1);
    verify(store, never()).enqueueCard(any(), any(), any());
  }

  @Test
  void blankTemplateStartsWithMarkdownProgressAndConnectsNormally() {
    properties.setCardTemplateId("");
    ObjectNode payload = objectMapper.createObjectNode().put("workflowId", WORKFLOW_1);
    DingTalkModels.Message message = message("text-start", "运行", true, null);
    when(store.conversation("cli_test", message)).thenReturn(Optional.empty());
    when(store.reserveStart("cli_test", message))
        .thenReturn(new DingTalkModels.StartReservation("started", WORKFLOW_1, payload));
    when(gateway.get("/workflows/" + WORKFLOW_1)).thenReturn(snapshot("running"));
    when(cards.renderMarkdown(any(), any())).thenReturn("**任务：** 测试任务");

    coordinator.start();
    coordinator.safelyHandleMessage(message);

    org.assertj.core.api.Assertions.assertThat(coordinator.isRunning()).isTrue();
    verify(store)
        .enqueueProgressMarkdown("start-card:" + WORKFLOW_1, WORKFLOW_1, "任务进度", "**任务：** 测试任务");
    verify(store, never()).enqueueCard(any(), any(), any());
    coordinator.stop();
  }

  @Test
  void textModePauseAndContinueCallAdvanceEndpointsWithoutAssistant() {
    properties.setCardTemplateId("");
    DingTalkModels.Binding binding =
        new DingTalkModels.Binding(WORKFLOW_2, "chat-1", "root-1", "active", 0, null, false);
    DingTalkModels.Message pause = message("pause-1", "暂停", false, "root-1");
    DingTalkModels.Message resume = message("resume-1", "继续", false, "root-1");
    ObjectNode waiting = snapshot("running");
    waiting.put("workflowId", WORKFLOW_2);
    waiting.putObject("pendingAdvance").put("gateId", GATE_1).put("state", "countdown");
    when(store.conversation("cli_test", pause)).thenReturn(Optional.of(binding));
    when(store.conversation("cli_test", resume)).thenReturn(Optional.of(binding));
    when(gateway.get("/workflows/" + WORKFLOW_2)).thenReturn(waiting);
    when(cards.renderMarkdown(any(), any())).thenReturn("**任务：** 测试任务");

    coordinator.safelyHandleMessage(pause);
    coordinator.safelyHandleMessage(resume);

    verify(gateway).post("/workflows/" + WORKFLOW_2 + "/advance/" + GATE_1 + "/hold", null);
    verify(gateway).post("/workflows/" + WORKFLOW_2 + "/advance/" + GATE_1 + "/confirm", null);
    verify(store, never()).registerInbound(eq("cli_test"), any(), any());
    verify(store)
        .enqueueProgressMarkdown("advance-text-result:pause-1", WORKFLOW_2, "任务进度", "**任务：** 测试任务");
    verify(store)
        .enqueueProgressMarkdown(
            "advance-text-result:resume-1", WORKFLOW_2, "任务进度", "**任务：** 测试任务");
  }

  @Test
  void blankTemplatePersistsHighLevelProgressEventAsMarkdownOutbox() {
    properties.setCardTemplateId("");
    DingTalkModels.Binding binding =
        new DingTalkModels.Binding(WORKFLOW_2, "chat-1", "root-1", "active", 0, null, false);
    ObjectNode history = objectMapper.createObjectNode();
    history
        .putArray("events")
        .addObject()
        .put("sequence", 1)
        .put("type", "node.started")
        .putObject("payload");
    when(store.pollable("cli_test")).thenReturn(List.of(binding));
    when(gateway.get("/workflows/" + WORKFLOW_2 + "/events/history?after=0&limit=200&view=bot"))
        .thenReturn(history);
    when(gateway.get("/workflows/" + WORKFLOW_2)).thenReturn(snapshot("running"));
    when(cards.renderMarkdown(any(), any())).thenReturn("**任务：** 测试任务");

    coordinator.start();
    coordinator.scheduleEvents();

    ArgumentCaptor<JsonNode> payload = ArgumentCaptor.forClass(JsonNode.class);
    verify(store, timeout(2000))
        .recordEvent(
            eq("cli_test"),
            eq(WORKFLOW_2),
            eq(1L),
            eq("workflow-event:" + WORKFLOW_2 + ":1"),
            eq("markdown"),
            eq("root-1"),
            payload.capture(),
            eq(false));
    org.assertj.core.api.Assertions.assertThat(payload.getValue().path("title").asText())
        .isEqualTo("任务进度");
    org.assertj.core.api.Assertions.assertThat(payload.getValue().path("text").asText())
        .isEqualTo("**任务：** 测试任务");
    coordinator.stop();
  }

  @Test
  void completedAssistantReplyIsSentAndReflectedInTheSameProgressCard() {
    String workflowMessageId = "11111111-1111-5111-8111-111111111111";
    DingTalkModels.Binding binding =
        new DingTalkModels.Binding(WORKFLOW_2, "chat-1", "root-1", "active", 0, "card-1", true);
    ObjectNode history = objectMapper.createObjectNode();
    history
        .putArray("events")
        .addObject()
        .put("sequence", 2)
        .put("type", "chat.assistant.completed")
        .putObject("payload")
        .put("messageId", workflowMessageId)
        .put("text", "当前正在执行质量审查步骤。");
    when(store.pollable("cli_test")).thenReturn(List.of(binding));
    when(store.inbound(WORKFLOW_2, workflowMessageId))
        .thenReturn(
            Optional.of(
                new DingTalkModels.Inbound(
                    "question-1", WORKFLOW_2, workflowMessageId, "accepted")));
    when(gateway.get("/workflows/" + WORKFLOW_2 + "/events/history?after=0&limit=200&view=bot"))
        .thenReturn(history);
    ObjectNode snapshot = snapshot("running");
    snapshot.put("workflowId", WORKFLOW_2);
    when(gateway.get("/workflows/" + WORKFLOW_2)).thenReturn(snapshot);
    when(cards.render(any(), eq("任务助手已回复。"), eq("当前正在执行质量审查步骤。")))
        .thenReturn(Map.of("markdown", "含最新回复的卡片"));

    coordinator.start();
    coordinator.scheduleEvents();

    ArgumentCaptor<JsonNode> textPayload = ArgumentCaptor.forClass(JsonNode.class);
    ArgumentCaptor<JsonNode> cardPayload = ArgumentCaptor.forClass(JsonNode.class);
    verify(store, timeout(2000))
        .recordAssistantCompleted(
            eq(WORKFLOW_2),
            eq(2L),
            eq("workflow-event:" + WORKFLOW_2 + ":2"),
            eq("question-1"),
            textPayload.capture(),
            eq("当前正在执行质量审查步骤。"),
            eq("assistant-card:" + WORKFLOW_2 + ":2"),
            cardPayload.capture());
    org.assertj.core.api.Assertions.assertThat(textPayload.getValue().path("text").asText())
        .isEqualTo("当前正在执行质量审查步骤。");
    org.assertj.core.api.Assertions.assertThat(cardPayload.getValue().path("markdown").asText())
        .isEqualTo("含最新回复的卡片");
    verify(store, timeout(2000)).markInboundFinished(WORKFLOW_2, workflowMessageId, false);
    coordinator.stop();
  }

  @Test
  void quotedMessageUsesStoredDeterministicAssistantMessageId() {
    DingTalkModels.Binding binding =
        new DingTalkModels.Binding(WORKFLOW_2, "chat-1", "root-1", "active", 0, null, false);
    DingTalkModels.Message message = message("question-1", "现在进度如何", false, "root-1");
    when(store.conversation("cli_test", message)).thenReturn(Optional.of(binding));
    when(store.registerInbound("cli_test", binding, message))
        .thenReturn(
            new DingTalkModels.Inbound(
                "question-1", WORKFLOW_2, "11111111-1111-5111-8111-111111111111", "accepted"));

    coordinator.safelyHandleMessage(message);

    ArgumentCaptor<JsonNode> body = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway).post(eq("/workflows/" + WORKFLOW_2 + "/messages"), body.capture());
    org.assertj.core.api.Assertions.assertThat(body.getValue().path("messageId").asText())
        .isEqualTo("11111111-1111-5111-8111-111111111111");
    org.assertj.core.api.Assertions.assertThat(body.getValue().path("text").asText())
        .isEqualTo("现在进度如何");
  }

  @Test
  void topLevelMentionDuringActiveTaskForwardsToAssistantInSameConversation() {
    DingTalkModels.Binding binding =
        new DingTalkModels.Binding(WORKFLOW_2, "chat-1", "root-1", "active", 0, null, false);
    DingTalkModels.Message message = message("question-top-1", "在吗", true, null);
    when(store.conversation("cli_test", message)).thenReturn(Optional.empty());
    when(store.active("cli_test", message)).thenReturn(Optional.of(binding));
    when(store.registerInbound("cli_test", binding, message))
        .thenReturn(
            new DingTalkModels.Inbound(
                "question-top-1", WORKFLOW_2, "22222222-2222-5222-8222-222222222222", "accepted"));

    coordinator.safelyHandleMessage(message);

    ArgumentCaptor<JsonNode> body = ArgumentCaptor.forClass(JsonNode.class);
    verify(gateway).post(eq("/workflows/" + WORKFLOW_2 + "/messages"), body.capture());
    org.assertj.core.api.Assertions.assertThat(body.getValue().path("text").asText())
        .isEqualTo("在吗");
    verify(store, never()).enqueueText(eq("top-help:question-top-1"), any(), any(), any(), any());
    verify(store, never()).reserveStart(eq("cli_test"), any());
  }

  @Test
  void topLevelMentionDoesNotAttachToActiveTaskInAnotherConversation() {
    DingTalkModels.Binding binding =
        new DingTalkModels.Binding(WORKFLOW_2, "chat-2", "root-2", "active", 0, null, false);
    DingTalkModels.Message message = message("question-top-other", "在吗", true, null);
    when(store.conversation("cli_test", message)).thenReturn(Optional.empty());
    when(store.active("cli_test", message)).thenReturn(Optional.of(binding));

    coordinator.safelyHandleMessage(message);

    verify(store)
        .enqueueTargetText(
            "top-help:question-top-other",
            null,
            "chat-1",
            "GROUP",
            "chat-1",
            "question-top-other",
            "该群尚未绑定任务定义，或当前没有运行中的任务。请联系管理员确认后发送“@机器人 运行”。");
    verify(store, never()).registerInbound(eq("cli_test"), any(), any());
    verify(gateway, never()).post(eq("/workflows/" + WORKFLOW_2 + "/messages"), any());
  }

  @Test
  void repeatedOrExpiredCardActionRefreshesInsteadOfFailing() {
    DingTalkModels.Binding binding =
        new DingTalkModels.Binding(WORKFLOW_3, "chat-1", "root-1", "active", 0, "card-1", false);
    when(store.binding(WORKFLOW_3)).thenReturn(Optional.of(binding));
    when(gateway.post("/workflows/" + WORKFLOW_3 + "/advance/" + GATE_1 + "/confirm", null))
        .thenReturn(objectMapper.createObjectNode());
    when(gateway.get("/workflows/" + WORKFLOW_3)).thenReturn(snapshot("running"));
    when(cards.render(any(), any(), any())).thenReturn(Map.of("schema", "2.0"));

    coordinator.safelyHandleAction(
        new DingTalkModels.CardAction(
            "card-1",
            "chat-1",
            "user-2",
            "button",
            Map.of(
                "action", "advance_confirm",
                "workflowId", WORKFLOW_3,
                "gateId", GATE_1)));

    verify(gateway).post("/workflows/" + WORKFLOW_3 + "/advance/" + GATE_1 + "/confirm", null);
    verify(store)
        .enqueueCard(
            eq("card-action:" + WORKFLOW_3 + ":" + GATE_1 + ":advance_confirm"),
            eq(WORKFLOW_3),
            any());
  }

  @Test
  void sendFailureReturnsOutboxItemToRetryAndLifecycleCanReconnect() {
    coordinator.start();
    verify(store).initialize("cli_test");
    org.assertj.core.api.Assertions.assertThat(coordinator.isRunning()).isTrue();
    coordinator.stop();
    coordinator.start();
    org.assertj.core.api.Assertions.assertThat(coordinator.isRunning()).isTrue();

    transport.sendFailure = new IllegalStateException("offline");
    DingTalkModels.Outbox item =
        new DingTalkModels.Outbox(
            "outbox-1",
            null,
            "chat-1",
            "message-1",
            "text",
            objectMapper.createObjectNode().put("text", "hello"));
    coordinator.deliver(item);

    verify(store).markOutboxFailed(eq("outbox-1"), any(RuntimeException.class));
    coordinator.stop();
  }

  @Test
  void markdownOutboxUsesBuiltInMarkdownMessageType() {
    DingTalkModels.Outbox item =
        new DingTalkModels.Outbox(
            "outbox-markdown-1",
            WORKFLOW_1,
            "chat-1",
            "message-1",
            "markdown",
            objectMapper.createObjectNode().put("title", "任务进度").put("text", "**状态：** 运行中"));

    coordinator.deliver(item);

    org.assertj.core.api.Assertions.assertThat(transport.lastMarkdownTitle).isEqualTo("任务进度");
    org.assertj.core.api.Assertions.assertThat(transport.lastMarkdown).isEqualTo("**状态：** 运行中");
    verify(store).markOutboxSent("outbox-markdown-1", "sent-markdown-1");
  }

  @Test
  void configuredPersonCanChatWithoutMentionAndReceivesDirectMessages() {
    DingTalkModels.Binding binding =
        new DingTalkModels.Binding(
            WORKFLOW_2,
            "person-chat-1",
            "PERSON",
            "person-1",
            "测试人员",
            "root-1",
            "active",
            0,
            null,
            false);
    DingTalkModels.Message message =
        new DingTalkModels.Message(
            "person-question-1", "person-chat-1", "1", "person-1", "现在进度如何", false, false, null);
    when(store.active("cli_test", message)).thenReturn(Optional.of(binding));
    when(store.registerInbound("cli_test", binding, message))
        .thenReturn(
            new DingTalkModels.Inbound(
                "person-question-1",
                WORKFLOW_2,
                "11111111-1111-5111-8111-111111111111",
                "accepted"));

    coordinator.safelyHandleMessage(message);

    verify(gateway).post(eq("/workflows/" + WORKFLOW_2 + "/messages"), any());

    DingTalkModels.Outbox item =
        new DingTalkModels.Outbox(
            "person-outbox-1",
            WORKFLOW_2,
            "person-chat-1",
            "PERSON",
            "person-1",
            null,
            "text",
            objectMapper.createObjectNode().put("text", "个人通知"));
    coordinator.deliver(item);

    org.assertj.core.api.Assertions.assertThat(transport.lastPersonId).isEqualTo("person-1");
    org.assertj.core.api.Assertions.assertThat(transport.lastPersonText).isEqualTo("个人通知");
    verify(store).markOutboxSent("person-outbox-1", "sent-person-1");
  }

  @Test
  void staleCardUpdateIsDiscardedBeforeItCanOverwriteNewerProgress() {
    DingTalkModels.Outbox item =
        new DingTalkModels.Outbox(
            "old-card-update",
            WORKFLOW_1,
            "chat-1",
            "root-1",
            "card_update",
            objectMapper.createObjectNode().putObject("card").put("markdown", "旧进度"));
    when(store.isLatestCardOutbox("old-card-update", WORKFLOW_1)).thenReturn(false);

    coordinator.deliver(item);

    verify(store).markOutboxSuperseded("old-card-update");
    verify(store, never()).markOutboxSent(eq("old-card-update"), any());
    verify(store, never()).markOutboxFailed(eq("old-card-update"), any());
  }

  private ObjectNode snapshot(String status) {
    ObjectNode snapshot = objectMapper.createObjectNode();
    snapshot.put("workflowId", WORKFLOW_1);
    snapshot.put("name", "测试任务");
    snapshot.put("status", status);
    snapshot.putObject("progress").put("completed", 0).put("total", 1);
    snapshot.putArray("nodes");
    return snapshot;
  }

  @Test
  void filteredNoiseAdvancesCursorOnceAndOneSlowBindingDoesNotBlockAnother() throws Exception {
    coordinator.start();
    var slow = new DingTalkModels.Binding(WORKFLOW_1, "chat-1", "root-1", "active", 0, null, false);
    var fast = new DingTalkModels.Binding(WORKFLOW_2, "chat-2", "root-2", "active", 0, null, false);
    when(store.pollable("cli_test")).thenReturn(List.of(slow, fast));
    var entered = new java.util.concurrent.CountDownLatch(1);
    var release = new java.util.concurrent.CountDownLatch(1);
    var empty = objectMapper.createObjectNode().put("nextCursor", 10000);
    empty.putArray("events");
    when(gateway.get("/workflows/" + WORKFLOW_1 + "/events/history?after=0&limit=200&view=bot"))
        .thenAnswer(
            invocation -> {
              entered.countDown();
              release.await();
              return empty;
            });
    when(gateway.get("/workflows/" + WORKFLOW_2 + "/events/history?after=0&limit=200&view=bot"))
        .thenReturn(empty);
    try {
      coordinator.scheduleEvents();
      org.junit.jupiter.api.Assertions.assertTrue(
          entered.await(2, java.util.concurrent.TimeUnit.SECONDS));
      verify(store, org.mockito.Mockito.timeout(2000))
          .recordEvent(
              "cli_test",
              WORKFLOW_2,
              10000,
              "cursor:" + WORKFLOW_2 + ":10000",
              null,
              null,
              null,
              false);
    } finally {
      release.countDown();
      coordinator.closeWorkers();
      coordinator.stop();
    }
  }

  private static DingTalkModels.Message message(
      String messageId, String content, boolean mentioned, String rootId) {
    return new DingTalkModels.Message(
        messageId, "chat-1", "2", "user-1", content, mentioned, false, rootId);
  }

  private static final class TestTransport implements DingTalkTransport {
    boolean connected;
    RuntimeException sendFailure;
    String lastMarkdownTitle;
    String lastMarkdown;
    String lastPersonId;
    String lastPersonText;

    @Override
    public void start(
        Consumer<DingTalkModels.Message> messageHandler,
        Consumer<DingTalkModels.CardAction> actionHandler) {
      connected = true;
    }

    @Override
    public void stop() {
      connected = false;
    }

    @Override
    public boolean connected() {
      return connected;
    }

    @Override
    public void testConnection(String clientId, String clientSecret) {
      if (sendFailure != null) throw sendFailure;
    }

    @Override
    public DingTalkModels.SendResult sendText(
        String conversationId, String replyToMessageId, String text) {
      if (sendFailure != null) throw sendFailure;
      return new DingTalkModels.SendResult("sent-1");
    }

    @Override
    public DingTalkModels.SendResult sendMarkdown(
        String conversationId, String replyToMessageId, String title, String markdown) {
      if (sendFailure != null) throw sendFailure;
      lastMarkdownTitle = title;
      lastMarkdown = markdown;
      return new DingTalkModels.SendResult("sent-markdown-1");
    }

    @Override
    public DingTalkModels.SendResult sendPersonText(String userId, String text) {
      if (sendFailure != null) throw sendFailure;
      lastPersonId = userId;
      lastPersonText = text;
      return new DingTalkModels.SendResult("sent-person-1");
    }

    @Override
    public DingTalkModels.SendResult sendCard(
        String conversationId, String replyToMessageId, Map<String, Object> card) {
      return new DingTalkModels.SendResult("sent-card-1");
    }

    @Override
    public void updateCard(String cardInstanceId, Map<String, Object> card) {}
  }
}
