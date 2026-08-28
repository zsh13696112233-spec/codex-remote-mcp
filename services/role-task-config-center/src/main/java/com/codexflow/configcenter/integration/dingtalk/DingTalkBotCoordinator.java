package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.application.WorkflowRunService;
import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.PreparedRun;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import com.codexflow.configcenter.integration.bot.BotPlatformGuard;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.SmartLifecycle;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 钉钉消息入口、工作流提交、事件同步和 Outbox 发送的应用协调器。 */
@Component
class DingTalkBotCoordinator implements SmartLifecycle {

  private static final Logger LOGGER = LoggerFactory.getLogger(DingTalkBotCoordinator.class);
  private static final Set<String> PROGRESS_EVENTS =
      Set.of(
          "node.started",
          "node.completed",
          "node.failed",
          "node.cancelled",
          "node.timed_out",
          "step.advance.waiting",
          "step.advance.held",
          "step.advance.confirmed",
          "step.advance.resumed",
          "step.advance.timed_out",
          "workflow.completed",
          "workflow.failed",
          "workflow.cancelled");
  private static final Set<String> TERMINAL_EVENTS =
      Set.of("workflow.completed", "workflow.failed", "workflow.cancelled");

  private final DingTalkProperties properties;
  private final DingTalkSettingsStore settings;
  private final DingTalkTransport transport;
  private final DingTalkStore store;
  private final WorkflowRunService workflowRunService;
  private final WorkflowRunStore workflowRunStore;
  private final GatewayClient gateway;
  private final DingTalkProgressCard progressCard;
  private final BotPlatformGuard platformGuard;
  private final ObjectMapper objectMapper;
  private final AtomicBoolean polling = new AtomicBoolean();
  private final AtomicBoolean sending = new AtomicBoolean();
  private final AtomicLong nextEventPollAt = new AtomicLong();
  private final AtomicLong nextOutboxSendAt = new AtomicLong();
  private ExecutorService handlers;
  private volatile boolean running;
  private volatile String connectionStatus = "disabled";

  DingTalkBotCoordinator(
      DingTalkProperties properties,
      DingTalkSettingsStore settings,
      DingTalkTransport transport,
      DingTalkStore store,
      WorkflowRunService workflowRunService,
      WorkflowRunStore workflowRunStore,
      GatewayClient gateway,
      DingTalkProgressCard progressCard,
      ObjectMapper objectMapper,
      BotPlatformGuard platformGuard) {
    this.properties = properties;
    this.settings = settings;
    this.transport = transport;
    this.store = store;
    this.workflowRunService = workflowRunService;
    this.workflowRunStore = workflowRunStore;
    this.gateway = gateway;
    this.progressCard = progressCard;
    this.objectMapper = objectMapper;
    this.platformGuard = platformGuard;
  }

  @Override
  public synchronized void start() {
    if (running) return;
    settings.applyPersisted();
    if (!properties.isEnabled()) {
      connectionStatus = "disabled";
      return;
    }
    try {
      platformGuard.assertCanEnable("dingtalk", true);
      properties.validateEnabledConfiguration();
      store.initialize(properties.getClientId());
      handlers = Executors.newFixedThreadPool(4);
      transport.start(
          message -> handlers.execute(() -> safelyHandleMessage(message)),
          action -> handlers.execute(() -> safelyHandleAction(action)));
      running = true;
      connectionStatus = "connected";
      nextEventPollAt.set(0);
      nextOutboxSendAt.set(0);
      LOGGER.info("钉钉机器人长连接已启动。任务并发上限为 1。");
    } catch (RuntimeException error) {
      running = false;
      connectionStatus = "failed";
      if (handlers != null) handlers.shutdownNow();
      handlers = null;
      LOGGER.warn("钉钉机器人长连接启动失败，请在配置页面检查参数和连接状态。");
    }
  }

  @Override
  public synchronized void stop() {
    running = false;
    try {
      transport.stop();
    } finally {
      if (handlers != null) handlers.shutdownNow();
      handlers = null;
      connectionStatus = properties.isEnabled() ? "disconnected" : "disabled";
    }
  }

