package com.codexflow.configcenter.integration.feishu;

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
import com.codexflow.configcenter.integration.bot.BotPlatformGuard;
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

/** 不连接真实飞书，验证消息、卡片动作、发送失败和长连接生命周期。 */
class FeishuBotCoordinatorTest {

  private static final String TASK_ID = "00000000-0000-4000-8000-000000000001";
  private static final String WORKFLOW_1 = "00000000-0000-4000-8000-000000000101";
  private static final String WORKFLOW_2 = "00000000-0000-4000-8000-000000000102";
  private static final String WORKFLOW_3 = "00000000-0000-4000-8000-000000000103";
  private static final String GATE_1 = "00000000000040008000000000000201";
  private final ObjectMapper objectMapper = new ObjectMapper();
  private FeishuProperties properties;
  private TestTransport transport;
  private FeishuSettingsStore settings;
  private FeishuStore store;
  private WorkflowRunService workflowRuns;
  private WorkflowRunStore runStore;
  private GatewayClient gateway;
  private FeishuProgressCard cards;
  private FeishuBotCoordinator coordinator;

  @BeforeEach
  void setUp() {
    properties = new FeishuProperties();
    properties.setEnabled(true);
    properties.setAppId("cli_test");
    properties.setAppSecret("test-secret");
    properties.setTaskDefinitionId(TASK_ID);
    transport = new TestTransport();
    settings = mock(FeishuSettingsStore.class);
    store = mock(FeishuStore.class);
    workflowRuns = mock(WorkflowRunService.class);
    runStore = mock(WorkflowRunStore.class);
    gateway = mock(GatewayClient.class);
    cards = mock(FeishuProgressCard.class);
    coordinator =
        new FeishuBotCoordinator(
            properties,
            settings,
            transport,
            store,
            workflowRuns,
            runStore,
            gateway,
            cards,
            objectMapper,
            mock(BotPlatformGuard.class));
  }

  @Test
  void topLevelMentionStartsOnceAndCreatesProgressCard() {
    ObjectNode payload = objectMapper.createObjectNode().put("workflowId", WORKFLOW_1);
    when(store.conversation(eq("cli_test"), any())).thenReturn(Optional.empty());
    when(store.reserveStart(eq("cli_test"), eq(TASK_ID), any()))
        .thenReturn(new FeishuModels.StartReservation("started", WORKFLOW_1, payload));
    when(gateway.get("/workflows/" + WORKFLOW_1)).thenReturn(snapshot("running"));
    when(cards.render(any(), any())).thenReturn(Map.of("schema", "2.0"));

    coordinator.safelyHandleMessage(message("start-1", "运行", true, null));

    verify(workflowRuns).submitPrepared(any());
    verify(store).markSubmitted(WORKFLOW_1);
    verify(store).enqueueCard(eq("start-card:" + WORKFLOW_1), eq(WORKFLOW_1), any());

    coordinator.safelyHandleMessage(message("ignored", "运行", false, null));
    verify(store, never())
        .reserveStart(eq("cli_test"), eq(TASK_ID), eq(message("ignored", "运行", false, null)));
  }

  @AfterEach
  void close() {
    coordinator.stop();
    coordinator.closeWorkers();
  }

  @Test
  void scheduledEventsAdvanceFilteredCursorThroughBackgroundWorker() {
    var binding =
        new FeishuModels.Binding(WORKFLOW_1, "chat-1", "root-1", null, "active", 0, null, false);
    when(store.pollable("cli_test")).thenReturn(List.of(binding));
    ObjectNode page = objectMapper.createObjectNode().put("nextCursor", 1000);
    page.putArray("events");
    when(gateway.get("/workflows/" + WORKFLOW_1 + "/events/history?after=0&limit=200&view=bot"))
        .thenReturn(page);

    coordinator.start();
    coordinator.scheduleEvents();

    verify(store, timeout(2000))
        .recordEvent(
            "cli_test",
            WORKFLOW_1,
            1000,
            "cursor:" + WORKFLOW_1 + ":1000",
            null,
            null,
            null,
            false);
  }

