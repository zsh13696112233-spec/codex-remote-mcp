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
    verify(gateway, never()).post(any(), any());
  }

  @Test
  void confirmationMustBelongToSender() {
    var snapshot = json.createObjectNode().put("status", "running");
    snapshot.putObject("pendingControl").put("actionId", "action").put("actorId", "app:user-1");
    when(gateway.get("/workflows/" + ID)).thenReturn(snapshot);
    bot.safelyHandleMessage(message(ID + " 确认执行"));
    verify(gateway, never()).post(any(), any());
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
  void dingtalkProgressEventsAdvanceCursorWithoutPush() {
    var binding = new DingTalkModels.Binding(ID, "group", "root", "active", 0, null, false);
    var event = json.createObjectNode().put("type", "node.completed");
    ReflectionTestUtils.invokeMethod(bot, "consumeEvent", binding, event, 1L);
    verify(store)
        .recordEvent(
            eq("app"), eq(ID), eq(1L), anyString(), isNull(), isNull(), isNull(), eq(false));
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
