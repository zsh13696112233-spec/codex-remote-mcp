package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.domain.DingTalkTargetDirectory;
import com.codexflow.configcenter.domain.DingTalkTaskBindingDirectory;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 钉钉绑定、入站幂等、单任务占用和可靠发送队列的事务边界。 */
@Service
class DingTalkStore {

  private final DingTalkWorkflowBindingRepository bindings;
  private final DingTalkInboundMessageRepository inboundMessages;
  private final DingTalkOutboxRepository outbox;
  private final DingTalkTaskBindingDirectory taskBindings;
  private final DingTalkTargetDirectory targetDirectory;
  private final ObjectMapper objectMapper;

  DingTalkStore(
      DingTalkWorkflowBindingRepository bindings,
      DingTalkInboundMessageRepository inboundMessages,
      DingTalkOutboxRepository outbox,
      DingTalkTaskBindingDirectory taskBindings,
      DingTalkTargetDirectory targetDirectory,
      ObjectMapper objectMapper) {
    this.bindings = bindings;
    this.inboundMessages = inboundMessages;
    this.outbox = outbox;
    this.taskBindings = taskBindings;
    this.targetDirectory = targetDirectory;
    this.objectMapper = objectMapper;
  }

  @Transactional
  public void initialize(String clientId) {
    for (DingTalkOutboxEntity item : outbox.findAll()) {
      if ("sending".equals(item.status)) {
        item.status = "pending";
        item.nextAttemptAt = Instant.now();
        item.updatedAt = Instant.now();
      }
    }
  }

  @Transactional
  public DingTalkModels.StartReservation reserveStart(
      String clientId, DingTalkModels.Message message) {
    Optional<DingTalkWorkflowBindingEntity> duplicate =
        bindings.findByClientIdAndTriggerMessageId(clientId, message.messageId());
    if (duplicate.isPresent()) {
      return new DingTalkModels.StartReservation("duplicate", duplicate.get().workflowId, null);
    }
    DingTalkTaskBindingDirectory.StartRoute route = taskBindings.reserveNamed(message.content());
    if (!"started".equals(route.outcome())) {
      return new DingTalkModels.StartReservation(route.outcome(), route.workflowId(), null);
    }
    Instant now = Instant.now();
    DingTalkWorkflowBindingEntity binding = new DingTalkWorkflowBindingEntity();
    binding.workflowId = route.workflowId();
    binding.clientId = clientId;
    binding.taskDefinitionId = route.taskDefinitionId();
    binding.triggerMessageId = message.messageId();
    binding.triggerSource = "dingtalk";
    binding.conversationId = message.conversationId();
    binding.targetType = incomingTargetType(message);
    binding.targetExternalId = incomingTargetId(message);
    binding.targetName = message.conversationTitle();
    binding.rootMessageId = message.messageId();
    binding.initiatorUserId = message.senderUserId();
    binding.sessionWebhook = message.sessionWebhook();
    binding.status = "submitting";
    binding.createdAt = now;
    binding.updatedAt = now;
    bindings.saveAndFlush(binding);
    return new DingTalkModels.StartReservation(
        "started", route.workflowId(), route.prepared().payload());
  }

  /** 为没有钉钉入站消息的网页或定时运行创建主动通知绑定。 */
  @Transactional
  public void reserveProactive(
      String clientId, String taskId, String workflowId, String triggerSource) {
    DingTalkTargetDirectory.TargetView target =
        taskBindings.reserveProactive(taskId, workflowId, clientId);
    Instant now = Instant.now();
    DingTalkWorkflowBindingEntity binding = new DingTalkWorkflowBindingEntity();
    binding.workflowId = workflowId;
    binding.clientId = clientId;
    binding.taskDefinitionId = taskId;
    binding.triggerSource = triggerSource;
    binding.conversationId = target.externalId();
    binding.targetType = target.targetType();
    binding.targetExternalId = target.externalId();
    binding.targetName = target.displayName();
    binding.status = "submitting";
    binding.createdAt = now;
    binding.updatedAt = now;
    bindings.saveAndFlush(binding);
  }

  @Transactional
  public void discoverGroup(String clientId, DingTalkModels.Message message) {
    targetDirectory.discoverGroup(clientId, message.conversationId(), message.conversationTitle());
  }

  @Transactional
  public void ensureConversation(
      String clientId,
      String workflowId,
      String taskId,
      DingTalkModels.Message message,
      String status) {
    if (bindings.existsById(workflowId)) return;
    DingTalkWorkflowBindingEntity binding = new DingTalkWorkflowBindingEntity();
    binding.workflowId = workflowId;
    binding.clientId = clientId;
    binding.taskDefinitionId = taskId;
    binding.triggerSource = "chat";
    binding.conversationId = message.conversationId();
    binding.targetType = incomingTargetType(message);
    binding.targetExternalId = incomingTargetId(message);
    binding.status =
        List.of("queued", "running", "cancelling").contains(status) ? "active" : "terminal";
    binding.createdAt = Instant.now();
    binding.updatedAt = binding.createdAt;
    bindings.saveAndFlush(binding);
  }

  public DingTalkModels.Binding route(String workflowId, DingTalkModels.Message message) {
    return new DingTalkModels.Binding(
        workflowId,
        message.conversationId(),
        incomingTargetType(message),
        incomingTargetId(message),
        message.conversationTitle(),
        message.messageId(),
        "chat",
        "active",
        0,
        null,
        false);
  }

  @Transactional
  public void enqueueReply(
      String dedupKey, String workflowId, DingTalkModels.Message message, String text) {
    var payload =
        objectMapper
            .createObjectNode()
            .put("text", (workflowId == null ? "" : "工作流编号：" + workflowId + "\n") + text)
            .put("atUserId", message.senderUserId())
            .put("sessionWebhook", message.sessionWebhook());
    enqueue(
        dedupKey,
        workflowId,
        message.conversationId(),
        incomingTargetType(message),
        incomingTargetId(message),
        message.messageId(),
        "reply",
        payload);
  }