  @Override
  public boolean isRunning() {
    return running && transport.connected();
  }

  synchronized void reconfigure() {
    stop();
    start();
  }

  String connectionStatus() {
    if (!properties.isEnabled()) return "disabled";
    if (isRunning()) return "connected";
    return "connected".equals(connectionStatus) ? "disconnected" : connectionStatus;
  }

  @Scheduled(fixedDelay = 250)
  void pollEvents() {
    if (!isRunning() || !due(nextEventPollAt) || !polling.compareAndSet(false, true)) return;
    try {
      for (DingTalkModels.Binding binding : store.pollable(properties.getClientId())) {
        pollBinding(binding);
      }
    } finally {
      polling.set(false);
    }
  }

  @Scheduled(fixedDelay = 250)
  void sendOutbox() {
    if (!isRunning() || !due(nextOutboxSendAt) || !sending.compareAndSet(false, true)) return;
    try {
      for (DingTalkModels.Outbox item : store.claimDue()) {
        deliver(item);
      }
    } finally {
      sending.set(false);
    }
  }

  void safelyHandleMessage(DingTalkModels.Message message) {
    try {
      handleMessage(message);
    } catch (RuntimeException error) {
      LOGGER.warn("处理钉钉消息失败，messageId={}。", message.messageId(), error);
      store.enqueueText(
          "message-error:" + message.messageId(),
          null,
          message.conversationId(),
          message.messageId(),
          "暂时无法处理这条消息，请稍后重试。");
    }
  }

  private void handleMessage(DingTalkModels.Message message) {
    if (!validMessageEnvelope(message)) return;
    if (!"2".equals(message.conversationType()) || message.mentionAll()) return;
    Optional<DingTalkModels.Binding> conversation =
        store.conversation(properties.getClientId(), message);
    if (conversation.isPresent()) {
      if (!hasCardTemplate() && handleTextAdvanceControl(conversation.get(), message)) return;
      forwardToAssistant(conversation.get(), message);
      return;
    }
    if (hasText(message.replyToMessageId())) return;
    if (!message.mentionedBot()) return;
    LOGGER.info("收到钉钉顶层 @ 消息，messageId={}。", message.messageId());
    String command = normalizedCommand(message.content());
    if (!command.isEmpty() && !"运行".equals(command)) {
      Optional<DingTalkModels.Binding> active = store.active(properties.getClientId());
      if (active.isPresent() && active.get().conversationId().equals(message.conversationId())) {
        if (!hasCardTemplate() && handleTextAdvanceControl(active.get(), message)) return;
        forwardToAssistant(active.get(), message);
        return;
      }
      store.enqueueText(
          "top-help:" + message.messageId(),
          null,
          message.conversationId(),
          message.messageId(),
          "请发送“@机器人 运行”启动任务；运行期间可直接 @机器人 提问，也可回复或引用任务消息咨询或控制。");
      return;
    }
    startOrReport(message);
  }

  private void startOrReport(DingTalkModels.Message message) {
    DingTalkModels.StartReservation reservation;
    reservation =
        store.reserveStart(properties.getClientId(), properties.getTaskDefinitionId(), message);
    if (!"started".equals(reservation.outcome())) {
      String workflowId = reservation.workflowId();
      String text =
          "duplicate".equals(reservation.outcome())
              ? "这条启动消息已经处理，任务编号：" + workflowId
              : "机器人当前已有任务运行，任务编号：" + workflowId;
      store.enqueueText(
          "start-result:" + message.messageId(),
          workflowId,
          message.conversationId(),
          message.messageId(),
          text);
      enqueueCurrentProgress(workflowId, "已返回当前任务进度。", "start-current:" + message.messageId());
      return;
    }

    try {
      workflowRunService.submitPrepared(
          new PreparedRun(reservation.workflowId(), reservation.payload()));
      store.markSubmitted(reservation.workflowId());
      enqueueCurrentProgress(
          reservation.workflowId(),
          "任务已启动。运行期间可直接 @机器人 提问，也可回复或引用本消息或进度消息咨询或控制。",
          "start-card:" + reservation.workflowId());
    } catch (RuntimeException error) {
      store.markSubmissionFailed(
          properties.getClientId(), reservation.workflowId(), "任务启动失败，请稍后重新 @机器人运行。");
    }
  }

