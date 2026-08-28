package com.codexflow.configcenter.integration.feishu;

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

/** 飞书消息入口、工作流提交、事件同步和 Outbox 发送的应用协调器。 */
@Component
class FeishuBotCoordinator implements SmartLifecycle {

  private static final Logger LOGGER = LoggerFactory.getLogger(FeishuBotCoordinator.class);
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

  private final FeishuProperties properties;
  private final FeishuSettingsStore settings;
  private final FeishuTransport transport;
  private final FeishuStore store;
  private final WorkflowRunService workflowRunService;
  private final WorkflowRunStore workflowRunStore;
  private final GatewayClient gateway;
  private final FeishuProgressCard progressCard;
  private final BotPlatformGuard platformGuard;
  private final ObjectMapper objectMapper;
  private final AtomicBoolean polling = new AtomicBoolean();
  private final AtomicBoolean sending = new AtomicBoolean();
  private final AtomicLong nextEventPollAt = new AtomicLong();
  private final AtomicLong nextOutboxSendAt = new AtomicLong();
  private ExecutorService handlers;
  private volatile boolean running;
  private volatile String connectionStatus = "disabled";

  FeishuBotCoordinator(
      FeishuProperties properties,
      FeishuSettingsStore settings,
      FeishuTransport transport,
      FeishuStore store,
      WorkflowRunService workflowRunService,
      WorkflowRunStore workflowRunStore,
      GatewayClient gateway,
      FeishuProgressCard progressCard,
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
      platformGuard.assertCanEnable("feishu", true);
      properties.validateEnabledConfiguration();
      store.initialize(properties.getAppId());
      handlers = Executors.newFixedThreadPool(4);
      transport.start(
          message -> handlers.execute(() -> safelyHandleMessage(message)),
          action -> handlers.execute(() -> safelyHandleAction(action)));
      running = true;
      connectionStatus = "connected";
      nextEventPollAt.set(0);
      nextOutboxSendAt.set(0);
      LOGGER.info("飞书机器人长连接已启动。任务并发上限为 1。");
    } catch (RuntimeException error) {
      running = false;
      connectionStatus = "failed";
      if (handlers != null) handlers.shutdownNow();
      handlers = null;
      LOGGER.warn("飞书机器人长连接启动失败，请在配置页面检查参数和连接状态。");
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
      for (FeishuModels.Binding binding : store.pollable(properties.getAppId())) {
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
      for (FeishuModels.Outbox item : store.claimDue()) {
        deliver(item);
      }
    } finally {
      sending.set(false);
    }
  }

  void safelyHandleMessage(FeishuModels.Message message) {
    try {
      handleMessage(message);
    } catch (RuntimeException error) {
      LOGGER.warn("处理飞书消息失败，messageId={}。", message.messageId(), error);
      store.enqueueText(
          "message-error:" + message.messageId(),
          null,
          message.chatId(),
          message.messageId(),
          "暂时无法处理这条消息，请稍后重试。");
    }
  }

  private void handleMessage(FeishuModels.Message message) {
    if (!validMessageEnvelope(message)) return;
    if (!"group".equalsIgnoreCase(message.chatType()) || message.mentionAll()) return;
    Optional<FeishuModels.Binding> conversation =
        store.conversation(properties.getAppId(), message);
    if (conversation.isPresent()) {
      forwardToAssistant(conversation.get(), message);
      return;
    }
    if (hasText(message.rootId())
        || hasText(message.threadId())
        || hasText(message.replyToMessageId())) return;
    if (!message.mentionedBot()) return;
    LOGGER.info("收到飞书顶层 @ 消息，messageId={}。", message.messageId());
    String command = normalizedCommand(message.content());
    if (!command.isEmpty() && !"运行".equals(command)) {
      store.enqueueText(
          "top-help:" + message.messageId(),
          null,
          message.chatId(),
          message.messageId(),
          "请发送“@机器人 运行”启动任务；任务启动后可在话题中咨询或控制。");
      return;
    }
    startOrReport(message);
  }

