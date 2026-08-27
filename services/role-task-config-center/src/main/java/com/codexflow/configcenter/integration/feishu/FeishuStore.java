package com.codexflow.configcenter.integration.feishu;

import com.codexflow.configcenter.domain.PreparedRun;
import com.codexflow.configcenter.domain.WorkflowRunStore;
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

/** 飞书绑定、入站幂等、单任务占用和可靠发送队列的事务边界。 */
@Service
class FeishuStore {

  private static final List<String> POLLED_STATUSES = List.of("submitting", "active", "terminal");

  private final FeishuBotStateRepository states;
  private final FeishuWorkflowBindingRepository bindings;
  private final FeishuInboundMessageRepository inboundMessages;
  private final FeishuOutboxRepository outbox;
  private final WorkflowRunStore workflowRuns;
  private final ObjectMapper objectMapper;

  FeishuStore(
      FeishuBotStateRepository states,
      FeishuWorkflowBindingRepository bindings,
      FeishuInboundMessageRepository inboundMessages,
      FeishuOutboxRepository outbox,
      WorkflowRunStore workflowRuns,
      ObjectMapper objectMapper) {
    this.states = states;
    this.bindings = bindings;
    this.inboundMessages = inboundMessages;
    this.outbox = outbox;
    this.workflowRuns = workflowRuns;
    this.objectMapper = objectMapper;
  }

  @Transactional
  public void initialize(String appId) {
    states.insertIfAbsent(appId);
    for (FeishuOutboxEntity item : outbox.findAll()) {
      if ("sending".equals(item.status)) {
        item.status = "pending";
        item.nextAttemptAt = Instant.now();
        item.updatedAt = Instant.now();
      }
    }
  }

  @Transactional
  public FeishuModels.StartReservation reserveStart(
      String appId, String taskDefinitionId, FeishuModels.Message message) {
    Optional<FeishuWorkflowBindingEntity> duplicate =
        bindings.findByAppIdAndTriggerMessageId(appId, message.messageId());
    if (duplicate.isPresent()) {
      return new FeishuModels.StartReservation("duplicate", duplicate.get().workflowId, null);
    }
    FeishuBotStateEntity state =
        states.findForUpdate(appId).orElseThrow(() -> new IllegalStateException("飞书机器人状态尚未初始化。"));
    if (state.activeWorkflowId != null) {
      return new FeishuModels.StartReservation("busy", state.activeWorkflowId, null);
    }

    PreparedRun prepared = workflowRuns.prepareLatest(taskDefinitionId);
    Instant now = Instant.now();
    FeishuWorkflowBindingEntity binding = new FeishuWorkflowBindingEntity();
    binding.workflowId = prepared.workflowId();
    binding.appId = appId;
    binding.taskDefinitionId = taskDefinitionId;
    binding.triggerMessageId = message.messageId();
    binding.chatId = message.chatId();
    binding.rootMessageId = message.messageId();
    binding.threadId = blankToNull(message.threadId());
    binding.initiatorOpenId = message.senderOpenId();
    binding.status = "submitting";
    binding.createdAt = now;
    binding.updatedAt = now;
    bindings.saveAndFlush(binding);
    state.activeWorkflowId = prepared.workflowId();
    state.updatedAt = now;
    return new FeishuModels.StartReservation("started", prepared.workflowId(), prepared.payload());
  }

  @Transactional(readOnly = true)
  public Optional<FeishuModels.Binding> active(String appId) {
    return states.findById(appId).flatMap(state -> binding(state.activeWorkflowId));
  }

  @Transactional(readOnly = true)
  public Optional<FeishuModels.Binding> binding(String workflowId) {
    if (workflowId == null) return Optional.empty();
    return bindings.findById(workflowId).map(FeishuStore::toBinding);
  }

  @Transactional(readOnly = true)
  public Optional<FeishuModels.Binding> conversation(String appId, FeishuModels.Message message) {
    if (message.threadId() != null && !message.threadId().isBlank()) {
      Optional<FeishuWorkflowBindingEntity> byThread =
          bindings.findFirstByAppIdAndChatIdAndThreadIdOrderByCreatedAtDesc(
              appId, message.chatId(), message.threadId());
      if (byThread.isPresent()) return byThread.map(FeishuStore::toBinding);
    }
    String rootId = blankToNull(message.rootId());
    if (rootId == null) rootId = blankToNull(message.replyToMessageId());
    if (rootId == null) return Optional.empty();
    return bindings
        .findFirstByAppIdAndChatIdAndRootMessageIdOrderByCreatedAtDesc(
            appId, message.chatId(), rootId)
        .map(FeishuStore::toBinding);
  }

  @Transactional
  public void markSubmitted(String workflowId) {
    FeishuWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    binding.status = "active";
    binding.updatedAt = Instant.now();
  }