  private void forwardToAssistant(DingTalkModels.Binding binding, DingTalkModels.Message message) {
    String text = normalizedCommand(message.content());
    if (text.isBlank()) {
      store.enqueueText(
          "empty-assistant:" + message.messageId(),
          binding.workflowId(),
          message.conversationId(),
          message.messageId(),
          "请在回复中输入想询问的状态或控制指令。");
      return;
    }
    if (text.length() > 4000) {
      store.enqueueText(
          "long-assistant:" + message.messageId(),
          binding.workflowId(),
          message.conversationId(),
          message.messageId(),
          "消息过长，请缩短到 4000 个字符以内。");
      return;
    }

    DingTalkModels.Inbound inbound =
        store.registerInbound(properties.getClientId(), binding, message);
    if (!"accepted".equals(inbound.status())) return;

    boolean restartReserved = false;
    if ("确认执行".equals(text)) {
      JsonNode snapshot;
      try {
        snapshot = gateway.get("/workflows/" + binding.workflowId());
      } catch (RuntimeException error) {
        store.markInboundFinished(binding.workflowId(), inbound.workflowMessageId(), true);
        store.enqueueText(
            "confirmation-status-failed:" + message.messageId(),
            binding.workflowId(),
            message.conversationId(),
            message.messageId(),
            "暂时无法确认任务状态，请稍后重试。");
        return;
      }
      if ("restart_from".equals(snapshot.path("pendingControl").path("type").asText())) {
        Optional<String> busy =
            store.acquireForRestart(properties.getClientId(), binding.workflowId());
        if (busy.isPresent()) {
          store.markInboundFinished(binding.workflowId(), inbound.workflowMessageId(), false);
          store.enqueueText(
              "restart-busy:" + message.messageId(),
              binding.workflowId(),
              message.conversationId(),
              message.messageId(),
              "当前有其他任务正在运行，暂不能返工。当前任务编号：" + busy.get());
          return;
        }
        restartReserved = true;
      }
    }

    ObjectNode request = objectMapper.createObjectNode();
    request.put("messageId", inbound.workflowMessageId());
    request.put("text", text);
    try {
      gateway.post("/workflows/" + binding.workflowId() + "/messages", request);
    } catch (RuntimeException error) {
      if (restartReserved && !workflowBecameActive(binding.workflowId())) {
        store.releaseRestartReservation(properties.getClientId(), binding.workflowId());
      }
      store.markInboundFinished(binding.workflowId(), inbound.workflowMessageId(), true);
      store.enqueueText(
          "assistant-submit-failed:" + message.messageId(),
          binding.workflowId(),
          message.conversationId(),
          message.messageId(),
          "任务助手暂时无法接收消息，请稍后重试。");
    }
  }

  private boolean workflowBecameActive(String workflowId) {
    try {
      String status = gateway.get("/workflows/" + workflowId).path("status").asText();
      return Set.of("queued", "running", "cancelling").contains(status);
    } catch (RuntimeException ignored) {
      return false;
    }
  }

  private boolean handleTextAdvanceControl(
      DingTalkModels.Binding binding, DingTalkModels.Message message) {
    String command = normalizedCommand(message.content());
    String action =
        switch (command) {
          case "暂停", "暂停，暂不进入下一步" -> "hold";
          case "继续", "立即进入下一步", "继续进入下一步" -> "confirm";
          default -> null;
        };
    if (action == null) return false;

    try {
      JsonNode snapshot = gateway.get("/workflows/" + binding.workflowId());
      String gateId = snapshot.path("pendingAdvance").path("gateId").asText();
      if (!isGateId(gateId)) {
        store.enqueueText(
            "advance-text-none:" + message.messageId(),
            binding.workflowId(),
            message.conversationId(),
            message.messageId(),
            "当前没有等待确认的步骤，无需执行这个操作。");
        return true;
      }
      gateway.post(
          "/workflows/" + binding.workflowId() + "/advance/" + gateId + "/" + action, null);
      String notice = "hold".equals(action) ? "已暂停，将等待手动继续。" : "已进入下一步。";
      enqueueCurrentProgress(
          binding.workflowId(), notice, "advance-text-result:" + message.messageId());
    } catch (GatewayFailure error) {
      if (error.getStatusCode() == 409 || error.getStatusCode() == 404) {
        enqueueCurrentProgress(
            binding.workflowId(),
            "操作已生效或等待已经结束；如果倒计时到期，任务已自动继续。",
            "advance-text-result:" + message.messageId());
      } else {
        enqueueAdvanceTextFailure(binding, message);
      }
    } catch (RuntimeException error) {
      enqueueAdvanceTextFailure(binding, message);
    }
    return true;
  }