  @Test
  void topLevelMentionWithBlankSdkReplyContextStillStarts() {
    ObjectNode payload = objectMapper.createObjectNode().put("workflowId", WORKFLOW_1);
    FeishuModels.Message message =
        new FeishuModels.Message(
            "start-blank-context", "chat-1", "group", "user-1", "运行", true, false, "", "", "");
    when(store.conversation("cli_test", message)).thenReturn(Optional.empty());
    when(store.reserveStart("cli_test", TASK_ID, message))
        .thenReturn(new FeishuModels.StartReservation("started", WORKFLOW_1, payload));
    when(gateway.get("/workflows/" + WORKFLOW_1)).thenReturn(snapshot("running"));
    when(cards.render(any(), any())).thenReturn(Map.of("schema", "2.0"));

    coordinator.safelyHandleMessage(message);

    verify(workflowRuns).submitPrepared(any());
    verify(store).markSubmitted(WORKFLOW_1);
    verify(store).enqueueCard(eq("start-card:" + WORKFLOW_1), eq(WORKFLOW_1), any());
  }

  @Test
  void threadMessageUsesStoredDeterministicAssistantMessageId() {
    FeishuModels.Binding binding =
        new FeishuModels.Binding(
            WORKFLOW_2, "chat-1", "root-1", "thread-1", "active", 0, null, false);
    FeishuModels.Message message = message("question-1", "现在进度如何", false, "root-1");
    when(store.conversation("cli_test", message)).thenReturn(Optional.of(binding));
    when(store.registerInbound("cli_test", binding, message))
        .thenReturn(
            new FeishuModels.Inbound(
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
  void repeatedOrExpiredCardActionRefreshesInsteadOfFailing() {
    FeishuModels.Binding binding =
        new FeishuModels.Binding(
            WORKFLOW_3, "chat-1", "root-1", "thread-1", "active", 0, "card-1", false);
    when(store.binding(WORKFLOW_3)).thenReturn(Optional.of(binding));
    when(gateway.post("/workflows/" + WORKFLOW_3 + "/advance/" + GATE_1 + "/confirm", null))
        .thenReturn(objectMapper.createObjectNode());
    when(gateway.get("/workflows/" + WORKFLOW_3)).thenReturn(snapshot("running"));
    when(cards.render(any(), any())).thenReturn(Map.of("schema", "2.0"));

    coordinator.safelyHandleAction(
        new FeishuModels.CardAction(
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
    FeishuModels.Outbox item =
        new FeishuModels.Outbox(
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

  private ObjectNode snapshot(String status) {
    ObjectNode snapshot = objectMapper.createObjectNode();
    snapshot.put("workflowId", WORKFLOW_1);
    snapshot.put("name", "测试任务");
    snapshot.put("status", status);
    snapshot.putObject("progress").put("completed", 0).put("total", 1);
    snapshot.putArray("nodes");
    return snapshot;
  }

  private static FeishuModels.Message message(
      String messageId, String content, boolean mentioned, String rootId) {
    return new FeishuModels.Message(
        messageId,
        "chat-1",
        "group",
        "user-1",
        content,
        mentioned,
        false,
        rootId,
        rootId == null ? null : "thread-1",
        rootId);
  }

  private static final class TestTransport implements FeishuTransport {
    boolean connected;
    RuntimeException sendFailure;

    @Override
    public void start(
        Consumer<FeishuModels.Message> messageHandler,
        Consumer<FeishuModels.CardAction> actionHandler) {
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
    public void testConnection(String appId, String appSecret) {
      if (sendFailure != null) throw sendFailure;
    }

    @Override
    public FeishuModels.SendResult sendText(String chatId, String replyToMessageId, String text) {
      if (sendFailure != null) throw sendFailure;
      return new FeishuModels.SendResult("sent-1");
    }

    @Override
    public FeishuModels.SendResult sendCard(
        String chatId, String replyToMessageId, Map<String, Object> card) {
      return new FeishuModels.SendResult("sent-card-1");
    }

    @Override
    public void updateCard(String messageId, Map<String, Object> card) {}
  }
}