  @Transactional
  public void completeReply(
      String workflowId, String workflowMessageId, long sequence, String text, String actionId) {
    completeReply(workflowId, workflowMessageId, sequence, text, actionId, null);
  }

  @Transactional
  public void completeReply(
      String workflowId,
      String workflowMessageId,
      long sequence,
      String text,
      String actionId,
      JsonNode waitingSnapshot) {
    DingTalkWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    if (sequence <= binding.eventCursor) return;
    inboundMessages
        .findByWorkflowIdAndWorkflowMessageId(workflowId, workflowMessageId)
        .ifPresent(
            inbound -> {
              String conversationId =
                  inbound.conversationId == null ? binding.conversationId : inbound.conversationId;
              boolean group =
                  inbound.conversationType == null
                      ? "GROUP".equals(binding.targetType)
                      : "2".equals(inbound.conversationType);
              var payload =
                  objectMapper
                      .createObjectNode()
                      .put("text", "工作流编号：" + workflowId + "\n" + text)
                      .put("atUserId", inbound.senderUserId)
                      .put("sessionWebhook", inbound.sessionWebhook)
                      .put("actionId", actionId);
              inbound.actionId = actionId;
              boolean waiting =
                  waitingSnapshot != null
                      && inbound.observedGateId != null
                      && inbound.observedGateId.equals(
                          waitingSnapshot.path("pendingAdvance").path("gateId").asText());
              JsonNode control =
                  waitingSnapshot == null
                      ? objectMapper.createObjectNode()
                      : waitingSnapshot.path("pendingControl");
              boolean restart =
                  actionId != null
                      && actionId.equals(control.path("actionId").asText())
                      && List.of("restart_from", "stop").contains(control.path("type").asText());
              if (waiting) {
                payload =
                    waitingPayload(workflowId, waitingSnapshot, text, false)
                        .put("answer", true)
                        .put("atUserId", inbound.senderUserId)
                        .put("actionId", actionId);
              }
              if (restart) {
                payload
                    .put("restartControl", true)
                    .put("controlType", control.path("type").asText())
                    .put("answer", true)
                    .put("controlExpiresAt", control.path("expiresAt").asText())
                    .put("controlActorId", control.path("actorId").asText());
              }
              enqueue(
                  "assistant:" + workflowMessageId,
                  workflowId,
                  conversationId,
                  group ? "GROUP" : "PERSON",
                  group ? conversationId : inbound.senderUserId,
                  inbound.messageId,
                  waiting || restart ? "waiting_card" : "reply",
                  payload);
            });
    binding.eventCursor = sequence;
    binding.updatedAt = Instant.now();
  }

  /** 普通过程消息；提问按入站来源路由，运行过程只发往启动会话或通知对象。 */
  @Transactional
  public void recordProcess(
      String workflowId,
      String workflowMessageId,
      long sequence,
      String text,
      boolean terminal,
      boolean attention) {
    recordProcess(workflowId, workflowMessageId, sequence, text, terminal, attention, null);
  }

  @Transactional
  public void recordProcess(
      String workflowId,
      String workflowMessageId,
      long sequence,
      String text,
      boolean terminal,
      boolean attention,
      JsonNode executionEvent) {
    DingTalkWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    if (sequence <= binding.eventCursor) return;
    String key = "process:" + workflowId + ":" + sequence;
    if (workflowMessageId != null) {
      inboundMessages
          .findByWorkflowIdAndWorkflowMessageId(workflowId, workflowMessageId)
          .ifPresent(
              inbound -> {
                boolean group = "2".equals(inbound.conversationType);
                enqueue(
                    key,
                    workflowId,
                    inbound.conversationId,
                    group ? "GROUP" : "PERSON",
                    group ? inbound.conversationId : inbound.senderUserId,
                    inbound.messageId,
                    "reply",
                    objectMapper
                        .createObjectNode()
                        .put("text", "工作流编号：" + workflowId + "\n" + text)
                        .put("sessionWebhook", inbound.sessionWebhook));
              });
    } else if (!"chat".equals(binding.triggerSource)) {
      var payload =
          objectMapper.createObjectNode().put("text", "工作流编号：" + workflowId + "\n" + text);
      if (executionEvent != null) {
        payload
            .putObject("executionEvent")
            .put("source", executionEvent.path("source").asText())
            .put("createdAt", executionEvent.path("createdAt").asText());
      }
      enqueue(
          key,
          workflowId,
          binding.conversationId,
          binding.targetType,
          binding.targetExternalId,
          binding.rootMessageId,
          "text",
          payload);
      if (attention
          && "dingtalk".equals(binding.triggerSource)
          && "GROUP".equals(binding.targetType)
          && binding.sessionWebhook != null) {
        // 正文不依赖临时地址。独立提醒失败时，已发送的进度和结果仍然可见。
        enqueue(
            key + ":mention",
            workflowId,
            binding.conversationId,
            binding.targetType,
            binding.targetExternalId,
            binding.rootMessageId,
            "reply",
            objectMapper
                .createObjectNode()
                .put("text", "工作流编号：" + workflowId + "\n任务状态已更新，请查看上方消息。")
                .put("atUserId", binding.initiatorUserId)
                .put("sessionWebhook", binding.sessionWebhook));
      }
    }
    binding.eventCursor = sequence;
    binding.updatedAt = Instant.now();
    if (terminal) {
      binding.status = "terminal";
      releaseSlot(binding, workflowId);
    }
  }

  @Transactional(readOnly = true)
  public String quotedAction(DingTalkModels.Message message) {
    return quotedOutgoing(message)
        .map(this::toOutbox)
        .map(item -> item.payload().path("actionId").asText(null))
        .orElse(null);
  }