  private void startOrReport(FeishuModels.Message message) {
    FeishuModels.StartReservation reservation;
    reservation =
        store.reserveStart(properties.getAppId(), properties.getTaskDefinitionId(), message);
    if (!"started".equals(reservation.outcome())) {
      String workflowId = reservation.workflowId();
      String text =
          "duplicate".equals(reservation.outcome())
              ? "这条启动消息已经处理，任务编号：" + workflowId
              : "机器人当前已有任务运行，任务编号：" + workflowId;
      store.enqueueText(
          "start-result:" + message.messageId(),
          workflowId,
          message.chatId(),
          message.messageId(),
          text);
      enqueueCurrentCard(workflowId, "已返回当前任务进度。", "start-current:" + message.messageId());
      return;
    }

    try {
      workflowRunService.submitPrepared(
          new PreparedRun(reservation.workflowId(), reservation.payload()));
      store.markSubmitted(reservation.workflowId());
      enqueueCurrentCard(
          reservation.workflowId(),
          "任务已启动。后续可在本话题中向任务助手询问状态或发出控制指令。",
          "start-card:" + reservation.workflowId());
    } catch (RuntimeException error) {
      store.markSubmissionFailed(
          properties.getAppId(), reservation.workflowId(), "任务启动失败，请稍后重新 @机器人运行。");
    }
  }

