package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.application.BoundedWork;
import com.codexflow.configcenter.application.WorkflowRunService;
import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.PreparedRun;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import java.time.Instant;
import java.util.List;
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
          this::safelyHandleAction);
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
        int batchLimit = remaining;
        var batch = DingTalkDatabaseRetry.execute("领取通知", () -> store.claimDue(batchLimit));
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
      // 首次入站即保持等待；同一编号重试不会保持后续新等待。
      JsonNode observation =
          gateway.post(
              "/workflows/" + workflowId + "/input-observations",
              objectMapper.createObjectNode().put("messageId", inbound.workflowMessageId()));
      if (observation != null)
        store.recordInputGate(message.messageId(), observation.path("gateId").asText(null));
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
          case "暂停", "暂停一下", "等一下", "暂停，暂不进入下一步" -> "hold";
          case "确认继续", "继续", "立即进入下一步", "继续进入下一步" -> "confirm";
          default -> null;
        };
    if (action == null) return false;

    DingTalkModels.Inbound inbound = null;
    try {
      JsonNode snapshot = gateway.get("/workflows/" + binding.workflowId());
      String gateId = snapshot.path("pendingAdvance").path("gateId").asText();
      String quotedGate = store.quotedAdvance(message);
      if ("confirm".equals(action) && message.quotedCard() && quotedGate == null) {
        reply(message, binding.workflowId(), "已识别任务，但无法确认这张引用卡片对应的等待。请点击当前等待卡片的“继续执行”按钮。");
        return true;
      }
      if (quotedGate != null && !quotedGate.equals(gateId)) {
        reply(message, binding.workflowId(), "引用的等待已结束，请引用当前等待消息操作。");
        return true;
      }
      if (!isGateId(gateId)) {
        inbound = store.registerInbound(properties.getClientId(), binding, message);
        gateway.post(
            "/workflows/" + binding.workflowId() + "/input-observations",
            objectMapper
                .createObjectNode()
                .put("messageId", inbound.workflowMessageId())
                .put("hold", !"confirm".equals(action)));
        reply(message, binding.workflowId(), "当前没有等待确认的步骤。");
        return true;
      }
      inbound = store.registerInbound(properties.getClientId(), binding, message);
      JsonNode observed =
          gateway.post(
              "/workflows/" + binding.workflowId() + "/input-observations",
              objectMapper
                  .createObjectNode()
                  .put("messageId", inbound.workflowMessageId())
                  .put("hold", !"confirm".equals(action)));
      if (!gateId.equals(observed.path("gateId").asText())) {
        reply(message, binding.workflowId(), "该消息对应的等待已结束，请重新发送。");
        store.markInboundFinished(binding.workflowId(), inbound.workflowMessageId(), false);
        return true;
      }
      gateway.post(
          "/workflows/" + binding.workflowId() + "/advance/" + gateId + "/" + action, null);
      store.markInboundFinished(binding.workflowId(), inbound.workflowMessageId(), false);
      reply(
          message,
          binding.workflowId(),
          "hold".equals(action) ? "已保持等待，请回复“确认继续”。" : "已确认，任务将继续下一步。");
    } catch (RuntimeException error) {
      reply(message, binding.workflowId(), "步骤等待可能已结束，请查询当前进度后重试。");
    } finally {
      if (inbound != null)
        store.markInboundFinished(binding.workflowId(), inbound.workflowMessageId(), false);
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

  Map<String, Object> safelyHandleAction(DingTalkModels.CardAction action) {
    LOGGER.info(
        "钉钉按钮回调：卡片={}，会话={}，操作人存在={}，任务编号有效={}，等待编号有效={}，继续动作={}。",
        DingTalkModels.referenceFingerprint(action.cardInstanceId()),
        DingTalkModels.referenceFingerprint(action.conversationId()),
        hasText(action.operatorUserId()),
        isUuid(stringValue(action.value(), "workflowId", null)),
        isGateId(stringValue(action.value(), "gateId", null)),
        "advance_confirm".equals(stringValue(action.value(), "action", action.actionId())));
    boolean success = false;
    try {
      success = handleAction(action);
    } catch (RuntimeException error) {
      LOGGER.warn("处理钉钉卡片动作失败，cardInstanceId={}。", action.cardInstanceId(), error);
    }
    LOGGER.info(
        "钉钉按钮处理结果：卡片={}，确认成功={}。",
        DingTalkModels.referenceFingerprint(action.cardInstanceId()),
        success);
    if (action.cardInstanceId() == null || !action.cardInstanceId().startsWith("wait-"))
      return Map.of();
    return Map.of(
        "cardData",
        Map.of("cardParamMap", Map.of("confirmRequestSucceeded", Boolean.toString(success))),
        "cardUpdateOptions",
        Map.of("updateCardDataByKey", true));
  }

  private boolean handleRestartCard(
      DingTalkModels.CardAction event, String workflowId, boolean confirm) {
    String controlId = stringValue(event.value(), "controlId", null);
    if (!isUuid(workflowId) || !hasText(controlId) || controlId.length() > 128) return false;
    var source =
        store.controlCardMessage(event, properties.getClientId(), workflowId, controlId, confirm);
    var binding = store.binding(workflowId);
    if (source.isEmpty() || binding.isEmpty()) return false;
    JsonNode snapshot = gateway.get("/workflows/" + workflowId);
    JsonNode pending = snapshot.path("pendingControl");
    if (!controlId.equals(pending.path("actionId").asText())
        || !(stringValue(event.value(), "action", event.actionId()).startsWith("stop_")
                ? "stop"
                : "restart_from")
            .equals(pending.path("type").asText())
        || !"pending".equals(pending.path("status").asText())
        || !(properties.getClientId() + ":" + event.operatorUserId())
            .equals(pending.path("actorId").asText())
        || !Instant.parse(pending.path("expiresAt").asText()).isAfter(Instant.now())) {
      store.refreshWaitingCards(workflowId, snapshot);
      return false;
    }
    boolean reserved = false;
    try {
      if (confirm && "restart_from".equals(pending.path("type").asText())) {
        if (store.acquireForRestart(properties.getClientId(), workflowId).isPresent()) return false;
        reserved = true;
      }
      var inbound = store.registerInbound(properties.getClientId(), binding.get(), source.get());
      var request =
          objectMapper
              .createObjectNode()
              .put("messageId", inbound.workflowMessageId())
              .put("text", source.get().content())
              .put("actorId", properties.getClientId() + ":" + event.operatorUserId())
              .put("expectedActionId", controlId);
      gateway.post("/workflows/" + workflowId + "/messages", request);
      return true; // 仅表示提交成功；执行结果由原有任务助手事件返回。
    } catch (RuntimeException error) {
      if (reserved && !workflowBecameActive(workflowId))
        store.releaseRestartReservation(properties.getClientId(), workflowId);
      throw error;
    }
  }

  private boolean handleAction(DingTalkModels.CardAction event) {
    String action = stringValue(event.value(), "action", event.actionId());
    String workflowId = stringValue(event.value(), "workflowId", null);
    String gateId = stringValue(event.value(), "gateId", null);
    if (List.of("restart_confirm", "restart_cancel", "stop_confirm", "stop_cancel")
        .contains(action)) return handleRestartCard(event, workflowId, action.endsWith("_confirm"));
    if (!isUuid(workflowId) || !isGateId(gateId)) return false;
    Optional<DingTalkModels.Binding> binding = store.binding(workflowId);
    if (binding.isEmpty()) return false;
    if (hasText(event.cardInstanceId()) && event.cardInstanceId().startsWith("wait-")) {
      if (!"advance_confirm".equals(action) || !store.ownsWaitingCard(event, workflowId, gateId))
        return false;
      boolean confirmed = true;
      try {
        gateway.post("/workflows/" + workflowId + "/advance/" + gateId + "/confirm", null);
      } catch (GatewayFailure error) {
        if (error.getStatusCode() != 409 && error.getStatusCode() != 404) throw error;
        confirmed = false;
      }
      try {
        store.refreshWaitingCards(workflowId, gateway.get("/workflows/" + workflowId));
      } catch (RuntimeException error) {
        LOGGER.debug("继续操作后卡片更新待下轮重试，workflowId={}。", workflowId);
      }
      return confirmed;
    }
    if (hasText(event.conversationId())
        && !binding.get().conversationId().equals(event.conversationId())) return false;
    if (hasText(event.cardInstanceId())
        && hasText(binding.get().progressCardInstanceId())
        && !binding.get().progressCardInstanceId().equals(event.cardInstanceId())) return false;
    String notice;
    try {
      if ("advance_hold".equals(action)) {
        gateway.post("/workflows/" + workflowId + "/advance/" + gateId + "/hold", null);
        notice = "已暂停，将等待手动继续。";
      } else if ("advance_confirm".equals(action)) {
        gateway.post("/workflows/" + workflowId + "/advance/" + gateId + "/confirm", null);
        notice = "已进入下一步。";
      } else {
        return false;
      }
    } catch (GatewayFailure error) {
      if (error.getStatusCode() != 409 && error.getStatusCode() != 404) throw error;
      notice = "操作已生效或等待已结束；如果倒计时已经到期，任务已自动继续。";
    }
    enqueueCurrentProgress(
        workflowId, notice, "card-action:" + workflowId + ":" + gateId + ":" + action);
    return true;
  }

  private void pollBinding(DingTalkModels.Binding binding) {
    if ("submitting".equals(binding.status()) && !recoverSubmission(binding)) return;
    store.refreshWaitingCards(
        binding.workflowId(), gateway.get("/workflows/" + binding.workflowId()));
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
          String visible = DingTalkExecutionNotice.execution(event, snapshot);
          if (visible.isBlank()) {
            store.recordEvent(
                properties.getClientId(),
                binding.workflowId(),
                sequence,
                "hidden-execution:" + sequence,
                null,
                null,
                null,
                false);
            return true;
          }
          String text = DingTalkExecutionNotice.stepLabel(event, snapshot) + "\n" + visible;
          if ("supervisor".equals(event.path("source").asText())) {
            recordProcessWithRetry(
                binding.workflowId(), questionId, sequence, text, false, false, event);
          } else
            recordProcessWithRetry(
                binding.workflowId(), questionId, sequence, text, false, false, null);
          return true;
        }
      }
    }

    if ("chat.assistant.completed".equals(type) || "chat.message.failed".equals(type)) {
      workflowMessageId = payload.path("messageId").asText();
      boolean failed = "chat.message.failed".equals(type);
      JsonNode replySnapshot = gateway.get("/workflows/" + binding.workflowId());
      String replyGate = replySnapshot.path("pendingAdvance").path("gateId").asText();
      boolean waitingReply =
          isGateId(replyGate)
              && !"closed".equals(DingTalkWaitingCard.state(replySnapshot, replyGate));
      if (waitingReply
          || (List.of("restart_from", "stop")
                  .contains(replySnapshot.path("pendingControl").path("type").asText())
              && payload
                  .path("actionId")
                  .asText()
                  .equals(replySnapshot.path("pendingControl").path("actionId").asText()))) {
        store.completeReply(
            binding.workflowId(),
            workflowMessageId,
            sequence,
            failed ? "任务助手暂时无法完成回复，请稍后重试。" : payload.path("text").asText(),
            payload.path("actionId").asText(null),
            replySnapshot);
      } else {
        store.completeReply(
            binding.workflowId(),
            workflowMessageId,
            sequence,
            failed ? "任务助手暂时无法完成回复，请稍后重试。" : payload.path("text").asText(),
            payload.path("actionId").asText(null));
      }
      finishAssistantEvent(binding, workflowMessageId, failed);
      return true;
    } else if (PROGRESS_EVENTS.contains(type)) {
      try {
        if ("step.advance.held".equals(type)) {
          JsonNode heldSnapshot = gateway.get("/workflows/" + binding.workflowId());
          String heldGate = heldSnapshot.path("pendingAdvance").path("gateId").asText();
          if (isGateId(heldGate)
              && heldGate.equals(payload.path("gateId").asText())
              && "held".equals(DingTalkWaitingCard.state(heldSnapshot, heldGate))) {
            store.recordWaitingCard(binding.workflowId(), sequence, heldSnapshot, false);
          } else {
            store.recordEvent(
                properties.getClientId(),
                binding.workflowId(),
                sequence,
                "expired-held:" + sequence,
                null,
                null,
                null,
                false);
          }
          store.refreshWaitingCards(binding.workflowId(), heldSnapshot);
          return true;
        }
        JsonNode snapshot = gateway.get("/workflows/" + binding.workflowId());
        if ("node.completed".equals(type) && !"chat".equals(binding.triggerSource())) {
          JsonNode gate = snapshot.path("pendingAdvance");
          String gateId = gate.path("gateId").asText();
          String nodeId = event.path("nodeId").asText(payload.path("nodeId").asText());
          if (isGateId(gateId)
              && nodeId.equals(gate.path("completedNodeId").asText())
              && !"closed".equals(DingTalkWaitingCard.state(snapshot, gateId))) {
            store.recordWaitingCard(binding.workflowId(), sequence, snapshot, true);
            return true;
          }
        }
        if (redundantAdvanceNotice(type, payload, snapshot)) {
          store.recordEvent(
              properties.getClientId(),
              binding.workflowId(),
              sequence,
              "merged-progress:" + sequence,
              null,
              null,
              null,
              false);
          store.refreshWaitingCards(binding.workflowId(), snapshot);
          return true;
        }
        if ("step.advance.waiting".equals(type)) {
          String gateId = payload.path("gateId").asText();
          JsonNode gate = snapshot.path("pendingAdvance");
          if (isGateId(gateId)
              && gateId.equals(gate.path("gateId").asText())
              && "countdown".equals(gate.path("state").asText())) {
            store.recordWaitingCard(binding.workflowId(), sequence, snapshot, true);
          } else {
            store.recordEvent(
                properties.getClientId(),
                binding.workflowId(),
                sequence,
                "expired-advance:" + gateId,
                null,
                null,
                null,
                false);
          }
          return true;
        }
        String notice =
            DingTalkExecutionNotice.stepLabel(event, snapshot) + "：" + eventNotice(type);
        store.refreshWaitingCards(binding.workflowId(), snapshot);
        if (TERMINAL_EVENTS.contains(type) && !snapshot.path("response").asText().isBlank())
          notice += "\n执行结果：\n" + DingTalkExecutionNotice.safe(snapshot.path("response").asText());
        recordProcessWithRetry(
            binding.workflowId(),
            null,
            sequence,
            notice,
            TERMINAL_EVENTS.contains(type),
            TERMINAL_EVENTS.contains(type)
                || "step.advance.waiting".equals(type)
                || "step.advance.held".equals(type)
                || "node.failed".equals(type)
                || "node.timed_out".equals(type),
            null);
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

  private void recordProcessWithRetry(
      String workflowId,
      String questionId,
      long sequence,
      String text,
      boolean terminal,
      boolean attention,
      JsonNode event) {
    DingTalkDatabaseRetry.run(
        "通知入队",
        () -> {
          if (event == null)
            store.recordProcess(workflowId, questionId, sequence, text, terminal, attention);
          else
            store.recordProcess(workflowId, questionId, sequence, text, terminal, attention, event);
        });
  }

  static boolean redundantAdvanceNotice(String type, JsonNode payload, JsonNode snapshot) {
    if ("step.advance.resumed".equals(type)) return true;
    if (!"step.advance.confirmed".equals(type)) return false;
    String nextId = payload.path("nextNodeId").asText();
    for (JsonNode node : snapshot.path("nodes"))
      if (!nextId.isBlank()
          && nextId.equals(node.path("id").asText())
          && !node.path("startedAt").asText().isBlank()) return true;
    return false;
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
      if ("waiting_card".equals(item.messageKind())
          || "waiting_card_update".equals(item.messageKind())) {
        deliverWaitingCard(item);
        return;
      }
      if (item.payload().hasNonNull("executionEvent")
          && DingTalkExecutionNotice.suppressWhileWaiting(
              item.payload().path("executionEvent"),
              gateway.get("/workflows/" + item.workflowId()))) {
        store.markOutboxSuperseded(item.id());
        return;
      }
      String gateId = item.payload().path("gateId").asText(null);
      String deliveredAt = item.payload().path("deliveredAt").asText(null);
      if (gateId != null && deliveredAt != null) {
        reportAdvanceDelivery(item, gateId, deliveredAt);
        store.markOutboxSent(item.id(), item.payload().path("sentMessageId").asText(null));
        return;
      }
      if (gateId != null) {
        JsonNode gate = gateway.get("/workflows/" + item.workflowId()).path("pendingAdvance");
        if (!gateId.equals(gate.path("gateId").asText())
            || !"countdown".equals(gate.path("state").asText())
            || !Instant.parse(gate.path("expiresAt").asText()).isAfter(Instant.now())) {
          store.markOutboxSuperseded(item.id());
          return;
        }
      }
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
      if (gateId != null) {
        Instant sentAt = Instant.now().truncatedTo(java.time.temporal.ChronoUnit.MICROS);
        store.markAdvanceDelivered(item.id(), result.messageId(), sentAt);
        reportAdvanceDelivery(item, gateId, sentAt.toString());
      }
      store.markOutboxSent(item.id(), result.messageId());
    } catch (RuntimeException error) {
      store.markOutboxFailed(item.id(), error);
    }
  }

  private void reportAdvanceDelivery(DingTalkModels.Outbox item, String gateId, String sentAt) {
    gateway.post(
        "/workflows/" + item.workflowId() + "/advance/" + gateId + "/notified",
        objectMapper.createObjectNode().put("sentAt", sentAt));
  }

  private void deliverWaitingCard(DingTalkModels.Outbox item) {
    String gateId = item.payload().path("gateId").asText();
    boolean update = "waiting_card_update".equals(item.messageKind());
    String cardId = update ? item.payload().path("cardId").asText() : "wait-" + item.id();
    String deliveredAt = item.payload().path("deliveredAt").asText(null);
    if (!update && deliveredAt != null) {
      if (item.payload().path("invitation").asBoolean())
        reportAdvanceDelivery(item, gateId, deliveredAt);
      store.markOutboxSent(item.id(), item.payload().path("sentMessageId").asText(null));
      return;
    }
    JsonNode snapshot = gateway.get("/workflows/" + item.workflowId());
    boolean closed = "closed".equals(DingTalkWaitingCard.cardState(snapshot, item.payload()));
    if (!update
        && closed
        && !item.payload().path("answer").asBoolean()
        && !item.payload().path("retainResult").asBoolean()) {
      store.markOutboxSuperseded(item.id());
      return;
    }
    Map<String, Object> data =
        DingTalkWaitingCard.render(item.workflowId(), item.payload(), snapshot);
    String sentMessageId = null;
    if (update) {
      transport.updateCard(cardId, data);
      store.markWaitingCardRefreshed(
          cardId,
          "disabled".equals(data.get("confirmStatus"))
              ? "closed"
              : DingTalkWaitingCard.cardState(snapshot, item.payload()));
    } else {
      DingTalkModels.SendResult receipt =
          transport.sendWaitingCard(
              cardId,
              item.targetType(),
              item.targetExternalId(),
              item.payload().path("atUserId").asText(null),
              data);
      sentMessageId = receipt == null ? null : receipt.messageId();
      Instant sentAt = Instant.now();
      store.markAdvanceDelivered(item.id(), sentMessageId, sentAt);
      if (item.payload().path("invitation").asBoolean())
        reportAdvanceDelivery(item, gateId, sentAt.toString());
    }
    store.markOutboxSent(item.id(), sentMessageId);
    store.refreshWaitingCards(item.workflowId(), gateway.get("/workflows/" + item.workflowId()));
  }

  private static String advanceNotice(JsonNode snapshot, JsonNode gate) {
    String completed = "当前步骤";
    String next = "下一步骤";
    int index = 0;
    for (JsonNode node : snapshot.path("nodes")) {
      index++;
      String name = DingTalkExecutionNotice.safe(node.path("displayName").asText("未命名步骤"));
      if (name.length() > 100) name = name.substring(0, 100) + "…";
      if (node.path("id").asText().equals(gate.path("completedNodeId").asText()))
        completed = "第" + index + "步「" + name + "」";
      if (node.path("id").asText().equals(gate.path("nextNodeId").asText()))
        next = "「" + name + "」";
    }
    return completed
        + "已完成，下一步"
        + next
        + "。\n"
        + "请引用本消息回复“确认继续”，立即开始下一步。\n"
        + "两分钟内未回复将自动继续。\n"
        + "如需提问或暂缓，请回复本消息，任务将保持等待。";
  }

  private static String normalizedCommand(String value) {
    return value == null ? "" : value.replaceAll("\\p{Z}", " ").strip();
  }

  private static boolean validMessageEnvelope(DingTalkModels.Message message) {
    return hasLength(message.messageId(), 256)
        && hasLength(message.conversationId(), 256)
        && hasLength(message.senderUserId(), 256)
        && optionalLength(message.replyToMessageId(), 256)
        && message.referenceIds().size() <= 8
        && message.referenceIds().stream().allMatch(id -> hasLength(id, 256));
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
      case "step.advance.held" -> "已保持等待，请回复“确认继续”进入下一步。";
      case "step.advance.confirmed", "step.advance.resumed" -> "已确认继续，等待下一步启动。";
      case "step.advance.timed_out" -> "两分钟等待已结束，任务已自动继续。";
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