  private void enqueueAdvanceTextFailure(
      DingTalkModels.Binding binding, DingTalkModels.Message message) {
    store.enqueueText(
        "advance-text-error:" + message.messageId(),
        binding.workflowId(),
        message.conversationId(),
        message.messageId(),
        "暂时无法执行步骤流转操作，请稍后重试。");
  }

  void safelyHandleAction(DingTalkModels.CardAction action) {
    try {
      handleAction(action);
    } catch (RuntimeException error) {
      LOGGER.warn("处理钉钉卡片动作失败，cardInstanceId={}。", action.cardInstanceId(), error);
    }
  }

  private void handleAction(DingTalkModels.CardAction event) {
    String action = stringValue(event.value(), "action", event.actionId());
    String workflowId = stringValue(event.value(), "workflowId", null);
    String gateId = stringValue(event.value(), "gateId", null);
    if (!isUuid(workflowId) || !isGateId(gateId)) return;
    Optional<DingTalkModels.Binding> binding = store.binding(workflowId);
    if (binding.isEmpty()) return;
    if (hasText(event.conversationId())
        && !binding.get().conversationId().equals(event.conversationId())) return;
    if (hasText(event.cardInstanceId())
        && hasText(binding.get().progressCardInstanceId())
        && !binding.get().progressCardInstanceId().equals(event.cardInstanceId())) return;
    String notice;
    try {
      if ("advance_hold".equals(action)) {
        gateway.post("/workflows/" + workflowId + "/advance/" + gateId + "/hold", null);
        notice = "已暂停，将等待手动继续。";
      } else if ("advance_confirm".equals(action)) {
        gateway.post("/workflows/" + workflowId + "/advance/" + gateId + "/confirm", null);
        notice = "已进入下一步。";
      } else {
        return;
      }
    } catch (GatewayFailure error) {
      if (error.getStatusCode() != 409 && error.getStatusCode() != 404) throw error;
      notice = "操作已生效或等待已结束；如果倒计时已经到期，任务已自动继续。";
    }
    enqueueCurrentProgress(
        workflowId, notice, "card-action:" + workflowId + ":" + gateId + ":" + action);
  }

  private void pollBinding(DingTalkModels.Binding binding) {
    if ("submitting".equals(binding.status()) && !recoverSubmission(binding)) return;
    long cursor = binding.eventCursor();
    while (true) {
      JsonNode result;
      try {
        result =
            gateway.get(
                "/workflows/"
                    + binding.workflowId()
                    + "/events/history?after="
                    + cursor
                    + "&limit=200");
      } catch (RuntimeException error) {
        LOGGER.debug("读取钉钉任务事件失败，workflowId={}。", binding.workflowId());
        return;
      }
      JsonNode events = result.path("events");
      if (!events.isArray() || events.isEmpty()) return;
      for (JsonNode event : events) {
        long sequence = event.path("sequence").asLong();
        if (!consumeEvent(binding, event, sequence)) return;
        cursor = Math.max(cursor, sequence);
      }
      if (events.size() < 200) return;
    }
  }