  /** 保持等待只发送一条完整通知，不再追加独立的 @ 提醒。 */
  @Transactional
  public void recordHeld(String workflowId, long sequence, String text) {
    DingTalkWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    if (sequence <= binding.eventCursor) return;
    if (!"chat".equals(binding.triggerSource)) {
      var payload =
          objectMapper.createObjectNode().put("text", "工作流编号：" + workflowId + "\n" + text);
      boolean reply =
          "dingtalk".equals(binding.triggerSource) && hasTextValue(binding.sessionWebhook);
      if (reply) {
        payload
            .put("sessionWebhook", binding.sessionWebhook)
            .put("atUserId", binding.initiatorUserId);
      }
      enqueue(
          "held:" + workflowId + ":" + sequence,
          workflowId,
          binding.conversationId,
          binding.targetType,
          binding.targetExternalId,
          binding.rootMessageId,
          reply ? "reply" : "text",
          payload);
    }
    binding.eventCursor = sequence;
    binding.updatedAt = Instant.now();
  }

  @Transactional(readOnly = true)
  public String quotedAdvance(DingTalkModels.Message message) {
    return quotedOutgoing(message).map(item -> item.advanceGateId).orElse(null);
  }

  @Transactional
  public void recordWaitingCard(
      String workflowId, long sequence, JsonNode snapshot, boolean invitation) {
    var binding = requiredBindingForUpdate(workflowId);
    if (sequence <= binding.eventCursor) return;
    if (!"chat".equals(binding.triggerSource)) {
      String gateId = snapshot.path("pendingAdvance").path("gateId").asText();
      if (!invitation
          && outbox
              .findByWorkflowIdAndWaitingCardStateIn(workflowId, List.of("countdown", "held"))
              .stream()
              .anyMatch(
                  card ->
                      "waiting_card".equals(card.messageKind)
                          && gateId.equals(card.advanceGateId)
                          && !toOutbox(card).payload().path("restartControl").asBoolean()
                          && binding.conversationId.equals(card.conversationId))) {
        refreshWaitingCards(workflowId, snapshot);
        binding.eventCursor = sequence;
        binding.updatedAt = Instant.now();
        return;
      }
      String body = invitation ? DingTalkWaitingCard.completion(snapshot) : "任务已保持等待，不会自动进入下一步。";
      var payload =
          waitingPayload(workflowId, snapshot, body, invitation)
              .put("atUserId", binding.initiatorUserId);
      if (invitation) payload.put("retainResult", true);
      enqueue(
          "waiting:" + workflowId + ":" + gateId + ":" + (invitation ? "invitation" : "held"),
          workflowId,
          binding.conversationId,
          binding.targetType,
          binding.targetExternalId,
          binding.rootMessageId,
          "waiting_card",
          payload);
    }
    binding.eventCursor = sequence;
    binding.updatedAt = Instant.now();
  }

  private tools.jackson.databind.node.ObjectNode waitingPayload(
      String workflowId, JsonNode snapshot, String text, boolean invitation) {
    return objectMapper
        .createObjectNode()
        .put("gateId", snapshot.path("pendingAdvance").path("gateId").asText())
        .put("invitation", invitation)
        .put("title", DingTalkExecutionNotice.safe(snapshot.path("name").asText("任务等待确认")))
        .put("steps", DingTalkWaitingCard.steps(snapshot))
        .put("text", "工作流编号：" + workflowId + "\n\n" + DingTalkExecutionNotice.safe(text));
  }

  /** 每张卡片独立刷新，回答正文仍使用各自的不可变快照。 */
  @Transactional
  public void refreshWaitingCards(String workflowId, JsonNode snapshot) {
    requiredBindingForUpdate(workflowId);
    for (var card :
        outbox.findByWorkflowIdAndWaitingCardStateIn(workflowId, List.of("countdown", "held"))) {
      if (card.deliveredAt == null) continue;
      String state = DingTalkWaitingCard.cardState(snapshot, toOutbox(card).payload());
      if (state.equals(card.waitingCardState)) continue;
      var payload = (tools.jackson.databind.node.ObjectNode) toOutbox(card).payload();
      payload.put("cardId", "wait-" + card.id);
      String updateKey = "waiting-update:" + card.id + ":" + state;
      enqueue(
          updateKey,
          workflowId,
          card.conversationId,
          card.targetType,
          card.targetExternalId,
          card.replyToMessageId,
          "waiting_card_update",
          payload);
      // 调度快照可能在投递前过时。仅在真正更新后保存远端展示状态；必要时重用更新记录。
      outbox
          .findByDedupKey(updateKey)
          .filter(item -> "sent".equals(item.status))
          .ifPresent(
              item -> {
                item.status = "pending";
                item.nextAttemptAt = Instant.now();
              });
    }
  }

  @Transactional
  public void markWaitingCardRefreshed(String cardId, String state) {
    var card = outbox.findById(cardId.substring(5)).orElseThrow();
    card.waitingCardState = state;
  }

  @Transactional(readOnly = true)
  public boolean ownsWaitingCard(
      DingTalkModels.CardAction event, String workflowId, String gateId) {
    if (event.cardInstanceId() == null || !event.cardInstanceId().startsWith("wait-")) return false;
    if (!hasTextValue(event.operatorUserId())) return false;
    return outbox
        .findById(event.cardInstanceId().substring(5))
        .filter(
            card ->
                "waiting_card".equals(card.messageKind)
                    && card.deliveredAt != null
                    && workflowId.equals(card.workflowId)
                    && gateId.equals(card.advanceGateId)
                    && !toOutbox(card).payload().path("restartControl").asBoolean()
                    && ("GROUP".equals(card.targetType)
                        ? (hasTextValue(card.conversationId)
                            && (!hasTextValue(event.conversationId())
                                || card.conversationId.equals(event.conversationId())))
                        : ("PERSON".equals(card.targetType)
                            && card.targetExternalId.equals(event.operatorUserId()))))
        .isPresent();
  }

