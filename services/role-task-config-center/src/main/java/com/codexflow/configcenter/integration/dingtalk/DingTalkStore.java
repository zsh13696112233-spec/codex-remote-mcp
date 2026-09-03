package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.domain.DingTalkTargetDirectory;
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

/** 钉钉绑定、入站幂等、单任务占用和可靠发送队列的事务边界。 */
@Service
class DingTalkStore {

  private static final List<String> POLLED_STATUSES = List.of("submitting", "active", "terminal");

  private final DingTalkBotStateRepository states;
  private final DingTalkWorkflowBindingRepository bindings;
  private final DingTalkInboundMessageRepository inboundMessages;
  private final DingTalkOutboxRepository outbox;
  private final WorkflowRunStore workflowRuns;
  private final ConfigService configService;
  private final DingTalkTargetDirectory targetDirectory;
  private final ObjectMapper objectMapper;

  DingTalkStore(
      DingTalkBotStateRepository states,
      DingTalkWorkflowBindingRepository bindings,
      DingTalkInboundMessageRepository inboundMessages,
      DingTalkOutboxRepository outbox,
      WorkflowRunStore workflowRuns,
      ConfigService configService,
      DingTalkTargetDirectory targetDirectory,
      ObjectMapper objectMapper) {
    this.states = states;
    this.bindings = bindings;
    this.inboundMessages = inboundMessages;
    this.outbox = outbox;
    this.workflowRuns = workflowRuns;
    this.configService = configService;
    this.targetDirectory = targetDirectory;
    this.objectMapper = objectMapper;
  }

  @Transactional
  public void initialize(String clientId) {
    states.insertIfAbsent(clientId);
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
      String clientId, String taskDefinitionId, DingTalkModels.Message message) {
    Optional<DingTalkWorkflowBindingEntity> duplicate =
        bindings.findByClientIdAndTriggerMessageId(clientId, message.messageId());
    if (duplicate.isPresent()) {
      return new DingTalkModels.StartReservation("duplicate", duplicate.get().workflowId, null);
    }
    DingTalkBotStateEntity state =
        states
            .findForUpdate(clientId)
            .orElseThrow(() -> new IllegalStateException("钉钉机器人状态尚未初始化。"));
    if (state.activeWorkflowId != null) {
      return new DingTalkModels.StartReservation("busy", state.activeWorkflowId, null);
    }

    DingTalkTargetDirectory.TargetView target =
        configService.requireDingTalkTargetForTask(taskDefinitionId, clientId);
    if (!matches(target.targetType(), target.externalId(), message)) {
      return new DingTalkModels.StartReservation("unauthorized", null, null);
    }
    PreparedRun prepared = workflowRuns.prepareLatest(taskDefinitionId);
    Instant now = Instant.now();
    DingTalkWorkflowBindingEntity binding = new DingTalkWorkflowBindingEntity();
    binding.workflowId = prepared.workflowId();
    binding.clientId = clientId;
    binding.taskDefinitionId = taskDefinitionId;
    binding.triggerMessageId = message.messageId();
    binding.conversationId = message.conversationId();
    binding.targetType = target.targetType();
    binding.targetExternalId = target.externalId();
    binding.targetName = target.displayName();
    binding.rootMessageId = message.messageId();
    binding.initiatorUserId = message.senderUserId();
    binding.status = "submitting";
    binding.createdAt = now;
    binding.updatedAt = now;
    bindings.saveAndFlush(binding);
    state.activeWorkflowId = prepared.workflowId();
    state.updatedAt = now;
    return new DingTalkModels.StartReservation(
        "started", prepared.workflowId(), prepared.payload());
  }

  @Transactional(readOnly = true)
  public DingTalkTargetDirectory.TargetView configuredTarget(
      String clientId, String taskDefinitionId) {
    return configService.requireDingTalkTargetForTask(taskDefinitionId, clientId);
  }

  @Transactional
  public void discoverGroup(String clientId, DingTalkModels.Message message) {
    targetDirectory.discoverGroup(clientId, message.conversationId(), message.conversationTitle());
  }

  @Transactional(readOnly = true)
  public Optional<DingTalkModels.Binding> active(String clientId) {
    return states.findById(clientId).flatMap(state -> binding(state.activeWorkflowId));
  }

  @Transactional(readOnly = true)
  public Optional<DingTalkModels.Binding> binding(String workflowId) {
    if (workflowId == null) return Optional.empty();
    return bindings.findById(workflowId).map(DingTalkStore::toBinding);
  }