  private boolean recoverSubmission(DingTalkModels.Binding binding) {
    try {
      gateway.get("/workflows/" + binding.workflowId());
      store.markSubmitted(binding.workflowId());
      return true;
    } catch (GatewayFailure error) {
      if (error.getStatusCode() != 404) return false;
    }
    try {
      workflowRunService.submitPrepared(workflowRunStore.getPrepared(binding.workflowId()));
      store.markSubmitted(binding.workflowId());
      enqueueCurrentProgress(
          binding.workflowId(), "服务恢复后已继续提交任务。", "recovered-card:" + binding.workflowId());
      return true;
    } catch (RuntimeException error) {
      store.markSubmissionFailed(
          properties.getClientId(), binding.workflowId(), "任务启动失败，请稍后重新 @机器人运行。");
      return false;
    }
  }

  private boolean consumeEvent(DingTalkModels.Binding binding, JsonNode event, long sequence) {
    String type = event.path("type").asText();
    JsonNode payload = event.path("payload");
    String messageKind = null;
    String replyTo = null;
    JsonNode outgoing = null;
    String workflowMessageId = null;
    boolean assistantFailed = false;

    if ("chat.assistant.completed".equals(type) || "chat.message.failed".equals(type)) {
      workflowMessageId = payload.path("messageId").asText();
      Optional<DingTalkModels.Inbound> inbound =
          store.inbound(binding.workflowId(), workflowMessageId);
      if (inbound.isPresent()) {
        replyTo = inbound.get().messageId();
        assistantFailed = "chat.message.failed".equals(type);
        String text =
            assistantFailed ? "任务助手暂时无法完成回复，请稍后重试。" : payload.path("text").asText("任务助手已完成处理。");
        if (!assistantFailed) {
          JsonNode card = null;
          if (hasCardTemplate()) {
            try {
              JsonNode snapshot = gateway.get("/workflows/" + binding.workflowId());
              card = objectMapper.valueToTree(progressCard.render(snapshot, "任务助手已回复。", text));
            } catch (RuntimeException error) {
              LOGGER.debug("将钉钉任务助手回复同步到进度卡失败，workflowId={}。", binding.workflowId());
            }
          }
          store.recordAssistantCompleted(
              binding.workflowId(),
              sequence,
              "workflow-event:" + binding.workflowId() + ":" + sequence,
              replyTo,
              objectMapper.createObjectNode().put("text", text),
              text,
              "assistant-card:" + binding.workflowId() + ":" + sequence,
              card);
          finishAssistantEvent(binding, workflowMessageId, false);
          return true;
        }
        messageKind = "text";
        outgoing = objectMapper.createObjectNode().put("text", text);
      }
    } else if (PROGRESS_EVENTS.contains(type)) {
      try {
        JsonNode snapshot = gateway.get("/workflows/" + binding.workflowId());
        if (hasCardTemplate()) {
          Map<String, Object> card =
              progressCard.render(
                  snapshot,
                  eventNotice(type),
                  store.latestAssistantReply(binding.workflowId()).orElse(null));
          outgoing = objectMapper.valueToTree(Map.of("card", card));
          messageKind = binding.progressCardInstanceId() == null ? "card" : "card_update";
        } else {
          outgoing =
              objectMapper
                  .createObjectNode()
                  .put("title", "任务进度")
                  .put("text", progressCard.renderMarkdown(snapshot, eventNotice(type)));
          messageKind = "markdown";
        }
        replyTo = binding.rootMessageId();
      } catch (RuntimeException error) {
        LOGGER.debug("刷新钉钉任务进度消息失败，workflowId={}。", binding.workflowId());
        return false;
      }
    }

    store.recordEvent(
        properties.getClientId(),
        binding.workflowId(),
        sequence,
        "workflow-event:" + binding.workflowId() + ":" + sequence,
        messageKind,
        replyTo,
        outgoing,
        TERMINAL_EVENTS.contains(type));
    if (workflowMessageId != null) {
      finishAssistantEvent(binding, workflowMessageId, assistantFailed);
    }
    return true;
  }

  private void finishAssistantEvent(
      DingTalkModels.Binding binding, String workflowMessageId, boolean assistantFailed) {
    store.markInboundFinished(binding.workflowId(), workflowMessageId, assistantFailed);
    try {
      String status = gateway.get("/workflows/" + binding.workflowId()).path("status").asText();
      store.reconcileRuntimeStatus(properties.getClientId(), binding.workflowId(), status);
    } catch (RuntimeException ignored) {
      // 下一轮事件轮询会继续校正状态；不影响已经持久化的助手回复。
    }
  }

