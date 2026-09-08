package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.application.BoundedWork;
import com.codexflow.configcenter.application.WorkflowRunService;
import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.PreparedRun;
import com.codexflow.configcenter.domain.WorkflowRunStore;
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

  @org.springframework.beans.factory.annotation.Value(
      "${codex.monitor.base-url:http://127.0.0.1:8090/}")
  private String monitorUrl = "http://127.0.0.1:8090/";

  private final DingTalkProperties properties;
  private final DingTalkSettingsStore settings;
  private final DingTalkTransport transport;
  private final DingTalkStore store;
  private final WorkflowRunService workflowRunService;
  private final WorkflowRunStore workflowRunStore;
  private final GatewayClient gateway;
  private final DingTalkProgressCard progressCard;
  private final ObjectMapper objectMapper;
  private final BoundedWork eventWorkers = new BoundedWork("dingtalk-events", 4, 4);
  private final BoundedWork outboxWorker = new BoundedWork("dingtalk-outbox", 1, 1);
  private int pollOffset;
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
      ObjectMapper objectMapper) {
    this.properties = properties;
    this.settings = settings;
    this.transport = transport;
    this.store = store;
    this.workflowRunService = workflowRunService;
    this.workflowRunStore = workflowRunStore;
    this.gateway = gateway;
    this.progressCard = progressCard;
    this.objectMapper = objectMapper;
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
      LOGGER.info("钉钉机器人长连接已启动。不同任务绑定可以并行运行。");
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
  void scheduleEvents() {
    if (!isRunning() || !due(nextEventPollAt)) return;
    var bindings = store.pollable(properties.getClientId());
    int size = bindings.size();
    for (int visited = 0; visited < size && eventWorkers.available() > 0; visited++) {
      var binding = bindings.get(Math.floorMod(pollOffset++, size));
      eventWorkers.submit(
          binding.workflowId(),
          () -> {
            if (isRunning()) pollBinding(binding);
          });
    }
  }

  @Scheduled(fixedDelay = 250)
  void scheduleOutbox() {
    outboxWorker.submit("outbox", this::sendOutbox);
  }

  @jakarta.annotation.PreDestroy
  void closeWorkers() {
    eventWorkers.close();
    outboxWorker.close();
  }

  void sendOutbox() {
    if (!isRunning() || !due(nextOutboxSendAt) || !sending.compareAndSet(false, true)) return;
    try {
      int remaining = 50;
      while (remaining > 0) {
        var batch = store.claimDue(remaining);
        if (batch.isEmpty()) break;
        for (DingTalkModels.Outbox item : batch) {
          deliver(item);
        }
        remaining -= batch.size();
      }
    } finally {
      sending.set(false);
    }
  }

  void safelyHandleMessage(DingTalkModels.Message message) {
    try {
      handleMessage(message);
    } catch (RuntimeException error) {
      if (error instanceof com.codexflow.configcenter.domain.NotFoundFailure
          || error instanceof com.codexflow.configcenter.domain.ConflictFailure
          || error instanceof IllegalArgumentException) {
        reply(message, null, error.getMessage());
        return;
      }
      LOGGER.warn("处理钉钉消息失败，messageId={}。", message.messageId());
      reply(message, null, "暂时无法处理这条消息，请检查工作流编号后重试。");
    }
  }

  private void handleMessage(DingTalkModels.Message original) {
    if (!validMessageEnvelope(original)) return;
    boolean group = "2".equals(original.conversationType());
    if (group && (original.mentionAll() || !original.mentionedBot())) return;
    if (group) store.discoverGroup(properties.getClientId(), original);
    DingTalkModels.Message message = original.withContent(normalizedCommand(original.content()));
    if (group && original.mentionedBot()) {
      // 富文本的 @名称与编号可能直接相连；不要求名称后存在空格。
      message =
          message.withContent(
              message
                  .content()
                  .replaceFirst(
                      "^@[^\\r\\n]+?\\s*(?=[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?:\\s|$))",
                      ""));
    }
    String command = message.content();
    String[] parts = command.split("\\s+", 2);
    String explicit = parts.length > 0 && isUuid(parts[0]) ? parts[0].toLowerCase() : null;
    Optional<DingTalkModels.Binding> quoted = store.conversation(properties.getClientId(), message);
    if (explicit != null && quoted.isPresent() && !explicit.equals(quoted.get().workflowId())) {
      reply(message, null, "工作流编号与引用消息不一致，请确认后重新发送。");
      return;
    }
    String workflowId =
        explicit != null ? explicit : quoted.map(DingTalkModels.Binding::workflowId).orElse(null);
    if (workflowId == null) {
      if (hasText(message.replyToMessageId())
          || hasText(message.quotedText())
          || command.isBlank()
          || !message.imageCodes().isEmpty()) {
        LOGGER.info(
            "钉钉消息未定位任务：正文长度={}，图片数={}，开头含@={}，包含标准编号={}，存在引用={}，特殊分隔字符={}。",
            command.length(),
            message.imageCodes().size(),
            command.startsWith("@"),
            java.util.regex.Pattern.compile("[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
                .matcher(command)
                .find(),
            hasText(message.replyToMessageId()) || hasText(message.quotedText()),
            original
                .content()
                .codePoints()
                .filter(
                    c ->
                        Character.isSpaceChar(c)
                            || Character.isWhitespace(c)
                            || Character.getType(c) == Character.FORMAT)
                .distinct()
                .limit(12)
                .mapToObj(c -> String.format("U+%04X", c))
                .toList());
        reply(message, null, "启动请发送完整任务定义名称；提问请带工作流编号和需求，或引用任务消息。");
        return;
      }
      startOrReport(message);
      return;
    }
    JsonNode snapshot = gateway.get("/workflows/" + workflowId);
    store.ensureConversation(
        properties.getClientId(),
        workflowId,
        workflowRunStore.taskDefinitionId(workflowId),
        message,
        snapshot.path("status").asText());
    DingTalkModels.Binding route = store.route(workflowId, message);
    message = message.withContent(explicit == null ? command : parts.length == 2 ? parts[1] : "");
    if (message.imageCodes().isEmpty() && handleTextAdvanceControl(route, message)) return;
    forwardToAssistant(route, message);
  }

  private void reply(DingTalkModels.Message message, String workflowId, String text) {
    store.enqueueReply("reply:" + message.messageId(), workflowId, message, text);
  }

  private void startOrReport(DingTalkModels.Message message) {
    DingTalkModels.StartReservation reservation =
        store.reserveStart(properties.getClientId(), message);
    String workflowId = reservation.workflowId();
    if (!"started".equals(reservation.outcome())) {
      store.ensureConversation(
          properties.getClientId(),
          workflowId,
          workflowRunStore.taskDefinitionId(workflowId),
          message,
          "running");
      reply(
          message,
          workflowId,
          "duplicate".equals(reservation.outcome()) ? "这条启动消息已经处理。" : "任务正在运行，本次未启动。");
      return;
    }
    try {
      workflowRunService.submitPrepared(new PreparedRun(workflowId, reservation.payload()));
      store.markSubmitted(workflowId);
      reply(
          message,
          workflowId,
          "任务已启动："
              + message.content()
              + "。后续请发送工作流编号加问题，或引用本消息。\n查看任务："
              + monitorUrl
              + (monitorUrl.contains("?") ? "&" : "?")
              + "workflowId="
              + workflowId);
    } catch (RuntimeException error) {
      reply(message, workflowId, "任务暂未成功启动，请在运行记录中查看状态。");
    }
  }

  private void forwardToAssistant(DingTalkModels.Binding binding, DingTalkModels.Message message) {
    String text = message.content();
    String workflowId = binding.workflowId();
    if (text.length() > 4000 || message.imageCodes().size() > 5) {
      reply(message, workflowId, "每条消息最多 4000 个字符和 5 张图片。");
      return;
    }
    DingTalkModels.Inbound inbound =
        store.registerInbound(properties.getClientId(), binding, message);
    if (!"accepted".equals(inbound.status())) return;
    boolean restartReserved = false;
    try {
      var imageIds =
          new java.util.ArrayList<>(
              store.messageImages(message.messageId(), workflowId, message.conversationId()));
      if (imageIds.isEmpty()) {
        imageIds.addAll(store.referencedImages(message, workflowId));
        int bytes = 0;
        for (String code : message.imageCodes()) {
          byte[] image = transport.downloadImage(code);
          bytes += image.length;
          if (bytes > 20_000_000) throw new IllegalArgumentException("图片合计不能超过 20 MB。");
          imageIds.add(gateway.uploadImage(workflowId, image).path("imageId").asText());
        }
      }
      if (imageIds.size() > 5) throw new IllegalArgumentException("每条消息最多 5 张图片。");
      store.saveMessageImages(message.messageId(), imageIds, text.isBlank());
      if (text.isBlank()) {
        reply(message, workflowId, "请引用这条图片消息，补充希望如何使用图片；也可以重新发送编号、需求和图片。");
        store.markInboundFinished(workflowId, inbound.workflowMessageId(), false);
        return;
      }
      String actor = properties.getClientId() + ":" + message.senderUserId();
      String actionId = null;
      if ("确认执行".equals(text) || "取消操作".equals(text)) {
        JsonNode pending = gateway.get("/workflows/" + workflowId).path("pendingControl");
        actionId =
            (!hasText(message.replyToMessageId()) && !hasText(message.quotedText()))
                ? pending.path("actionId").asText(null)
                : store.quotedAction(message);
        if (actionId == null
            || !actor.equals(pending.path("actorId").asText())
            || !actionId.equals(pending.path("actionId").asText())) {
          throw new IllegalArgumentException("请由提议人携带工作流编号，或引用本人有效的确认消息操作。");
        }
        if ("确认执行".equals(text) && "restart_from".equals(pending.path("type").asText())) {
          var busy = store.acquireForRestart(properties.getClientId(), workflowId);
          if (busy.isPresent()) throw new IllegalArgumentException("该任务已有其他运行，暂不能返工。");
          restartReserved = true;
        }
      }
      ObjectNode request =
          objectMapper
              .createObjectNode()
              .put("messageId", inbound.workflowMessageId())
              .put("text", text)
              .put("actorId", actor);
      if (actionId != null) request.put("expectedActionId", actionId);
      request.set("imageIds", objectMapper.valueToTree(imageIds));
      gateway.post("/workflows/" + workflowId + "/messages", request);
    } catch (RuntimeException error) {
      if (restartReserved && !workflowBecameActive(workflowId))
        store.releaseRestartReservation(properties.getClientId(), workflowId);
      store.markInboundFinished(workflowId, inbound.workflowMessageId(), true);
      reply(
          message,
          workflowId,
          error instanceof IllegalArgumentException
              ? error.getMessage()
              : "图片或消息未能交给任务助手，请检查格式后重新发送。");
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

  private boolean workflowFinished(String workflowId) {
    try {
      String status = gateway.get("/workflows/" + workflowId).path("status").asText();
      return Set.of("completed", "failed", "cancelled").contains(status);
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
        reply(message, binding.workflowId(), "当前没有等待确认的步骤。");
        return true;
      }
      gateway.post(
          "/workflows/" + binding.workflowId() + "/advance/" + gateId + "/" + action, null);
      reply(message, binding.workflowId(), "hold".equals(action) ? "已暂停，将等待手动继续。" : "已进入下一步。");
    } catch (RuntimeException error) {
      reply(message, binding.workflowId(), "步骤等待可能已结束，请查询当前进度后重试。");
    }
    return true;
  }

  private void enqueueBindingText(
      DingTalkModels.Binding binding, String dedupKey, String replyToMessageId, String text) {
    store.enqueueTargetText(
        dedupKey,
        binding.workflowId(),
        binding.conversationId(),
        binding.targetType(),
        binding.targetExternalId(),
        replyToMessageId,
        text);
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
    for (int page = 0; page < 1; page++) {
      JsonNode result;
      try {
        result =
            gateway.get(
                "/workflows/"
                    + binding.workflowId()
                    + "/events/history?after="
                    + cursor
                    + "&limit=200&view=bot");
      } catch (RuntimeException error) {
        LOGGER.debug("读取钉钉任务事件失败，workflowId={}。", binding.workflowId());
        return;
      }
      JsonNode events = result.path("events");
      if (!events.isArray()) return;
      for (JsonNode event : events) {
        long sequence = event.path("sequence").asLong();
        if (!consumeEvent(binding, event, sequence)) return;
        cursor = Math.max(cursor, sequence);
      }
      long nextCursor = result.path("nextCursor").asLong(cursor);
      if (nextCursor > cursor) {
        // 被过滤的底层事件只推进一次游标，不逐条产生数据库事务。
        store.recordEvent(
            properties.getClientId(),
            binding.workflowId(),
            nextCursor,
            "cursor:" + binding.workflowId() + ":" + nextCursor,
            null,
            null,
            null,
            false);
      }
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
      if ("submit_failed".equals(workflowRunStore.runStatus(binding.workflowId()))) {
        store.markSubmissionFailed(
            properties.getClientId(), binding.workflowId(), "任务启动失败，请稍后重新 @机器人运行。");
      }
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

    if (type.startsWith("appserver.")) {
      String notice = DingTalkExecutionNotice.execution(event);
      if (!notice.isBlank()) {
        String questionId =
            "assistant".equals(event.path("source").asText())
                ? payload.path("messageId").asText(null)
                : null;
        // 助手事件没有关联提问时不能回落到任务通知对象。
        if (!"assistant".equals(event.path("source").asText()) || hasText(questionId)) {
          JsonNode snapshot = gateway.get("/workflows/" + binding.workflowId());
          store.recordProcess(
              binding.workflowId(),
              questionId,
              sequence,
              DingTalkExecutionNotice.stepLabel(event, snapshot)
                  + "\n"
                  + DingTalkExecutionNotice.execution(event, snapshot),
              false,
              false);
          return true;
        }
      }
    }

    if ("chat.assistant.completed".equals(type) || "chat.message.failed".equals(type)) {
      workflowMessageId = payload.path("messageId").asText();
      boolean failed = "chat.message.failed".equals(type);
      store.completeReply(
          binding.workflowId(),
          workflowMessageId,
          sequence,
          failed ? "任务助手暂时无法完成回复，请稍后重试。" : payload.path("text").asText(),
          payload.path("actionId").asText(null));
      finishAssistantEvent(binding, workflowMessageId, failed);
      return true;
    } else if (PROGRESS_EVENTS.contains(type)) {
      try {
        JsonNode snapshot = gateway.get("/workflows/" + binding.workflowId());
        String notice =
            DingTalkExecutionNotice.stepLabel(event, snapshot) + "：" + eventNotice(type);
        if (TERMINAL_EVENTS.contains(type) && !snapshot.path("response").asText().isBlank())
          notice += "\n执行结果：\n" + DingTalkExecutionNotice.safe(snapshot.path("response").asText());
        store.recordProcess(
            binding.workflowId(),
            null,
            sequence,
            notice,
            TERMINAL_EVENTS.contains(type),
            TERMINAL_EVENTS.contains(type)
                || "step.advance.waiting".equals(type)
                || "step.advance.held".equals(type)
                || "node.failed".equals(type)
                || "node.timed_out".equals(type));
        return true;
      } catch (RuntimeException error) {
        LOGGER.debug("读取任务进度失败，workflowId={}。", binding.workflowId());
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
      DingTalkModels.Binding binding =
          store
              .binding(workflowId)
              .orElse(new DingTalkModels.Binding(workflowId, "", "", "active", 0, null, false));
      if ("dingtalk".equals(binding.triggerSource()) || "chat".equals(binding.triggerSource()))
        return;
      store.enqueueText(
          dedupKey,
          workflowId,
          binding.conversationId(),
          binding.rootMessageId(),
          "工作流编号：" + workflowId + "\n" + DingTalkExecutionNotice.safe(notice));
    } catch (RuntimeException error) {
      LOGGER.debug("生成钉钉任务进度消息失败，workflowId={}。", workflowId);
    }
  }

  private boolean supportsCard(DingTalkModels.Binding binding) {
    return !"chat".equals(binding.triggerSource())
        && !"dingtalk".equals(binding.triggerSource())
        && "GROUP".equals(binding.targetType())
        && !properties.getCardTemplateId().isBlank();
  }

  @SuppressWarnings("unchecked")
  void deliver(DingTalkModels.Outbox item) {
    try {
      DingTalkModels.SendResult result;
      if ("reply".equals(item.messageKind())) {
        result = transport.sendReply(item.targetExternalId(), item.targetType(), item.payload());
      } else if ("text".equals(item.messageKind())) {
        result =
            "PERSON".equals(item.targetType())
                ? transport.sendPersonText(
                    item.targetExternalId(), item.payload().path("text").asText())
                : transport.sendText(
                    item.targetExternalId(),
                    item.replyToMessageId(),
                    item.payload().path("text").asText());
      } else if ("markdown".equals(item.messageKind())) {
        result =
            "PERSON".equals(item.targetType())
                ? transport.sendPersonMarkdown(
                    item.targetExternalId(),
                    item.payload().path("title").asText("任务进度"),
                    item.payload().path("text").asText())
                : transport.sendMarkdown(
                    item.targetExternalId(),
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
    return value == null ? "" : value.replaceAll("\\p{Z}", " ").strip();
  }

  private static boolean validMessageEnvelope(DingTalkModels.Message message) {
    return hasLength(message.messageId(), 256)
        && hasLength(message.conversationId(), 256)
        && hasLength(message.senderUserId(), 256)
        && optionalLength(message.replyToMessageId(), 256);
  }

  private static boolean matches(DingTalkModels.Binding binding, DingTalkModels.Message message) {
    if ("PERSON".equals(binding.targetType())) {
      return !"2".equals(message.conversationType())
          && binding.targetExternalId().equals(message.senderUserId());
    }
    return "2".equals(message.conversationType())
        && binding.targetExternalId().equals(message.conversationId());
  }

  private static String incomingTargetType(DingTalkModels.Message message) {
    return "2".equals(message.conversationType()) ? "GROUP" : "PERSON";
  }

  private static String incomingTargetId(DingTalkModels.Message message) {
    return "2".equals(message.conversationType())
        ? message.conversationId()
        : message.senderUserId();
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
      return UUID.fromString(value).toString().equalsIgnoreCase(value);
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
      case "node.started" -> "已开始执行。";
      case "node.completed" -> "已完成。";
      case "node.failed" -> "执行失败，请查看监控页。";
      case "node.cancelled" -> "已取消。";
      case "node.timed_out" -> "执行超时。";
      case "step.advance.waiting" -> "等待进入下一步，可通过编号暂停或继续。";
      case "step.advance.held" -> "已暂停，请通过编号继续。";
      case "step.advance.confirmed", "step.advance.resumed" -> "已确认继续下一步。";
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