  private void forwardToAssistant(FeishuModels.Binding binding, FeishuModels.Message message) {
    String text = normalizedCommand(message.content());
    if (text.isBlank()) {
      store.enqueueText(
          "empty-assistant:" + message.messageId(),
          binding.workflowId(),
          message.chatId(),
          message.messageId(),
          "请在话题中输入想询问的状态或控制指令。");
      return;
    }
    if (text.length() > 4000) {
      store.enqueueText(
          "long-assistant:" + message.messageId(),
          binding.workflowId(),
          message.chatId(),
          message.messageId(),
          "消息过长，请缩短到 4000 个字符以内。");
      return;
    }

    FeishuModels.Inbound inbound = store.registerInbound(properties.getAppId(), binding, message);
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
            message.chatId(),
            message.messageId(),
            "暂时无法确认任务状态，请稍后重试。");
        return;
      }
      if ("restart_from".equals(snapshot.path("pendingControl").path("type").asText())) {
        Optional<String> busy =
            store.acquireForRestart(properties.getAppId(), binding.workflowId());
        if (busy.isPresent()) {
          store.markInboundFinished(binding.workflowId(), inbound.workflowMessageId(), false);
          store.enqueueText(
              "restart-busy:" + message.messageId(),
              binding.workflowId(),
              message.chatId(),
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
        store.releaseRestartReservation(properties.getAppId(), binding.workflowId());
      }
      store.markInboundFinished(binding.workflowId(), inbound.workflowMessageId(), true);
      store.enqueueText(
          "assistant-submit-failed:" + message.messageId(),
          binding.workflowId(),
          message.chatId(),
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

  void safelyHandleAction(FeishuModels.CardAction action) {
    try {
      handleAction(action);
    } catch (RuntimeException error) {
      LOGGER.warn("处理飞书卡片动作失败，messageId={}。", action.messageId(), error);
    }
  }

  private void handleAction(FeishuModels.CardAction event) {
    String action = stringValue(event.value(), "action", event.actionId());
    String workflowId = stringValue(event.value(), "workflowId", null);
    String gateId = stringValue(event.value(), "gateId", null);
    if (!isUuid(workflowId) || !isGateId(gateId)) return;
    Optional<FeishuModels.Binding> binding = store.binding(workflowId);
    if (binding.isEmpty() || !binding.get().chatId().equals(event.chatId())) return;
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
    enqueueCurrentCard(
        workflowId, notice, "card-action:" + workflowId + ":" + gateId + ":" + action);
  }

  private void pollBinding(FeishuModels.Binding binding) {
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
        LOGGER.debug("读取飞书任务事件失败，workflowId={}。", binding.workflowId());
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

  private boolean recoverSubmission(FeishuModels.Binding binding) {
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
      enqueueCurrentCard(
          binding.workflowId(), "服务恢复后已继续提交任务。", "recovered-card:" + binding.workflowId());
      return true;
    } catch (RuntimeException error) {
      store.markSubmissionFailed(
          properties.getAppId(), binding.workflowId(), "任务启动失败，请稍后重新 @机器人运行。");
      return false;
    }
  }

  private boolean consumeEvent(FeishuModels.Binding binding, JsonNode event, long sequence) {
    String type = event.path("type").asText();
    JsonNode payload = event.path("payload");
    String messageKind = null;
    String replyTo = null;
    JsonNode outgoing = null;
    String workflowMessageId = null;
    boolean assistantFailed = false;

    if ("chat.assistant.completed".equals(type) || "chat.message.failed".equals(type)) {
      workflowMessageId = payload.path("messageId").asText();
      Optional<FeishuModels.Inbound> inbound =
          store.inbound(binding.workflowId(), workflowMessageId);
      if (inbound.isPresent()) {
        replyTo = inbound.get().messageId();
        assistantFailed = "chat.message.failed".equals(type);
        String text =
            assistantFailed ? "任务助手暂时无法完成回复，请稍后重试。" : payload.path("text").asText("任务助手已完成处理。");
        messageKind = "text";
        outgoing = objectMapper.createObjectNode().put("text", text);
      }
    } else if (PROGRESS_EVENTS.contains(type)) {
      try {
        JsonNode snapshot = gateway.get("/workflows/" + binding.workflowId());
        Map<String, Object> card = progressCard.render(snapshot, eventNotice(type));
        outgoing = objectMapper.valueToTree(Map.of("card", card));
        messageKind = binding.progressMessageId() == null ? "card" : "card_update";
        replyTo = binding.rootMessageId();
      } catch (RuntimeException error) {
        LOGGER.debug("刷新飞书任务卡片失败，workflowId={}。", binding.workflowId());
        return false;
      }
    }

    store.recordEvent(
        properties.getAppId(),
        binding.workflowId(),
        sequence,
        "workflow-event:" + binding.workflowId() + ":" + sequence,
        messageKind,
        replyTo,
        outgoing,
        TERMINAL_EVENTS.contains(type));
    if (workflowMessageId != null) {
      store.markInboundFinished(binding.workflowId(), workflowMessageId, assistantFailed);
      try {
        String status = gateway.get("/workflows/" + binding.workflowId()).path("status").asText();
        store.reconcileRuntimeStatus(properties.getAppId(), binding.workflowId(), status);
      } catch (RuntimeException ignored) {
        // 下一轮事件轮询会继续校正状态；不影响已经持久化的助手回复。
      }
    }
    return true;
  }

  private void enqueueCurrentCard(String workflowId, String notice, String dedupKey) {
    try {
      JsonNode snapshot = gateway.get("/workflows/" + workflowId);
      store.enqueueCard(
          dedupKey, workflowId, objectMapper.valueToTree(progressCard.render(snapshot, notice)));
    } catch (RuntimeException error) {
      LOGGER.debug("生成飞书任务卡片失败，workflowId={}。", workflowId);
    }
  }

  @SuppressWarnings("unchecked")
  void deliver(FeishuModels.Outbox item) {
    try {
      FeishuModels.SendResult result;
      if ("text".equals(item.messageKind())) {
        result =
            transport.sendText(
                item.chatId(), item.replyToMessageId(), item.payload().path("text").asText());
      } else {
        Map<String, Object> card =
            objectMapper.convertValue(item.payload().path("card"), Map.class);
        FeishuModels.Binding binding =
            item.workflowId() == null ? null : store.binding(item.workflowId()).orElse(null);
        if (binding != null && binding.progressMessageId() != null) {
          transport.updateCard(binding.progressMessageId(), card);
          result = new FeishuModels.SendResult(binding.progressMessageId());
        } else {
          result = transport.sendCard(item.chatId(), item.replyToMessageId(), card);
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

  private static boolean validMessageEnvelope(FeishuModels.Message message) {
    return hasLength(message.messageId(), 256)
        && hasLength(message.chatId(), 256)
        && hasLength(message.senderOpenId(), 256)
        && optionalLength(message.rootId(), 256)
        && optionalLength(message.threadId(), 256)
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