  @Transactional(readOnly = true)
  public Optional<DingTalkModels.Binding> conversation(
      String clientId, DingTalkModels.Message message) {
    String repliedMessageId = blankToNull(message.replyToMessageId());
    if (repliedMessageId == null) return Optional.empty();
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
    DingTalkBotStateEntity state = states.findForUpdate(clientId).orElseThrow();
    if (state.activeWorkflowId != null && !workflowId.equals(state.activeWorkflowId)) {
      return Optional.of(state.activeWorkflowId);
    }
    state.activeWorkflowId = workflowId;
    state.updatedAt = Instant.now();
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.status = "active";
    binding.updatedAt = Instant.now();
    return Optional.empty();
  }

  @Transactional
  public void releaseRestartReservation(String clientId, String workflowId) {
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    binding.status = "terminal";
    binding.updatedAt = Instant.now();
    releaseSlot(clientId, workflowId);
  }

  @Transactional
  public void reconcileRuntimeStatus(String clientId, String workflowId, String runtimeStatus) {
    DingTalkWorkflowBindingEntity binding = requiredBinding(workflowId);
    if (List.of("completed", "failed", "cancelled").contains(runtimeStatus)) {
      binding.status = "terminal";
      binding.updatedAt = Instant.now();
      releaseSlot(clientId, workflowId);
    } else if (List.of("queued", "running", "cancelling").contains(runtimeStatus)) {
      DingTalkBotStateEntity state = states.findForUpdate(clientId).orElseThrow();
      if (state.activeWorkflowId == null || workflowId.equals(state.activeWorkflowId)) {
        state.activeWorkflowId = workflowId;
        state.updatedAt = Instant.now();
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
    releaseSlot(clientId, workflowId);
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
    return bindings.findByClientIdAndStatusInOrderByCreatedAt(clientId, POLLED_STATUSES).stream()
        .filter(binding -> !"terminal".equals(binding.status) || binding.waitingAssistant)
        .map(DingTalkStore::toBinding)
        .toList();
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
      releaseSlot(clientId, workflowId);
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
    binding.waitingAssistant =
        inboundMessages.findAll().stream()
            .anyMatch(item -> workflowId.equals(item.workflowId) && "accepted".equals(item.status));
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

  @Transactional
  public List<DingTalkModels.Outbox> claimDue() {
    List<DingTalkModels.Outbox> claimed = new ArrayList<>();
    for (DingTalkOutboxEntity item :
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

  @Transactional(readOnly = true)
  public boolean isLatestCardOutbox(String id, String workflowId) {
    if (workflowId == null) return true;
    return outbox.findLatestCard(workflowId).map(item -> item.id.equals(id)).orElse(true);
  }

  @Transactional
  public void markOutboxSuperseded(String id) {
    DingTalkOutboxEntity item = outbox.findById(id).orElseThrow();
    item.status = "sent";
    item.lastError = null;
    item.updatedAt = Instant.now();
  }

  @Transactional
  public void markOutboxFailed(String id, RuntimeException error) {
    DingTalkOutboxEntity item = outbox.findById(id).orElseThrow();
    item.status = "failed";
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

  private void releaseSlot(String clientId, String workflowId) {
    DingTalkBotStateEntity state = states.findForUpdate(clientId).orElseThrow();
    if (workflowId.equals(state.activeWorkflowId)) {
      state.activeWorkflowId = null;
      state.updatedAt = Instant.now();
    }
  }

  private DingTalkWorkflowBindingEntity requiredBinding(String workflowId) {
    return bindings.findById(workflowId).orElseThrow();
  }

  private DingTalkWorkflowBindingEntity requiredBindingForUpdate(String workflowId) {
    return bindings.findForUpdate(workflowId).orElseThrow();
  }

  private DingTalkModels.Outbox toOutbox(DingTalkOutboxEntity item) {
    try {
      return new DingTalkModels.Outbox(
          item.id,
          item.workflowId,
          item.conversationId,
          item.targetType == null ? "GROUP" : item.targetType,
          item.targetExternalId == null ? item.conversationId : item.targetExternalId,
          item.replyToMessageId,
          item.messageKind,
          objectMapper.readTree(item.payloadJson));
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

  private static boolean matches(
      String targetType, String targetExternalId, DingTalkModels.Message message) {
    if ("PERSON".equals(targetType)) {
      return !"2".equals(message.conversationType())
          && targetExternalId.equals(message.senderUserId());
    }
    return "2".equals(message.conversationType())
        && targetExternalId.equals(message.conversationId());
  }

  private static String abbreviate(String value, int maxLength) {
    if (value == null) return "未知错误";
    return value.length() <= maxLength ? value : value.substring(0, maxLength);
  }
}