  private void enqueueCurrentProgress(String workflowId, String notice, String dedupKey) {
    try {
      JsonNode snapshot = gateway.get("/workflows/" + workflowId);
      if (hasCardTemplate()) {
        store.enqueueCard(
            dedupKey,
            workflowId,
            objectMapper.valueToTree(
                progressCard.render(
                    snapshot, notice, store.latestAssistantReply(workflowId).orElse(null))));
      } else {
        store.enqueueProgressMarkdown(
            dedupKey, workflowId, "任务进度", progressCard.renderMarkdown(snapshot, notice));
      }
    } catch (RuntimeException error) {
      LOGGER.debug("生成钉钉任务进度消息失败，workflowId={}。", workflowId);
    }
  }

  private boolean hasCardTemplate() {
    return !properties.getCardTemplateId().isBlank();
  }

  @SuppressWarnings("unchecked")
  void deliver(DingTalkModels.Outbox item) {
    try {
      DingTalkModels.SendResult result;
      if ("text".equals(item.messageKind())) {
        result =
            transport.sendText(
                item.conversationId(),
                item.replyToMessageId(),
                item.payload().path("text").asText());
      } else if ("markdown".equals(item.messageKind())) {
        result =
            transport.sendMarkdown(
                item.conversationId(),
                item.replyToMessageId(),
                item.payload().path("title").asText("任务进度"),
                item.payload().path("text").asText());
      } else {
        if (!store.isLatestCardOutbox(item.id(), item.workflowId())) {
          store.markOutboxSuperseded(item.id());
          return;
        }
        Map<String, Object> card =
            objectMapper.convertValue(item.payload().path("card"), Map.class);
        DingTalkModels.Binding binding =
            item.workflowId() == null ? null : store.binding(item.workflowId()).orElse(null);
        if (binding != null && binding.progressCardInstanceId() != null) {
          transport.updateCard(binding.progressCardInstanceId(), card);
          result = new DingTalkModels.SendResult(binding.progressCardInstanceId());
        } else {
          result = transport.sendCard(item.conversationId(), item.replyToMessageId(), card);
        }
      }
      store.markOutboxSent(item.id(), result.messageId());
    } catch (RuntimeException error) {
      store.markOutboxFailed(item.id(), error);
    }
  }

  private static String normalizedCommand(String value) {
    return value == null ? "" : value.trim();
  }

  private static boolean validMessageEnvelope(DingTalkModels.Message message) {
    return hasLength(message.messageId(), 256)
        && hasLength(message.conversationId(), 256)
        && hasLength(message.senderUserId(), 256)
        && optionalLength(message.replyToMessageId(), 256);
  }

  private static boolean hasLength(String value, int max) {
    return value != null && !value.isBlank() && value.length() <= max;
  }

  private static boolean optionalLength(String value, int max) {
    return value == null || value.length() <= max;
  }

  private static boolean hasText(String value) {
    return value != null && !value.isBlank();
  }

  private static boolean isUuid(String value) {
    if (value == null) return false;
    try {
      UUID.fromString(value);
      return true;
    } catch (IllegalArgumentException error) {
      return false;
    }
  }

  private static boolean isGateId(String value) {
    return value != null && (value.matches("[0-9a-fA-F]{32}") || isUuid(value));
  }

  private static String stringValue(Map<String, Object> values, String key, String fallback) {
    Object value = values.get(key);
    return value == null || value.toString().isBlank() ? fallback : value.toString();
  }

  private static String eventNotice(String type) {
    return switch (type) {
      case "step.advance.timed_out" -> "30 秒等待已结束，任务已自动继续。";
      case "workflow.completed" -> "任务已完成。";
      case "workflow.failed" -> "任务执行失败。";
      case "workflow.cancelled" -> "任务已停止。";
      default -> "任务进度已更新。";
    };
  }

  private boolean due(AtomicLong nextAt) {
    long now = System.currentTimeMillis();
    long current = nextAt.get();
    return now >= current
        && nextAt.compareAndSet(current, now + properties.getEventPollIntervalMs());
  }
}