  @Transactional
  public Optional<String> acquireForRestart(String appId, String workflowId) {
    FeishuBotStateEntity state = states.findForUpdate(appId).orElseThrow();
    if (state.activeWorkflowId != null && !workflowId.equals(state.activeWorkflowId)) {
      return Optional.of(state.activeWorkflowId);
    }
    state.activeWorkflowId = workflowId;
    state.updatedAt = Instant.now();
    FeishuWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.status = "active";
    binding.updatedAt = Instant.now();
    return Optional.empty();
  }

  @Transactional
  public void releaseRestartReservation(String appId, String workflowId) {
    FeishuWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.status = "terminal";
    binding.updatedAt = Instant.now();
    releaseSlot(appId, workflowId);
  }

  @Transactional
  public void reconcileRuntimeStatus(String appId, String workflowId, String runtimeStatus) {
    FeishuWorkflowBindingEntity binding = requiredBinding(workflowId);
    if (List.of("completed", "failed", "cancelled").contains(runtimeStatus)) {
      binding.status = "terminal";
      binding.updatedAt = Instant.now();
      releaseSlot(appId, workflowId);
    } else if (List.of("queued", "running", "cancelling").contains(runtimeStatus)) {
      FeishuBotStateEntity state = states.findForUpdate(appId).orElseThrow();
      if (state.activeWorkflowId == null || workflowId.equals(state.activeWorkflowId)) {
        state.activeWorkflowId = workflowId;
        state.updatedAt = Instant.now();
        binding.status = "active";
        binding.updatedAt = Instant.now();
      }
    }
  }

  @Transactional
  public void markSubmissionFailed(String appId, String workflowId, String reason) {
    FeishuWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.status = "failed";
    binding.updatedAt = Instant.now();
    releaseSlot(appId, workflowId);
    enqueue(
        "submit-failed:" + workflowId,
        workflowId,
        binding.chatId,
        binding.rootMessageId,
        "text",
        objectMapper.createObjectNode().put("text", reason));
  }

  @Transactional(readOnly = true)
  public List<FeishuModels.Binding> pollable(String appId) {
    return bindings.findByAppIdAndStatusInOrderByCreatedAt(appId, POLLED_STATUSES).stream()
        .filter(binding -> !"terminal".equals(binding.status) || binding.waitingAssistant)
        .map(FeishuStore::toBinding)
        .toList();
  }

  @Transactional
  public FeishuModels.Inbound registerInbound(
      String appId, FeishuModels.Binding binding, FeishuModels.Message message) {
    Optional<FeishuInboundMessageEntity> existing = inboundMessages.findById(message.messageId());
    if (existing.isPresent()) {
      FeishuInboundMessageEntity entity = existing.get();
      if ("accepted".equals(entity.status)) {
        FeishuWorkflowBindingEntity storedBinding = requiredBinding(binding.workflowId());
        storedBinding.waitingAssistant = true;
        storedBinding.updatedAt = Instant.now();
      }
      return toInbound(entity);
    }
    String workflowMessageId = deterministicUuid(appId, binding.workflowId(), message.messageId());
    Instant now = Instant.now();
    FeishuInboundMessageEntity entity = new FeishuInboundMessageEntity();
    entity.messageId = message.messageId();
    entity.workflowId = binding.workflowId();
    entity.workflowMessageId = workflowMessageId;
    entity.senderOpenId = message.senderOpenId();
    entity.status = "accepted";
    entity.createdAt = now;
    entity.updatedAt = now;
    inboundMessages.saveAndFlush(entity);
    FeishuWorkflowBindingEntity storedBinding = requiredBinding(binding.workflowId());
    storedBinding.waitingAssistant = true;
    if (storedBinding.threadId == null) storedBinding.threadId = blankToNull(message.threadId());
    storedBinding.updatedAt = now;
    return toInbound(entity);
  }

  @Transactional(readOnly = true)
  public Optional<FeishuModels.Inbound> inbound(String workflowId, String workflowMessageId) {
    return inboundMessages
        .findByWorkflowIdAndWorkflowMessageId(workflowId, workflowMessageId)
        .map(FeishuStore::toInbound);
  }

  @Transactional
  public boolean recordEvent(
      String appId,
      String workflowId,
      long sequence,
      String dedupKey,
      String messageKind,
      String replyTo,
      JsonNode payload,
      boolean terminal) {
    FeishuWorkflowBindingEntity binding = requiredBindingForUpdate(workflowId);
    if (sequence <= binding.eventCursor) return false;
    if (messageKind != null) {
      enqueue(dedupKey, workflowId, binding.chatId, replyTo, messageKind, payload);
    }
    binding.eventCursor = sequence;
    binding.updatedAt = Instant.now();
    if (terminal) {
      binding.status = "terminal";
      releaseSlot(appId, workflowId);
    }
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
    FeishuWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.waitingAssistant =
        inboundMessages.findAll().stream()
            .anyMatch(item -> workflowId.equals(item.workflowId) && "accepted".equals(item.status));
    binding.updatedAt = Instant.now();
  }