  @Transactional(readOnly = true)
  public Optional<DingTalkModels.Message> controlCardMessage(
      DingTalkModels.CardAction event,
      String clientId,
      String workflowId,
      String controlId,
      boolean confirm) {
    if (event.cardInstanceId() == null
        || !event.cardInstanceId().startsWith("wait-")
        || !hasTextValue(event.operatorUserId())) return Optional.empty();
    return outbox
        .findById(event.cardInstanceId().substring(5))
        .filter(
            card -> {
              JsonNode payload = toOutbox(card).payload();
              return "waiting_card".equals(card.messageKind)
                  && card.deliveredAt != null
                  && workflowId.equals(card.workflowId)
                  && payload.path("restartControl").asBoolean()
                  && controlId.equals(payload.path("actionId").asText())
                  && (String.valueOf(event.value().getOrDefault("action", event.actionId()))
                              .startsWith("stop_")
                          ? "stop"
                          : "restart_from")
                      .equals(payload.path("controlType").asText("restart_from"))
                  && (clientId + ":" + event.operatorUserId())
                      .equals(payload.path("controlActorId").asText())
                  && (!hasTextValue(event.conversationId())
                      || event.conversationId().equals(card.conversationId))
                  && ("GROUP".equals(card.targetType)
                      || ("PERSON".equals(card.targetType)
                          && event.operatorUserId().equals(card.targetExternalId)));
            })
        .map(
            card ->
                new DingTalkModels.Message(
                    UUID.nameUUIDFromBytes(
                            (event.cardInstanceId() + ":" + controlId + ":" + confirm)
                                .getBytes(StandardCharsets.UTF_8))
                        .toString(),
                    card.conversationId,
                    "GROUP".equals(card.targetType) ? "2" : "1",
                    event.operatorUserId(),
                    confirm ? "确认执行" : "取消操作",
                    true,
                    false,
                    null));
  }

  @Transactional
  public void recordAdvance(String workflowId, long sequence, String gateId, String text) {
    DingTalkWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    if (sequence <= binding.eventCursor) return;
    if (!"chat".equals(binding.triggerSource)) {
      // 在工作流行锁内编号，重试复用已入队正文；不同等待不能因正文相同而引用歧义。
      long noticeNumber = outbox.countAdvanceNotices(workflowId) + 1;
      var payload =
          objectMapper
              .createObjectNode()
              .put("text", "工作流编号：" + workflowId + "\n第" + noticeNumber + "次等待确认\n" + text)
              .put("gateId", gateId);
      boolean reply =
          "dingtalk".equals(binding.triggerSource) && hasTextValue(binding.sessionWebhook);
      if (reply) {
        payload
            .put("sessionWebhook", binding.sessionWebhook)
            .put("atUserId", binding.initiatorUserId);
      }
      enqueue(
          "advance:" + workflowId + ":" + gateId,
          workflowId,
          binding.conversationId,
          binding.targetType,
          binding.targetExternalId,
          binding.rootMessageId,
          reply ? "reply" : "text",
          payload);
    }
    binding.eventCursor = sequence;
    binding.updatedAt = Instant.now();
  }

  private Optional<DingTalkOutboxEntity> quotedOutgoing(DingTalkModels.Message message) {
    var matches = new java.util.LinkedHashMap<String, DingTalkOutboxEntity>();
    if (message.referenceIds().isEmpty()) return quotedOutgoingSingle(message);
    for (String id : message.referenceIds()) {
      quotedOutgoingSingle(message.withReference(id)).ifPresent(item -> matches.put(item.id, item));
    }
    if (matches.size() > 1) throw new IllegalArgumentException("引用对应多条任务消息，请重新引用需要回复的消息。");
    return matches.values().stream().findFirst();
  }

  private Optional<DingTalkOutboxEntity> quotedOutgoingSingle(DingTalkModels.Message message) {
    if (hasTextValue(message.replyToMessageId())) {
      if (message.replyToMessageId().startsWith("wait-")) {
        var card =
            outbox
                .findById(message.replyToMessageId().substring(5))
                .filter(
                    item ->
                        "waiting_card".equals(item.messageKind)
                            && (item.conversationId.equals(message.conversationId())
                                || ("PERSON".equals(item.targetType)
                                    && "1".equals(message.conversationType())
                                    && item.targetExternalId.equals(message.senderUserId()))));
        if (card.isPresent()) return card;
      }
      var found =
          outbox.findFirstByConversationIdAndSentMessageIdOrderByCreatedAtDesc(
              message.conversationId(), message.replyToMessageId());
      if (found.isPresent()) return found;
    }
    // Some session replies do not return msgId. Only match exact content of an actually sent reply.
    if (!hasTextValue(message.quotedText())) return Optional.empty();
    String quotedText = normalizeQuote(message.quotedText());
    var header =
        java.util.regex.Pattern.compile(
                "^工作流编号[：:]\\s*([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?:\\n|$)")
            .matcher(quotedText);
    var matches = new ArrayList<DingTalkOutboxEntity>();
    if (header.find()) {
      String workflowId = header.group(1).toLowerCase(java.util.Locale.ROOT);
      for (int page = 0; ; page++) {
        var candidates =
            outbox.findDeliveredForQuote(
                message.conversationId(),
                workflowId,
                org.springframework.data.domain.PageRequest.of(page, 200));
        for (var item : candidates)
          if (quotedText.equals(normalizeQuote(toOutbox(item).payload().path("text").asText())))
            matches.add(item);
        if (candidates.size() < 200) break;
      }
    } else {
      for (var item : outbox.findRecentDelivered(message.conversationId()))
        if (quotedText.equals(normalizeQuote(toOutbox(item).payload().path("text").asText())))
          matches.add(item);
    }
    if (matches.isEmpty()) return Optional.empty();
    var first = matches.get(0);
    // 同一任务重复投递的普通进度正文可以关联；不同等待、控制提议或回答来源仍不能合并。
    boolean sameSource =
        matches.stream()
            .allMatch(
                item ->
                    java.util.Objects.equals(first.workflowId, item.workflowId)
                        && java.util.Objects.equals(first.advanceGateId, item.advanceGateId)
                        && java.util.Objects.equals(first.replyToMessageId, item.replyToMessageId)
                        && java.util.Objects.equals(first.messageKind, item.messageKind)
                        && java.util.Objects.equals(
                            toOutbox(first).payload().path("actionId").asText(),
                            toOutbox(item).payload().path("actionId").asText()));
    return sameSource && (matches.size() == 1 || !"waiting_card".equals(first.messageKind))
        ? Optional.of(first)
        : Optional.empty();
  }

  private static String normalizeQuote(String text) {
    return text.replace("\r\n", "\n").replace('\r', '\n').strip();
  }

  private static boolean hasTextValue(String value) {
    return value != null && !value.isBlank();
  }

  @Transactional(readOnly = true)
  public List<String> messageImages(String messageId, String workflowId, String conversationId) {
    if (messageId == null) return List.of();
    return inboundMessages
        .findById(messageId)
        .filter(
            item ->
                workflowId.equals(item.workflowId) && conversationId.equals(item.conversationId))
        .map(
            item -> {
              if (item.imageIdsJson == null) return List.<String>of();
              try {
                var ids = new ArrayList<String>();
                for (JsonNode value : objectMapper.readTree(item.imageIdsJson))
                  ids.add(value.asText());
                return List.copyOf(ids);
              } catch (Exception error) {
                throw new IllegalStateException("无法读取图片关联。");
              }
            })
        .orElse(List.of());
  }

  @Transactional(readOnly = true)
  public List<String> referencedImages(DingTalkModels.Message message, String workflowId) {
    String source =
        quotedOutgoing(message)
            .filter(item -> workflowId.equals(item.workflowId))
            .map(item -> item.replyToMessageId)
            .orElse(message.replyToMessageId());
    return messageImages(source, workflowId, message.conversationId());
  }

  @Transactional
  public void saveMessageImages(String messageId, List<String> imageIds, boolean awaitingText) {
    var item = inboundMessages.findById(messageId).orElseThrow();
    try {
      item.imageIdsJson = objectMapper.writeValueAsString(imageIds);
    } catch (Exception error) {
      throw new IllegalStateException("无法保存图片关联。");
    }
    if (awaitingText) item.status = "awaiting_text";
  }

  @Transactional(readOnly = true)
  public Optional<DingTalkModels.Binding> active(String clientId, DingTalkModels.Message message) {
    return taskBindings
        .active(clientId, incomingTargetType(message), incomingTargetId(message))
        .flatMap(route -> binding(route.workflowId()));
  }

  @Transactional(readOnly = true)
  public boolean hasActive(String clientId) {
    return taskBindings.hasActive(clientId);
  }

  @Transactional
  public void releaseFinished(String workflowId) {
    taskBindings.releaseFinished(workflowId);
  }

  @Transactional(readOnly = true)
  public Optional<DingTalkModels.Binding> binding(String workflowId) {
    if (workflowId == null) return Optional.empty();
    return bindings.findById(workflowId).map(DingTalkStore::toBinding);
  }

  @Transactional(readOnly = true)
  public Optional<DingTalkModels.Binding> conversation(
      String clientId, DingTalkModels.Message message) {
    var matches = new java.util.LinkedHashMap<String, DingTalkModels.Binding>();
    if (message.referenceIds().isEmpty())
      conversationSingle(clientId, message)
          .ifPresent(binding -> matches.put(binding.workflowId(), binding));
    // 先校验是否指向不同卡片（包括同一任务的不同等待），不能只取首个命中。
    quotedOutgoing(message);
    for (String id : message.referenceIds()) {
      conversationSingle(clientId, message.withReference(id))
          .ifPresent(binding -> matches.put(binding.workflowId(), binding));
    }
    quotedCardBinding(clientId, message)
        .ifPresent(binding -> matches.put(binding.workflowId(), binding));
    if (matches.size() > 1) throw new IllegalArgumentException("引用对应多个任务，请重新引用需要回复的消息。");
    return matches.values().stream().findFirst();
  }

  private Optional<DingTalkModels.Binding> quotedCardBinding(
      String clientId, DingTalkModels.Message message) {
    if (!message.quotedCard() || !hasTextValue(message.quotedText())) return Optional.empty();
    var matcher =
        java.util.regex.Pattern.compile(
                "(?m)^工作流编号[：:][ \\t]*([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})[ \\t]*$")
            .matcher(normalizeQuote(message.quotedText()));
    var ids = new java.util.LinkedHashSet<String>();
    while (matcher.find()) ids.add(matcher.group(1).toLowerCase(java.util.Locale.ROOT));
    if (ids.size() > 1) throw new IllegalArgumentException("引用卡片包含多个工作流编号，请明确要咨询的任务。");
    if (ids.isEmpty()) return Optional.empty();
    String workflowId = ids.iterator().next();
    return bindings
        .findById(workflowId)
        .filter(binding -> clientId.equals(binding.clientId))
        .filter(
            binding ->
                outbox.countDeliveredQuotedCards(
                        workflowId,
                        message.conversationType(),
                        message.conversationId(),
                        message.senderUserId())
                    > 0)
        .map(DingTalkStore::toBinding);
  }

  private Optional<DingTalkModels.Binding> conversationSingle(
      String clientId, DingTalkModels.Message message) {
    var outgoing =
        quotedOutgoing(message)
            .filter(item -> item.workflowId != null)
            .flatMap(item -> bindings.findById(item.workflowId))
            .filter(item -> clientId.equals(item.clientId))
            .map(DingTalkStore::toBinding);
    if (outgoing.isPresent()) return outgoing;
    String repliedMessageId = blankToNull(message.replyToMessageId());
    if (repliedMessageId == null) return Optional.empty();
    var inbound = inboundMessages.findById(repliedMessageId);
    if (inbound.isPresent() && message.conversationId().equals(inbound.get().conversationId)) {
      return binding(inbound.get().workflowId);
    }
    Optional<DingTalkWorkflowBindingEntity> byRoot =
        bindings.findFirstByClientIdAndConversationIdAndRootMessageIdOrderByCreatedAtDesc(
            clientId, message.conversationId(), repliedMessageId);
    if (byRoot.isPresent()) return byRoot.map(DingTalkStore::toBinding);
    Optional<DingTalkWorkflowBindingEntity> byCard =
        bindings.findFirstByClientIdAndConversationIdAndProgressCardInstanceIdOrderByCreatedAtDesc(
            clientId, message.conversationId(), repliedMessageId);
    if (byCard.isPresent()) return byCard.map(DingTalkStore::toBinding);
    return outbox
        .findFirstByConversationIdAndSentMessageIdOrderByCreatedAtDesc(
            message.conversationId(), repliedMessageId)
        .filter(item -> item.workflowId != null)
        .flatMap(item -> bindings.findById(item.workflowId))
        .filter(binding -> clientId.equals(binding.clientId))
        .map(DingTalkStore::toBinding);
  }