  @Transactional
  public void enqueueText(
      String dedupKey, String workflowId, String chatId, String replyTo, String text) {
    enqueue(
        dedupKey,
        workflowId,
        chatId,
        replyTo,
        "text",
        objectMapper.createObjectNode().put("text", text));
  }

  @Transactional
  public void enqueueCard(String dedupKey, String workflowId, JsonNode card) {
    FeishuWorkflowBindingEntity binding = requiredBinding(workflowId);
    enqueue(
        dedupKey,
        workflowId,
        binding.chatId,
        binding.rootMessageId,
        binding.progressMessageId == null ? "card" : "card_update",
        objectMapper.createObjectNode().set("card", card));
  }

  @Transactional
  public List<FeishuModels.Outbox> claimDue() {
    List<FeishuModels.Outbox> claimed = new ArrayList<>();
    for (FeishuOutboxEntity item :
        outbox.findDueForUpdate(List.of("pending", "failed"), Instant.now())) {
      item.status = "sending";
      item.attemptCount++;
      item.updatedAt = Instant.now();
      claimed.add(toOutbox(item));
    }
    return claimed;
  }

  @Transactional
  public void markOutboxSent(String id, String sentMessageId) {
    FeishuOutboxEntity item = outbox.findById(id).orElseThrow();
    item.status = "sent";
    item.lastError = null;
    item.updatedAt = Instant.now();
    if ("card".equals(item.messageKind) && item.workflowId != null && sentMessageId != null) {
      FeishuWorkflowBindingEntity binding = requiredBinding(item.workflowId);
      binding.progressMessageId = sentMessageId;
      binding.updatedAt = Instant.now();
    }
  }

  @Transactional
  public void markOutboxFailed(String id, RuntimeException error) {
    FeishuOutboxEntity item = outbox.findById(id).orElseThrow();
    item.status = "failed";
    item.lastError = abbreviate(error.getMessage(), 2000);
    item.nextAttemptAt =
        Instant.now().plus(Math.min(60, 1L << Math.min(item.attemptCount, 6)), ChronoUnit.SECONDS);
    item.updatedAt = Instant.now();
  }

  private void enqueue(
      String dedupKey,
      String workflowId,
      String chatId,
      String replyTo,
      String messageKind,
      JsonNode payload) {
    if (outbox.existsByDedupKey(dedupKey)) return;
    Instant now = Instant.now();
    FeishuOutboxEntity item = new FeishuOutboxEntity();
    item.id = UUID.randomUUID().toString();
    item.dedupKey = dedupKey;
    item.workflowId = workflowId;
    item.chatId = chatId;
    item.replyToMessageId = replyTo;
    item.messageKind = messageKind;
    try {
      item.payloadJson = objectMapper.writeValueAsString(payload);
    } catch (Exception error) {
      throw new IllegalStateException("无法保存飞书待发送消息。", error);
    }
    item.status = "pending";
    item.nextAttemptAt = now;
    item.createdAt = now;
    item.updatedAt = now;
    outbox.save(item);
  }

  private void releaseSlot(String appId, String workflowId) {
    FeishuBotStateEntity state = states.findForUpdate(appId).orElseThrow();
    if (workflowId.equals(state.activeWorkflowId)) {
      state.activeWorkflowId = null;
      state.updatedAt = Instant.now();
    }
  }

  private FeishuWorkflowBindingEntity requiredBinding(String workflowId) {
    return bindings.findById(workflowId).orElseThrow();
  }

  private FeishuWorkflowBindingEntity requiredBindingForUpdate(String workflowId) {
    return bindings.findForUpdate(workflowId).orElseThrow();
  }

  private FeishuModels.Outbox toOutbox(FeishuOutboxEntity item) {
    try {
      return new FeishuModels.Outbox(
          item.id,
          item.workflowId,
          item.chatId,
          item.replyToMessageId,
          item.messageKind,
          objectMapper.readTree(item.payloadJson));
    } catch (Exception error) {
      throw new IllegalStateException("无法读取飞书待发送消息。", error);
    }
  }

  private static FeishuModels.Binding toBinding(FeishuWorkflowBindingEntity binding) {
    return new FeishuModels.Binding(
        binding.workflowId,
        binding.chatId,
        binding.rootMessageId,
        binding.threadId,
        binding.status,
        binding.eventCursor,
        binding.progressMessageId,
        binding.waitingAssistant);
  }

  private static FeishuModels.Inbound toInbound(FeishuInboundMessageEntity inbound) {
    return new FeishuModels.Inbound(
        inbound.messageId, inbound.workflowId, inbound.workflowMessageId, inbound.status);
  }

  private static String deterministicUuid(String appId, String workflowId, String messageId) {
    try {
      byte[] hash =
          MessageDigest.getInstance("SHA-1")
              .digest(
                  (appId + "\n" + workflowId + "\n" + messageId).getBytes(StandardCharsets.UTF_8));
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

  private static String abbreviate(String value, int maxLength) {
    if (value == null) return "未知错误";
    return value.length() <= maxLength ? value : value.substring(0, maxLength);
  }
}