  @Transactional
  public void markSubmitted(String workflowId) {
    DingTalkWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    binding.status = "active";
    binding.updatedAt = Instant.now();
  }

  @Transactional
  public Optional<String> acquireForRestart(String clientId, String workflowId) {
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    Optional<String> busy = taskBindings.acquireForRestart(binding.taskDefinitionId, workflowId);
    if (busy.isPresent()) return busy;
    binding.status = "active";
    binding.updatedAt = Instant.now();
    return Optional.empty();
  }

  @Transactional
  public void releaseRestartReservation(String clientId, String workflowId) {
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.status = "terminal";
    binding.updatedAt = Instant.now();
    releaseSlot(binding, workflowId);
  }

  @Transactional
  public void reconcileRuntimeStatus(String clientId, String workflowId, String runtimeStatus) {
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    if (List.of("completed", "failed", "cancelled").contains(runtimeStatus)) {
      binding.status = "terminal";
      binding.updatedAt = Instant.now();
      releaseSlot(binding, workflowId);
    } else if (List.of("queued", "running", "cancelling").contains(runtimeStatus)) {
      if (taskBindings.reconcile(binding.taskDefinitionId, workflowId, true)) {
        binding.status = "active";
        binding.updatedAt = Instant.now();
      }
    }
  }

  @Transactional
  public void markSubmissionFailed(String clientId, String workflowId, String reason) {
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.status = "failed";
    binding.updatedAt = Instant.now();
    releaseSlot(binding, workflowId);
    enqueue(
        "submit-failed:" + workflowId,
        workflowId,
        binding.conversationId,
        binding.targetType,
        binding.targetExternalId,
        binding.rootMessageId,
        "text",
        objectMapper.createObjectNode().put("text", reason));
  }

  @Transactional(readOnly = true)
  public List<DingTalkModels.Binding> pollable(String clientId) {
    return bindings.findPollable(clientId).stream().map(DingTalkStore::toBinding).toList();
  }

  @Transactional
  public DingTalkModels.Inbound registerInbound(
      String clientId, DingTalkModels.Binding binding, DingTalkModels.Message message) {
    Optional<DingTalkInboundMessageEntity> existing = inboundMessages.findById(message.messageId());
    if (existing.isPresent()) {
      DingTalkInboundMessageEntity entity = existing.get();
      if ("accepted".equals(entity.status)) {
        DingTalkWorkflowBindingEntity storedBinding = requiredBinding(binding.workflowId());
        storedBinding.waitingAssistant = true;
        storedBinding.updatedAt = Instant.now();
      }
      return toInbound(entity);
    }
    String workflowMessageId =
        deterministicUuid(clientId, binding.workflowId(), message.messageId());
    Instant now = Instant.now();
    DingTalkInboundMessageEntity entity = new DingTalkInboundMessageEntity();
    entity.messageId = message.messageId();
    entity.workflowId = binding.workflowId();
    entity.workflowMessageId = workflowMessageId;
    entity.senderUserId = message.senderUserId();
    entity.conversationId = message.conversationId();
    entity.conversationType = message.conversationType();
    entity.sessionWebhook = message.sessionWebhook();
    entity.status = "accepted";
    entity.createdAt = now;
    entity.updatedAt = now;
    inboundMessages.saveAndFlush(entity);
    DingTalkWorkflowBindingEntity storedBinding = requiredBinding(binding.workflowId());
    storedBinding.waitingAssistant = true;
    storedBinding.updatedAt = now;
    return toInbound(entity);
  }

  @Transactional(readOnly = true)
  public Optional<DingTalkModels.Inbound> inbound(String workflowId, String workflowMessageId) {
    return inboundMessages
        .findByWorkflowIdAndWorkflowMessageId(workflowId, workflowMessageId)
        .map(DingTalkStore::toInbound);
  }

  @Transactional
  public void recordInputGate(String messageId, String gateId) {
    var inbound = inboundMessages.findById(messageId).orElseThrow();
    if (inbound.observedGateId == null) inbound.observedGateId = gateId == null ? "" : gateId;
  }

  @Transactional(readOnly = true)
  public Optional<String> latestAssistantReply(String workflowId) {
    return bindings.findById(workflowId).map(binding -> binding.latestAssistantReply);
  }

  @Transactional
  public boolean recordEvent(
      String clientId,
      String workflowId,
      long sequence,
      String dedupKey,
      String messageKind,
      String replyTo,
      JsonNode payload,
      boolean terminal) {
    DingTalkWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    if (sequence <= binding.eventCursor) return false;
    if (messageKind != null) {
      enqueue(
          dedupKey,
          workflowId,
          binding.conversationId,
          binding.targetType,
          binding.targetExternalId,
          replyTo,
          messageKind,
          payload);
    }
    binding.eventCursor = sequence;
    binding.updatedAt = Instant.now();
    if (terminal) {
      binding.status = "terminal";
      releaseSlot(binding, workflowId);
    }
    return true;
  }

  @Transactional
  public boolean recordAssistantCompleted(
      String workflowId,
      long sequence,
      String textDedupKey,
      String replyTo,
      JsonNode textPayload,
      String assistantReply,
      String cardDedupKey,
      JsonNode cardPayload) {
    DingTalkWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    if (sequence <= binding.eventCursor) return false;
    enqueue(
        textDedupKey,
        workflowId,
        binding.conversationId,
        binding.targetType,
        binding.targetExternalId,
        replyTo,
        "text",
        textPayload);
    binding.latestAssistantReply = abbreviate(assistantReply, 20_000);
    binding.latestAssistantReplyAt = Instant.now();
    if (cardPayload != null) {
      enqueue(
          cardDedupKey,
          workflowId,
          binding.conversationId,
          binding.targetType,
          binding.targetExternalId,
          binding.rootMessageId,
          binding.progressCardInstanceId == null ? "card" : "card_update",
          objectMapper.createObjectNode().set("card", cardPayload));
    }
    binding.eventCursor = sequence;
    binding.updatedAt = Instant.now();
    return true;
  }

  @Transactional
  public void markInboundFinished(String workflowId, String workflowMessageId, boolean failed) {
    inboundMessages
        .findByWorkflowIdAndWorkflowMessageId(workflowId, workflowMessageId)
        .ifPresent(
            inbound -> {
              inbound.status = failed ? "failed" : "completed";
              inbound.updatedAt = Instant.now();
            });
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.waitingAssistant = inboundMessages.existsByWorkflowIdAndStatus(workflowId, "accepted");
    binding.updatedAt = Instant.now();
  }

  @Transactional
  public void enqueueText(
      String dedupKey, String workflowId, String conversationId, String replyTo, String text) {
    if (workflowId != null) {
      DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
      enqueueTargetText(
          dedupKey,
          workflowId,
          binding.conversationId,
          binding.targetType == null ? "GROUP" : binding.targetType,
          binding.targetExternalId == null ? binding.conversationId : binding.targetExternalId,
          replyTo,
          text);
      return;
    }
    enqueueTargetText(dedupKey, workflowId, conversationId, "GROUP", conversationId, replyTo, text);
  }

  @Transactional
  public void enqueueTargetText(
      String dedupKey,
      String workflowId,
      String conversationId,
      String targetType,
      String targetExternalId,
      String replyTo,
      String text) {
    enqueue(
        dedupKey,
        workflowId,
        conversationId,
        targetType,
        targetExternalId,
        replyTo,
        "text",
        objectMapper.createObjectNode().put("text", text));
  }

  @Transactional
  public void enqueueCard(String dedupKey, String workflowId, JsonNode card) {
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    enqueue(
        dedupKey,
        workflowId,
        binding.conversationId,
        binding.targetType,
        binding.targetExternalId,
        binding.rootMessageId,
        binding.progressCardInstanceId == null ? "card" : "card_update",
        objectMapper.createObjectNode().set("card", card));
  }

  @Transactional
  public void enqueueProgressMarkdown(
      String dedupKey, String workflowId, String title, String markdown) {
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    enqueue(
        dedupKey,
        workflowId,
        binding.conversationId,
        binding.targetType,
        binding.targetExternalId,
        binding.rootMessageId,
        "markdown",
        objectMapper.createObjectNode().put("title", title).put("text", markdown));
  }

  @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
  public List<DingTalkModels.Outbox> claimDue() {
    return claimDue(50);
  }

  @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
  public List<DingTalkModels.Outbox> claimDue(int limit) {
    if (limit < 1 || limit > 50) throw new IllegalArgumentException("领取消息数量必须在 1 到 50 之间。");
    // 每个工作流、会话只领取最早的未完成消息，包括正在退避或发送中的前项。
    List<DingTalkModels.Outbox> claimed = new ArrayList<>();
    Instant now = Instant.now();
    // 范围筛选不加锁，避免领取查询的范围锁与并发入队争用；仅锁定选中的主键。
    for (String id : outbox.findDueIds(List.of("pending", "failed"), now, limit)) {
      DingTalkOutboxEntity item = outbox.lockById(id).orElse(null);
      // 候选查询后可能已被另一领取事务处理，必须在持锁后复核。
      if (item == null
          || !("pending".equals(item.status) || "failed".equals(item.status))
          || item.nextAttemptAt.isAfter(now)
          || outbox.countBlockingPredecessors(id) > 0) continue;
      item.status = "sending";
      item.attemptCount++;
      item.updatedAt = Instant.now();
      claimed.add(toOutbox(item));
    }
    return claimed;
  }

  @Transactional
  public void markOutboxSent(String id, String sentMessageId) {
    DingTalkOutboxEntity item = outbox.findById(id).orElseThrow();
    item.status = "sent";
    item.lastError = null;
    item.sentMessageId = sentMessageId;
    item.updatedAt = Instant.now();
    if ("card".equals(item.messageKind) && item.workflowId != null && sentMessageId != null) {
      DingTalkWorkflowBindingEntity binding = requiredBinding(item.workflowId);
      binding.progressCardInstanceId = sentMessageId;
      binding.updatedAt = Instant.now();
    }
  }

  @Transactional
  public void markAdvanceDelivered(String id, String sentMessageId, Instant deliveredAt) {
    DingTalkOutboxEntity item = outbox.findById(id).orElseThrow();
    if (item.deliveredAt == null) {
      item.deliveredAt = deliveredAt;
      item.sentMessageId = sentMessageId;
      if ("waiting_card".equals(item.messageKind))
        org.slf4j.LoggerFactory.getLogger(DingTalkStore.class)
            .info(
                "钉钉卡片关联指纹：会话={}，卡片={}，平台消息={}。",
                DingTalkModels.referenceFingerprint(item.conversationId),
                DingTalkModels.referenceFingerprint("wait-" + item.id),
                DingTalkModels.referenceFingerprint(item.sentMessageId));
    }
  }

  @Transactional(readOnly = true)
  public boolean isLatestCardOutbox(String id, String workflowId) {
    if (workflowId == null) return true;
    return outbox.findLatestCard(workflowId).map(item -> item.id.equals(id)).orElse(true);
  }

  @Transactional
  public void markOutboxSuperseded(String id) {
    DingTalkOutboxEntity item = outbox.findById(id).orElseThrow();
    item.status = "sent";
    if ("waiting_card".equals(item.messageKind)) item.waitingCardState = "closed";
    item.lastError = null;
    item.updatedAt = Instant.now();
  }

  @Transactional
  public void markOutboxFailed(String id, RuntimeException error) {
    DingTalkOutboxEntity item = outbox.findById(id).orElseThrow();
    item.status =
        "reply".equals(item.messageKind) && item.advanceGateId == null ? "abandoned" : "failed";
    item.lastError = abbreviate(error.getMessage(), 2000);
    item.nextAttemptAt =
        Instant.now().plus(Math.min(60, 1L << Math.min(item.attemptCount, 6)), ChronoUnit.SECONDS);
    item.updatedAt = Instant.now();
  }

  private void enqueue(
      String dedupKey,
      String workflowId,
      String conversationId,
      String targetType,
      String targetExternalId,
      String replyTo,
      String messageKind,
      JsonNode payload) {
    String content = payload.path("text").asText("");
    if (("reply".equals(messageKind) || "text".equals(messageKind)) && content.length() > 1000) {
      String prefix = workflowId == null ? "" : "工作流编号：" + workflowId + "\n";
      String body = content.startsWith(prefix) ? content.substring(prefix.length()) : content;
      int part = 0;
      for (int offset = 0; offset < body.length(); ) {
        int end = Math.min(offset + 850, body.length());
        if (end < body.length() && Character.isHighSurrogate(body.charAt(end - 1))) end--;
        var chunk = ((tools.jackson.databind.node.ObjectNode) payload).deepCopy();
        chunk.put("text", prefix + body.substring(offset, end));
        if (end < body.length()) chunk.remove("atUserId");
        enqueue(
            dedupKey + ":part:" + part++,
            workflowId,
            conversationId,
            targetType,
            targetExternalId,
            replyTo,
            messageKind,
            chunk);
        offset = end;
      }
      return;
    }
    if (outbox.existsByDedupKey(dedupKey)) return;
    Instant now = Instant.now();
    DingTalkOutboxEntity item = new DingTalkOutboxEntity();
    item.id = UUID.randomUUID().toString();
    item.dedupKey = dedupKey;
    item.workflowId = workflowId;
    item.conversationId = conversationId;
    item.targetType = targetType;
    item.targetExternalId = targetExternalId;
    item.replyToMessageId = replyTo;
    item.messageKind = messageKind;
    item.advanceGateId = payload.path("gateId").asText(null);
    if ("waiting_card".equals(messageKind)) item.waitingCardState = "countdown";
    try {
      item.payloadJson = objectMapper.writeValueAsString(payload);
    } catch (Exception error) {
      throw new IllegalStateException("无法保存钉钉待发送消息。", error);
    }
    item.status = "pending";
    item.nextAttemptAt = now;
    item.createdAt = now;
    item.updatedAt = now;
    outbox.save(item);
  }

  private void releaseSlot(DingTalkWorkflowBindingEntity binding, String workflowId) {
    taskBindings.reconcile(binding.taskDefinitionId, workflowId, false);
  }

  private DingTalkWorkflowBindingEntity requiredBinding(String workflowId) {
    return bindings.findById(workflowId).orElseThrow();
  }

  private DingTalkWorkflowBindingEntity requiredBindingForUpdate(String workflowId) {
    return bindings.findForUpdate(workflowId).orElseThrow();
  }

  private DingTalkModels.Outbox toOutbox(DingTalkOutboxEntity item) {
    try {
      var payload =
          (tools.jackson.databind.node.ObjectNode) objectMapper.readTree(item.payloadJson);
      if (item.advanceGateId != null) payload.put("gateId", item.advanceGateId);
      if (item.deliveredAt != null) {
        payload.put("deliveredAt", item.deliveredAt.toString());
        payload.put("sentMessageId", item.sentMessageId);
      }
      return new DingTalkModels.Outbox(
          item.id,
          item.workflowId,
          item.conversationId,
          item.targetType == null ? "GROUP" : item.targetType,
          item.targetExternalId == null ? item.conversationId : item.targetExternalId,
          item.replyToMessageId,
          item.messageKind,
          payload);
    } catch (Exception error) {
      throw new IllegalStateException("无法读取钉钉待发送消息。", error);
    }
  }

  private static DingTalkModels.Binding toBinding(DingTalkWorkflowBindingEntity binding) {
    return new DingTalkModels.Binding(
        binding.workflowId,
        binding.conversationId,
        binding.targetType == null ? "GROUP" : binding.targetType,
        binding.targetExternalId == null ? binding.conversationId : binding.targetExternalId,
        binding.targetName == null ? "群聊" : binding.targetName,
        binding.rootMessageId,
        binding.triggerSource == null ? "dingtalk" : binding.triggerSource,
        binding.status,
        binding.eventCursor,
        binding.progressCardInstanceId,
        binding.waitingAssistant);
  }

  private static DingTalkModels.Inbound toInbound(DingTalkInboundMessageEntity inbound) {
    return new DingTalkModels.Inbound(
        inbound.messageId, inbound.workflowId, inbound.workflowMessageId, inbound.status);
  }

  private static String deterministicUuid(String clientId, String workflowId, String messageId) {
    try {
      byte[] hash =
          MessageDigest.getInstance("SHA-1")
              .digest(
                  (clientId + "\n" + workflowId + "\n" + messageId)
                      .getBytes(StandardCharsets.UTF_8));
      hash[6] = (byte) ((hash[6] & 0x0f) | 0x50);
      hash[8] = (byte) ((hash[8] & 0x3f) | 0x80);
      String value = HexFormat.of().formatHex(hash, 0, 16);
      return value.substring(0, 8)
          + "-"
          + value.substring(8, 12)
          + "-"
          + value.substring(12, 16)
          + "-"
          + value.substring(16, 20)
          + "-"
          + value.substring(20);
    } catch (Exception error) {
      throw new IllegalStateException("无法生成任务助手消息编号。", error);
    }
  }

  private static String blankToNull(String value) {
    return value == null || value.isBlank() ? null : value;
  }

  private static String incomingTargetType(DingTalkModels.Message message) {
    return "2".equals(message.conversationType()) ? "GROUP" : "PERSON";
  }

  private static String incomingTargetId(DingTalkModels.Message message) {
    return "2".equals(message.conversationType())
        ? message.conversationId()
        : message.senderUserId();
  }

  private static String abbreviate(String value, int maxLength) {
    if (value == null) return "未知错误";
    return value.length() <= maxLength ? value : value.substring(0, maxLength);
  }
}
