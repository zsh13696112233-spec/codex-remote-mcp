package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.DingTalkTargetDirectory;
import com.codexflow.configcenter.domain.TaskLaunchStore;
import com.codexflow.configcenter.dto.DingTalkConfigSaveRequest;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 验证钉钉任务级并发锁、目标路由、消息幂等、事件游标和可靠发送状态。 */
@SpringBootTest
class DingTalkStoreIntegrationTest {

  private String lastTaskName;

  @Test
  void quotedProgressSurvivesHistoryLimitAndDuplicateDeliveryWithoutMergingDifferentGates() {
    String client = "quote-history-" + UUID.randomUUID();
    createTask(client);
    String workflow = store.reserveStart(client, message("quote-history-start")).workflowId();
    var original =
        new DingTalkModels.Message(
            "source", "quote-history-group", "2", "bob", "问题", true, false, null);
    String notice = "步骤「PA-20260902-145323-验收角色」：已完成";
    store.enqueueReply("quote-history-one", workflow, original, notice);
    store.enqueueReply("quote-history-two", workflow, original, notice);
    for (int i = 0; i < 205; i++)
      store.enqueueReply("quote-history-noise-" + i, workflow, original, "其他进度" + i);
    jdbc.update(
        "update codex_sop_dingtalk_outbox set status = 'sent' where workflow_id = ?", workflow);
    var reply =
        new DingTalkModels.Message(
            "reply",
            "quote-history-group",
            "2",
            "bob",
            "为什么",
            true,
            false,
            "unmatched-native-id",
            null,
            List.of(),
            null,
            "工作流编号：" + workflow + "\r\n" + notice);
    assertThat(store.conversation(client, reply))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(workflow);
    assertThat(store.conversation("another-client", reply)).isEmpty();
    var forged =
        new DingTalkModels.Message(
            "forged",
            "quote-history-group",
            "2",
            "bob",
            "问题",
            true,
            false,
            "unknown",
            null,
            List.of(),
            null,
            "工作流编号：" + workflow + "\n没有发送过的正文");
    assertThat(store.conversation(client, forged)).isEmpty();
    jdbc.update(
        "update codex_sop_dingtalk_outbox set advance_gate_id = 'old-gate', delivered_at = CURRENT_TIMESTAMP where dedup_key = 'quote-history-one'");
    jdbc.update(
        "update codex_sop_dingtalk_outbox set advance_gate_id = 'new-gate', delivered_at = CURRENT_TIMESTAMP where dedup_key = 'quote-history-two'");
    assertThat(store.conversation(client, reply)).isEmpty();
    assertThat(store.quotedAdvance(reply)).isNull();
  }

  @Test
  void waitingCompletionDeduplicatesByGateAndHeldReusesExistingCard() {
    String client = "merge-cards-" + UUID.randomUUID();
    createTask(client);
    String workflow = store.reserveStart(client, message("start-merge-cards")).workflowId();
    var snapshot = objectMapper.createObjectNode();
    var gate =
        snapshot
            .putObject("pendingAdvance")
            .put("gateId", "one")
            .put("state", "held")
            .put("completedNodeId", "first")
            .put("nextNodeId", "second");
    String result = "完整结果".repeat(1000);
    var nodes = snapshot.putArray("nodes");
    nodes.addObject().put("id", "first").put("displayName", "同名角色").put("response", result);
    nodes.addObject().put("id", "second").put("displayName", "同名角色");
    store.recordWaitingCard(workflow, 1, snapshot, true);
    store.recordWaitingCard(workflow, 2, snapshot, true);
    store.recordWaitingCard(workflow, 3, snapshot, false);
    var rows =
        jdbc.queryForList(
            "select payload_json from codex_sop_dingtalk_outbox where workflow_id = ? and message_kind = 'waiting_card'",
            String.class,
            workflow);
    assertThat(rows).hasSize(1);
    var payload = objectMapper.readTree(rows.get(0));
    assertThat(payload.path("text").asText()).contains(result);
    assertThat(payload.path("steps").asText()).contains("第1步「同名角色」", "第2步「同名角色」");
    assertThat(payload.path("retainResult").asBoolean()).isTrue();
    String id =
        jdbc.queryForObject(
            "select id from codex_sop_dingtalk_outbox where workflow_id = ? and message_kind = 'waiting_card'",
            String.class,
            workflow);
    store.markAdvanceDelivered(id, "carrier", java.time.Instant.now());
    store.markOutboxSent(id, "carrier");
    store.recordWaitingCard(workflow, 4, snapshot, false);
    assertThat(
            jdbc.queryForObject(
                "select count(*) from codex_sop_dingtalk_outbox where workflow_id = ? and message_kind = 'waiting_card'",
                Integer.class,
                workflow))
        .isEqualTo(1);
    gate.put("gateId", "two");
    store.recordWaitingCard(workflow, 5, snapshot, false);
    assertThat(
            jdbc.queryForObject(
                "select count(*) from codex_sop_dingtalk_outbox where workflow_id = ? and message_kind = 'waiting_card'",
                Integer.class,
                workflow))
        .isEqualTo(2);
    assertThat(store.binding(workflow).orElseThrow().eventCursor()).isEqualTo(5);
  }

  @Test
  void waitingCardsKeepMultipleAnswersAndQueueIndependentInvalidation() {
    String client = "cards-" + UUID.randomUUID();
    createTask(client);
    String workflow = store.reserveStart(client, message("start-cards")).workflowId();
    var snapshot = objectMapper.createObjectNode().put("name", "测试任务");
    snapshot.putObject("pendingAdvance").put("gateId", "gate-one").put("state", "held");
    store.recordWaitingCard(workflow, 1, snapshot, true);
    store.recordWaitingCard(workflow, 1, snapshot, true);
    var binding = store.binding(workflow).orElseThrow();
    for (int i = 0; i < 2; i++) {
      var question =
          new DingTalkModels.Message(
              "question-cards-" + i, "other-cards", "2", "asker", "问题", true, false, null);
      var inbound = store.registerInbound(client, binding, question);
      store.recordInputGate(question.messageId(), "gate-one");
      store.completeReply(workflow, inbound.workflowMessageId(), i + 2, "回答" + i, null, snapshot);
    }
    var ids =
        jdbc.queryForList(
            "select id from codex_sop_dingtalk_outbox where workflow_id = ? and message_kind = 'waiting_card'",
            String.class,
            workflow);
    assertThat(ids).hasSize(3);
    var late =
        new DingTalkModels.Message(
            "late-cards", "other-cards", "2", "asker", "旧问题", true, false, null);
    var lateInbound = store.registerInbound(client, binding, late);
    store.recordInputGate(late.messageId(), "gate-one");
    store.recordInputGate(late.messageId(), "gate-two");
    var nextSnapshot = snapshot.deepCopy();
    ((ObjectNode) nextSnapshot.path("pendingAdvance")).put("gateId", "gate-two");
    store.completeReply(
        workflow, lateInbound.workflowMessageId(), 4, "旧问题的迟到回答", null, nextSnapshot);
    assertThat(
            jdbc.queryForObject(
                "select message_kind from codex_sop_dingtalk_outbox where dedup_key = ?",
                String.class,
                "assistant:" + lateInbound.workflowMessageId()))
        .isEqualTo("reply");
    // 未投递的卡片不创建更新，避免邀请过期后更新不存在的远端实例。
    store.refreshWaitingCards(workflow, objectMapper.createObjectNode());
    assertThat(
            jdbc.queryForObject(
                "select count(*) from codex_sop_dingtalk_outbox where workflow_id = ? and message_kind = 'waiting_card_update'",
                Integer.class,
                workflow))
        .isZero();
    for (String id : ids) {
      store.markAdvanceDelivered(id, "carrier-" + id, java.time.Instant.now());
      store.markOutboxSent(id, "carrier-" + id);
    }
    String answerId =
        jdbc.queryForObject(
            "select id from codex_sop_dingtalk_outbox where workflow_id = ? and conversation_id = 'other-cards' order by created_at limit 1",
            String.class,
            workflow);
    var quote =
        new DingTalkModels.Message(
            "quote", "other-cards", "2", "asker", "继续", true, false, "wait-" + answerId);
    assertThat(store.quotedAdvance(quote)).isEqualTo("gate-one");
    var nativeQuote =
        new DingTalkModels.Message(
            "native-quote", "other-cards", "2", "asker", "问题", true, false, "carrier-" + answerId);
    assertThat(store.conversation(client, nativeQuote))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(workflow);
    assertThat(store.quotedAdvance(nativeQuote)).isEqualTo("gate-one");
    var twoIds =
        new DingTalkModels.Message(
            "native-two",
            "other-cards",
            "2",
            "asker",
            "问题",
            true,
            false,
            "unknown-top-level",
            null,
            List.of(),
            null,
            "",
            List.of("carrier-" + answerId));
    assertThat(store.conversation(client, twoIds))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(workflow);
    assertThat(store.quotedAdvance(twoIds)).isEqualTo("gate-one");
    String secondAnswerId =
        jdbc.queryForObject(
            "select id from codex_sop_dingtalk_outbox where workflow_id = ? and conversation_id = 'other-cards' and message_kind = 'waiting_card' and id <> ?",
            String.class,
            workflow,
            answerId);
    var conflictingIds =
        new DingTalkModels.Message(
            "native-conflict",
            "other-cards",
            "2",
            "asker",
            "问题",
            true,
            false,
            "carrier-" + answerId,
            null,
            List.of(),
            null,
            "",
            List.of("carrier-" + secondAnswerId));
    assertThatThrownBy(() -> store.conversation(client, conflictingIds))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("引用对应多条任务消息");
    var wrongGroup =
        new DingTalkModels.Message(
            "wrong-quote",
            "unrelated-group",
            "2",
            "asker",
            "问题",
            true,
            false,
            "carrier-" + answerId);
    assertThat(store.conversation(client, wrongGroup)).isEmpty();
    assertThat(store.conversation(client, quote))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(workflow);
    var action =
        new DingTalkModels.CardAction(
            "wait-" + answerId, "other-cards", "asker", "advance_confirm", java.util.Map.of());
    assertThat(store.ownsWaitingCard(action, workflow, "gate-one")).isTrue();
    assertThat(store.ownsWaitingCard(action, workflow, "gate-two")).isFalse();
    var rawQuote =
        objectMapper
            .createObjectNode()
            .put("msgId", "card-question")
            .put("conversationId", "other-cards")
            .put("conversationType", "2")
            .put("senderStaffId", "asker");
    var cardContent =
        rawQuote
            .putObject("text")
            .put("content", "这是为什么")
            .putObject("repliedMsg")
            .put("msgId", "unmatched-platform-id")
            .putObject("content")
            .putArray("cardContent");
    cardContent.addObject().put("value", "任务名称");
    var children = cardContent.addObject().putArray("children");
    children.addObject().put("value", "工作流编号：" + workflow);
    var parser = new OfficialDingTalkTransport(new DingTalkProperties(), objectMapper);
    var cardQuote = parser.toMessage(rawQuote.toString());
    assertThat(store.conversation(client, cardQuote))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(workflow);
    assertThat(store.quotedAdvance(cardQuote)).isNull();
    assertThat(store.conversation("other-client", cardQuote)).isEmpty();
    rawQuote.put("conversationId", "wrong-group");
    assertThat(store.conversation(client, parser.toMessage(rawQuote.toString()))).isEmpty();
    rawQuote.put("conversationId", "other-cards");
    children.addObject().put("value", "工作流编号：00000000-0000-4000-8000-000000000001");
    assertThatThrownBy(() -> store.conversation(client, parser.toMessage(rawQuote.toString())))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("多个工作流编号");
    var withoutConversation =
        new DingTalkModels.CardAction(
            "wait-" + answerId, null, "asker", "advance_confirm", java.util.Map.of());
    assertThat(store.ownsWaitingCard(withoutConversation, workflow, "gate-one")).isTrue();
    assertThat(store.ownsWaitingCard(withoutConversation, workflow, "gate-two")).isFalse();
    assertThat(store.ownsWaitingCard(withoutConversation, "other-workflow", "gate-one")).isFalse();
    assertThat(
            store.ownsWaitingCard(
                new DingTalkModels.CardAction(
                    "wait-" + answerId,
                    "wrong-group",
                    "asker",
                    "advance_confirm",
                    java.util.Map.of()),
                workflow,
                "gate-one"))
        .isFalse();
    assertThat(
            store.ownsWaitingCard(
                new DingTalkModels.CardAction(
                    "wait-" + answerId, null, null, "advance_confirm", java.util.Map.of()),
                workflow,
                "gate-one"))
        .isFalse();
    jdbc.update("update codex_sop_dingtalk_outbox set delivered_at = null where id = ?", answerId);
    assertThat(store.ownsWaitingCard(withoutConversation, workflow, "gate-one")).isFalse();
    store.markAdvanceDelivered(answerId, "carrier-" + answerId, java.time.Instant.now());
    jdbc.update(
        "update codex_sop_dingtalk_outbox set target_type = 'PERSON', target_external_id = 'asker' where id = ?",
        answerId);
    assertThat(store.ownsWaitingCard(withoutConversation, workflow, "gate-one")).isTrue();
    assertThat(
            store.ownsWaitingCard(
                new DingTalkModels.CardAction(
                    "wait-" + answerId, null, "other-user", "advance_confirm", java.util.Map.of()),
                workflow,
                "gate-one"))
        .isFalse();
    jdbc.update(
        "update codex_sop_dingtalk_outbox set target_type = 'GROUP', target_external_id = 'other-cards' where id = ?",
        answerId);
    store.refreshWaitingCards(workflow, objectMapper.createObjectNode());
    store.refreshWaitingCards(workflow, objectMapper.createObjectNode());
    assertThat(
            jdbc.queryForObject(
                "select count(*) from codex_sop_dingtalk_outbox where workflow_id = ? and message_kind = 'waiting_card_update'",
                Integer.class,
                workflow))
        .isEqualTo(3);
    assertThat(store.binding(workflow).orElseThrow().progressCardInstanceId()).isNull();
    // 关闭更新在投递前可能过时：保存实际展示状态，并允许后续真正关闭时重用更新记录。
    String source = ids.get(0);
    String update =
        jdbc.queryForObject(
            "select id from codex_sop_dingtalk_outbox where dedup_key = ?",
            String.class,
            "waiting-update:" + source + ":closed");
    store.markWaitingCardRefreshed("wait-" + source, "held");
    store.markOutboxSent(update, "wait-" + source);
    store.refreshWaitingCards(workflow, objectMapper.createObjectNode());
    assertThat(
            jdbc.queryForObject(
                "select status from codex_sop_dingtalk_outbox where id = ?", String.class, update))
        .isEqualTo("pending");
  }

  private final java.util.Map<String, String> namesByConversation = new java.util.HashMap<>();

  @Autowired DingTalkStore store;
  @Autowired ConfigService config;
  @Autowired JdbcTemplate jdbc;
  @Autowired ObjectMapper objectMapper;
  @Autowired DingTalkBotAdminService adminService;
  @Autowired DingTalkTargetDirectory targets;
  @Autowired TaskLaunchStore taskLaunches;
  @Autowired PlatformTransactionManager transactions;
  @Autowired jakarta.persistence.EntityManager entityManager;

  @Test
  void staleDingTalkPointerCannotOverwriteANewerWebRunOnRestart() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    String oldId = store.reserveStart(clientId, message("old-start")).workflowId();
    store.reconcileRuntimeStatus(clientId, oldId, "completed");
    String currentId = taskLaunches.reserveLatest(taskId).prepared().workflowId();
    jdbc.update(
        "update codex_sop_task_definitions set dingtalk_active_workflow_id = ? where id = ?",
        oldId,
        taskId);

    assertThat(store.acquireForRestart(clientId, oldId)).contains(currentId);
    assertThat(taskLaunches.activeWorkflowId(taskId)).contains(currentId);
    assertThat(store.binding(oldId).orElseThrow().status()).isEqualTo("terminal");
    store.reconcileRuntimeStatus(clientId, oldId, "completed");
    assertThat(taskLaunches.activeWorkflowId(taskId)).contains(currentId);
    assertThat(store.active(clientId, message("check"))).isEmpty();
  }

  @Test
  void advanceQuoteAndDeliveryTimeSurviveRetry() {
    String client = "advance-" + UUID.randomUUID();
    createTask(client);
    var start = message("advance-start");
    String workflow =
        store
            .reserveStart(
                client,
                new DingTalkModels.Message(
                    start.messageId(),
                    start.conversationId(),
                    "2",
                    "user-1",
                    start.content(),
                    true,
                    false,
                    null,
                    "群",
                    java.util.List.of(),
                    "https://oapi.dingtalk.com/robot/sendBySession?session=test"))
            .workflowId();
    store.recordAdvance(workflow, 1, "11111111111111111111111111111111", "第1步完成，请确认继续");
    var item =
        store.claimDue().stream()
            .filter(row -> workflow.equals(row.workflowId()))
            .findFirst()
            .orElseThrow();
    assertThat(item.payload().path("gateId").asText())
        .isEqualTo("11111111111111111111111111111111");
    assertThat(item.messageKind()).isEqualTo("reply");
    assertThat(item.payload().path("atUserId").asText()).isEqualTo("user-1");
    assertThat(item.payload().path("sessionWebhook").asText()).endsWith("session=test");
    var sentAt = java.time.Instant.now().truncatedTo(java.time.temporal.ChronoUnit.MICROS);
    store.markAdvanceDelivered(item.id(), "sent-advance", sentAt);
    store.markOutboxFailed(item.id(), new IllegalStateException("回执失败"));
    var callback =
        objectMapper
            .createObjectNode()
            .put("msgId", "reply-before-receipt")
            .put("conversationId", "chat-1");
    callback
        .putObject("text")
        .put("content", "确认继续")
        .putObject("repliedMsg")
        .put("content", item.payload().path("text").asText());
    var quotedBeforeReceipt =
        new OfficialDingTalkTransport(new DingTalkProperties(), objectMapper)
            .toMessage(callback.toString());
    assertThat(store.quotedAdvance(quotedBeforeReceipt))
        .isEqualTo("11111111111111111111111111111111");
    jdbc.update(
        "update codex_sop_dingtalk_outbox set next_attempt_at = ? where id = ?",
        java.time.Instant.now().minusSeconds(5),
        item.id());
    var retry =
        store.claimDue().stream()
            .filter(row -> row.id().equals(item.id()))
            .findFirst()
            .orElseThrow();
    assertThat(java.time.Instant.parse(retry.payload().path("deliveredAt").asText()))
        .isEqualTo(sentAt.truncatedTo(java.time.temporal.ChronoUnit.MICROS));
    store.markOutboxSent(item.id(), "sent-advance");
    var quoted =
        new DingTalkModels.Message(
            "quote-advance", "chat-1", "2", "user", "确认继续", true, false, "sent-advance");
    assertThat(store.quotedAdvance(quoted)).isEqualTo("11111111111111111111111111111111");
    assertThat(item.payload().path("text").asText()).contains("第1次等待确认");
    store.recordAdvance(workflow, 2, "22222222222222222222222222222222", "第1步完成，请确认继续");
    store.recordAdvance(workflow, 2, "22222222222222222222222222222222", "第1步完成，请确认继续");
    assertThat(store.quotedAdvance(quoted)).isEqualTo("11111111111111111111111111111111");
    String nextId =
        jdbc.queryForObject(
            "select id from codex_sop_dingtalk_outbox where workflow_id = ? and advance_gate_id = ?",
            String.class,
            workflow,
            "22222222222222222222222222222222");
    var next =
        store.claimDue().stream().filter(row -> nextId.equals(row.id())).findFirst().orElseThrow();
    assertThat(next.payload().path("text").asText()).contains("第2次等待确认");
    // 会话回复可能没有消息编号，两轮相同业务正文仍必须分别定位。
    store.markAdvanceDelivered(nextId, null, java.time.Instant.now());
    store.markOutboxSent(nextId, null);
    ((tools.jackson.databind.node.ObjectNode) callback.path("text").path("repliedMsg"))
        .put("content", next.payload().path("text").asText());
    var quotedNext =
        new OfficialDingTalkTransport(new DingTalkProperties(), objectMapper)
            .toMessage(callback.toString());
    assertThat(store.quotedAdvance(quotedNext)).isEqualTo("22222222222222222222222222222222");
    assertThat(store.quotedAdvance(quotedBeforeReceipt))
        .isEqualTo("11111111111111111111111111111111");
    assertThat(
            jdbc.queryForObject(
                "select count(*) from codex_sop_dingtalk_outbox where workflow_id = ?",
                Integer.class,
                workflow))
        .isEqualTo(2);
  }

  @Test
  void heldNoticeIsOneMentionedReplyAndDuplicateEventDoesNotResend() {
    String client = "held-" + UUID.randomUUID();
    createTask(client);
    var start = message("held-start");
    String workflow =
        store
            .reserveStart(
                client,
                new DingTalkModels.Message(
                    start.messageId(),
                    start.conversationId(),
                    "2",
                    "user-1",
                    start.content(),
                    true,
                    false,
                    null,
                    "群",
                    java.util.List.of(),
                    "https://oapi.dingtalk.com/robot/sendBySession?session=test"))
            .workflowId();
    store.recordHeld(workflow, 1, "任务已保持等待，不会自动进入下一步。");
    store.recordHeld(workflow, 1, "重复通知");
    assertThat(
            jdbc.queryForObject(
                "select count(*) from codex_sop_dingtalk_outbox where workflow_id = ?",
                Integer.class,
                workflow))
        .isEqualTo(1);
    var notice =
        store.claimDue().stream()
            .filter(row -> workflow.equals(row.workflowId()))
            .findFirst()
            .orElseThrow();
    assertThat(notice.messageKind()).isEqualTo("reply");
    assertThat(notice.payload().path("atUserId").asText()).isEqualTo("user-1");
    assertThat(notice.payload().path("text").asText())
        .contains("已保持等待")
        .doesNotContain("查看上方消息", "重复通知");
    store.markOutboxSent(notice.id(), "held-sent");
  }

  @Test
  void advanceWithoutSessionUsesTextAndSupervisorQueueKeepsEventTime() {
    String client = "advance-fallback-" + UUID.randomUUID();
    createTask(client);
    String workflow = store.reserveStart(client, message("fallback-start")).workflowId();
    store.recordAdvance(workflow, 1, "11111111111111111111111111111111", "请确认继续");
    var notice =
        store.claimDue().stream()
            .filter(row -> workflow.equals(row.workflowId()))
            .findFirst()
            .orElseThrow();
    assertThat(notice.messageKind()).isEqualTo("text");
    store.markOutboxSent(notice.id(), "fallback-sent");
    var event =
        objectMapper
            .createObjectNode()
            .put("source", "supervisor")
            .put("createdAt", "2026-09-09T06:33:10Z");
    store.recordProcess(workflow, null, 2, "执行进度", false, false, event);
    var process =
        store.claimDue().stream()
            .filter(row -> workflow.equals(row.workflowId()))
            .findFirst()
            .orElseThrow();
    assertThat(process.payload().path("executionEvent")).isEqualTo(event);
    store.markOutboxSent(process.id(), "process-sent");
    store.recordHeld(workflow, 3, "已保持等待");
    var held =
        store.claimDue().stream()
            .filter(row -> workflow.equals(row.workflowId()))
            .findFirst()
            .orElseThrow();
    assertThat(held.messageKind()).isEqualTo("text");
    store.markOutboxSent(held.id(), "held-fallback-sent");
  }

  @Test
  void acquiringAnExistingRunAlsoProtectsAnUnreconciledDingTalkOwner() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    String oldId = store.reserveStart(clientId, message("old-start")).workflowId();
    store.reconcileRuntimeStatus(clientId, oldId, "completed");
    String currentId = store.reserveStart(clientId, message("new-start")).workflowId();
    jdbc.update(
        "update codex_sop_task_definitions set active_workflow_id = null where id = ?", taskId);

    assertThat(taskLaunches.acquireExisting(taskId, oldId)).contains(currentId);
    assertThat(taskLaunches.activeWorkflowId(taskId)).contains(currentId);
    assertThat(taskLaunches.activeLaunches())
        .contains(new TaskLaunchStore.ActiveLaunch(taskId, currentId));
    assertThatThrownBy(() -> taskLaunches.reserveLatest(taskId))
        .isInstanceOf(ConflictFailure.class);
  }

  @Test
  void releasingAndReservingInOneTransactionDoesNotReuseStaleJpaState() {
    String taskId = createTask("app-" + UUID.randomUUID());
    String currentId =
        new TransactionTemplate(transactions)
            .execute(
                status -> {
                  String oldId = taskLaunches.reserveLatest(taskId).prepared().workflowId();
                  taskLaunches.release(oldId);
                  String nextId = taskLaunches.reserveLatest(taskId).prepared().workflowId();
                  assertThat(nextId).isNotEqualTo(oldId);
                  return nextId;
                });
    assertThat(taskLaunches.activeWorkflowId(taskId)).contains(currentId);
  }

  @Test
  void concurrentWebAndDingTalkStartsShareOneTaskReservation() throws Exception {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    CountDownLatch start = new CountDownLatch(1);
    ExecutorService executor = Executors.newFixedThreadPool(2);
    try {
      Future<String> web =
          executor.submit(
              () -> {
                start.await();
                try {
                  return taskLaunches.reserveLatest(taskId).prepared().workflowId();
                } catch (ConflictFailure busy) {
                  return null;
                }
              });
      Future<DingTalkModels.StartReservation> bot =
          executor.submit(
              () -> {
                start.await();
                return store.reserveStart(clientId, message("concurrent-start"));
              });
      start.countDown();
      String webId = web.get(10, TimeUnit.SECONDS);
      DingTalkModels.StartReservation botRun = bot.get(10, TimeUnit.SECONDS);
      assertThat(botRun.outcome()).isEqualTo(webId == null ? "started" : "busy");
      String owner = webId == null ? botRun.workflowId() : webId;
      assertThat(taskLaunches.activeWorkflowId(taskId)).contains(owner);
      assertThat(
              jdbc.queryForObject(
                  "select count(*) from codex_sop_task_runs where task_definition_id = ?",
                  Integer.class,
                  taskId))
          .isEqualTo(1);
      if (webId != null) assertThat(store.active(clientId, message("check"))).isEmpty();
    } finally {
      executor.shutdownNow();
    }
  }

  @Test
  void lateTerminalAndActiveReconciliationCannotReplaceANewerRun() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    String oldId = store.reserveStart(clientId, message("old-start")).workflowId();
    store.reconcileRuntimeStatus(clientId, oldId, "completed");
    String currentId = store.reserveStart(clientId, message("new-start")).workflowId();

    store.reconcileRuntimeStatus(clientId, oldId, "running");
    assertThat(store.binding(oldId).orElseThrow().status()).isEqualTo("terminal");
    store.reconcileRuntimeStatus(clientId, oldId, "completed");
    taskLaunches.release(oldId);

    assertThat(taskLaunches.activeWorkflowId(taskId)).contains(currentId);
    assertThat(store.active(clientId, message("check")))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(currentId);
  }

  @Test
  void restartAndNewWebRunCannotBothAcquireTheTask() throws Exception {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    String oldId = store.reserveStart(clientId, message("restart-source")).workflowId();
    store.reconcileRuntimeStatus(clientId, oldId, "completed");
    CountDownLatch start = new CountDownLatch(1);
    ExecutorService executor = Executors.newFixedThreadPool(2);
    try {
      Future<Boolean> restarted =
          executor.submit(
              () -> {
                start.await();
                return store.acquireForRestart(clientId, oldId).isEmpty();
              });
      Future<String> web =
          executor.submit(
              () -> {
                start.await();
                try {
                  return taskLaunches.reserveLatest(taskId).prepared().workflowId();
                } catch (ConflictFailure busy) {
                  return null;
                }
              });
      start.countDown();
      boolean acquired = restarted.get(10, TimeUnit.SECONDS);
      String webId = web.get(10, TimeUnit.SECONDS);
      assertThat(acquired).isEqualTo(webId == null);
      assertThat(taskLaunches.activeWorkflowId(taskId)).contains(acquired ? oldId : webId);
      if (acquired) {
        assertThat(store.acquireForRestart(clientId, oldId)).isEmpty();
        store.releaseRestartReservation(clientId, oldId);
        assertThat(taskLaunches.activeWorkflowId(taskId)).isEmpty();
        assertThat(store.active(clientId, message("check"))).isEmpty();
      } else {
        assertThat(store.active(clientId, message("check"))).isEmpty();
      }
    } finally {
      executor.shutdownNow();
    }
  }

  @Test
  void rolledBackBotStartLeavesNeitherTaskOccupancyNorNotificationBinding() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    new TransactionTemplate(transactions)
        .executeWithoutResult(
            status -> {
              assertThat(store.reserveStart(clientId, message("rolled-back")).outcome())
                  .isEqualTo("started");
              status.setRollbackOnly();
            });

    assertThat(taskLaunches.activeWorkflowId(taskId)).isEmpty();
    assertThat(store.active(clientId, message("check"))).isEmpty();
    assertThat(
            jdbc.queryForObject(
                "select count(*) from codex_sop_task_runs where task_definition_id = ?",
                Integer.class,
                taskId))
        .isZero();
  }

  @Test
  void pageSettingsPersistWithoutReturningTheSecret() {
    String clientId = "cli_" + UUID.randomUUID().toString().replace("-", "");
    DingTalkBotAdminService.ConfigView saved =
        adminService.save(
            new DingTalkConfigSaveRequest(
                false, clientId, "secret-value", "progress.schema", 1500L));

    assertThat(saved.clientId()).isEqualTo(clientId);
    assertThat(saved.cardTemplateId()).isEqualTo("progress.schema");
    assertThat(saved.eventPollIntervalMs()).isEqualTo(1500L);
    assertThat(saved.secretConfigured()).isTrue();
    assertThat(saved.persisted()).isTrue();
    assertThat(objectMapper.valueToTree(saved).has("clientSecret")).isFalse();
    assertThat(
            jdbc.queryForObject(
                "select client_secret from codex_sop_dingtalk_bot_settings where id = 1",
                String.class))
        .isEqualTo("secret-value");

    DingTalkBotAdminService.ConfigView updated =
        adminService.save(new DingTalkConfigSaveRequest(false, clientId, "", "", 2000L));
    assertThat(updated.cardTemplateId()).isEmpty();
    assertThat(updated.eventPollIntervalMs()).isEqualTo(2000L);
    assertThat(
            jdbc.queryForObject(
                "select client_secret from codex_sop_dingtalk_bot_settings where id = 1",
                String.class))
        .isEqualTo("secret-value");
  }

  @Test
  void duplicateTriggerAndConcurrentStartsKeepOneActiveWorkflow() throws Exception {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    store.initialize(clientId);
    CountDownLatch start = new CountDownLatch(1);
    ExecutorService executor = Executors.newFixedThreadPool(2);
    try {
      Future<DingTalkModels.StartReservation> first =
          executor.submit(
              () -> {
                start.await();
                return store.reserveStart(clientId, message("m-1"));
              });
      Future<DingTalkModels.StartReservation> second =
          executor.submit(
              () -> {
                start.await();
                return store.reserveStart(clientId, message("m-2"));
              });
      start.countDown();
      List<DingTalkModels.StartReservation> results = List.of(first.get(), second.get());
      assertThat(results)
          .extracting(DingTalkModels.StartReservation::outcome)
          .containsExactlyInAnyOrder("started", "busy");
      DingTalkModels.StartReservation started =
          results.stream()
              .filter(value -> "started".equals(value.outcome()))
              .findFirst()
              .orElseThrow();
      assertThat(store.active(clientId, message("active-check")))
          .get()
          .extracting(DingTalkModels.Binding::workflowId)
          .isEqualTo(started.workflowId());
      String triggerMessageId =
          jdbc.queryForObject(
              "select trigger_message_id from codex_sop_dingtalk_workflow_bindings where workflow_id = ?",
              String.class,
              started.workflowId());
      assertThat(store.reserveStart(clientId, message(triggerMessageId)).outcome())
          .isEqualTo("duplicate");
    } finally {
      executor.shutdownNow();
    }
  }

  @Test
  void differentTargetsSharingOneSopCanRunAndReleaseIndependently() {
    String clientId = "app-" + UUID.randomUUID();
    String sopId = createSop();
    String firstTarget = createGroupTarget(clientId, "chat-a", "甲群");
    String secondTarget = createGroupTarget(clientId, "chat-b", "乙群");
    createBoundTask(sopId, firstTarget);
    createBoundTask(sopId, secondTarget);
    DingTalkModels.Message firstMessage = message("multi-a", "chat-a");
    DingTalkModels.Message secondMessage = message("multi-b", "chat-b");

    DingTalkModels.StartReservation first = store.reserveStart(clientId, firstMessage);
    DingTalkModels.StartReservation second = store.reserveStart(clientId, secondMessage);

    assertThat(first.outcome()).isEqualTo("started");
    assertThat(second.outcome()).isEqualTo("started");
    assertThat(first.workflowId()).isNotEqualTo(second.workflowId());
    assertThat(store.active(clientId, firstMessage))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(first.workflowId());
    assertThat(store.active(clientId, secondMessage))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(second.workflowId());

    store.recordEvent(clientId, first.workflowId(), 1, null, null, null, null, true);

    assertThat(store.active(clientId, firstMessage)).isEmpty();
    assertThat(store.active(clientId, secondMessage))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(second.workflowId());
  }

  @Test
  void webRunWithoutProactiveNotificationCanQueueUnboundBusyReply() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    TaskLaunchStore.LaunchReservation web = taskLaunches.reserveLatest(taskId);
    DingTalkModels.Message message = message("busy-after-web");

    DingTalkModels.StartReservation busy = store.reserveStart(clientId, message);

    assertThat(busy.outcome()).isEqualTo("busy");
    assertThat(busy.workflowId()).isEqualTo(web.prepared().workflowId());
    assertThat(store.binding(busy.workflowId())).isEmpty();

    store.enqueueTargetText(
        "busy-after-web",
        null,
        message.conversationId(),
        "GROUP",
        message.conversationId(),
        message.messageId(),
        "当前绑定已有任务运行，任务编号：" + busy.workflowId());

    DingTalkModels.Outbox outgoing =
        store.claimDue().stream()
            .filter(item -> message.messageId().equals(item.replyToMessageId()))
            .findFirst()
            .orElseThrow();
    assertThat(outgoing.workflowId()).isNull();
    assertThat(outgoing.payload().path("text").asText()).contains(busy.workflowId());
    store.markOutboxSent(outgoing.id(), "busy-after-web-reply");
    taskLaunches.release(web.prepared().workflowId());
  }

  @Test
  void oneTargetCanNotifyMultipleTaskDefinitions() {
    String clientId = "app-" + UUID.randomUUID();
    String sopId = createSop();
    String targetId = createGroupTarget(clientId, "chat-unique", "唯一绑定群");
    createBoundTask(sopId, targetId);

    assertThat(createBoundTask(sopId, targetId)).isNotBlank();
  }

  @Test
  void markdownProgressOutboxPersistsBuiltInMessagePayload() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    store.initialize(clientId);
    DingTalkModels.StartReservation reservation =
        store.reserveStart(clientId, message("markdown-trigger"));
    store.markSubmitted(reservation.workflowId());

    store.enqueueProgressMarkdown(
        "markdown-progress-" + reservation.workflowId(),
        reservation.workflowId(),
        "任务进度",
        "**状态：** 运行中");

    DingTalkModels.Outbox item = store.claimDue().get(0);
    assertThat(item.messageKind()).isEqualTo("markdown");
    assertThat(item.replyToMessageId()).isEqualTo("markdown-trigger");
    assertThat(item.payload().path("title").asText()).isEqualTo("任务进度");
    assertThat(item.payload().path("text").asText()).isEqualTo("**状态：** 运行中");
    store.markOutboxSent(item.id(), "markdown-message-1");
  }

  @Test
  void proactiveWebBindingHasNoInboundRootAndUsesTheFrozenTarget() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    TaskLaunchStore.LaunchReservation launch = taskLaunches.reserveLatest(taskId);

    store.reserveProactive(clientId, taskId, launch.prepared().workflowId(), "web");
    DingTalkModels.Binding binding = store.binding(launch.prepared().workflowId()).orElseThrow();

    assertThat(binding.triggerSource()).isEqualTo("web");
    assertThat(binding.rootMessageId()).isNull();
    assertThat(binding.targetType()).isEqualTo("GROUP");
    assertThat(binding.targetExternalId()).isEqualTo("chat-1");

    store.markSubmitted(binding.workflowId());
    store.enqueueProgressMarkdown(
        "proactive-progress-" + binding.workflowId(), binding.workflowId(), "任务进度", "**状态：** 等待开始");
    DingTalkModels.Outbox outgoing =
        store.claimDue().stream()
            .filter(item -> binding.workflowId().equals(item.workflowId()))
            .findFirst()
            .orElseThrow();
    assertThat(outgoing.replyToMessageId()).isNull();
    store.markOutboxSent(outgoing.id(), "proactive-message-1");

    store.recordEvent(clientId, binding.workflowId(), 1, null, null, null, null, true);
    assertThat(
            jdbc.queryForObject(
                "select active_workflow_id from codex_sop_task_definitions where id = ?",
                String.class,
                taskId))
        .isNull();
  }

  @Test
  void anyGroupCanStartNamedTask() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    store.initialize(clientId);
    DingTalkModels.Message wrongGroup =
        new DingTalkModels.Message(
            "wrong-group-trigger", "chat-2", "2", "user-2", lastTaskName, true, false, null);

    assertThat(store.reserveStart(clientId, wrongGroup).outcome()).isEqualTo("started");
    assertThat(store.active(clientId, wrongGroup)).isEmpty();
  }

  @Test
  void inboundUuidEventCursorOutboxAndTerminalReleaseAreDurable() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    store.initialize(clientId);
    DingTalkModels.StartReservation reservation =
        store.reserveStart(clientId, message("trigger-1"));
    store.markSubmitted(reservation.workflowId());
    DingTalkModels.Binding binding = store.binding(reservation.workflowId()).orElseThrow();

    DingTalkModels.Message question =
        new DingTalkModels.Message(
            "question-1", "chat-1", "2", "user-2", "现在进度如何", false, false, "trigger-1");
    DingTalkModels.Inbound first = store.registerInbound(clientId, binding, question);
    DingTalkModels.Inbound retried = store.registerInbound(clientId, binding, question);
    assertThat(retried.workflowMessageId()).isEqualTo(first.workflowMessageId());
    assertThat(first.workflowMessageId()).matches("[0-9a-f-]{36}");
    store.markInboundFinished(binding.workflowId(), first.workflowMessageId(), false);
    assertThat(store.registerInbound(clientId, binding, question).status()).isEqualTo("completed");
    assertThat(store.binding(binding.workflowId()))
        .get()
        .extracting(DingTalkModels.Binding::waitingAssistant)
        .isEqualTo(false);

    ObjectNode payload = objectMapper.createObjectNode().put("text", "完成回复");
    assertThat(
            store.recordEvent(
                clientId,
                binding.workflowId(),
                10,
                "event-10",
                "text",
                question.messageId(),
                payload,
                false))
        .isTrue();
    assertThat(
            store.recordEvent(
                clientId,
                binding.workflowId(),
                10,
                "event-10",
                "text",
                question.messageId(),
                payload,
                false))
        .isFalse();
    List<DingTalkModels.Outbox> claimed = store.claimDue();
    assertThat(claimed).hasSize(1);
    store.markOutboxSent(claimed.get(0).id(), "bot-message-1");
    DingTalkModels.Message replyToBot =
        new DingTalkModels.Message(
            "question-2", "chat-1", "2", "user-3", "结果呢", false, false, "bot-message-1");
    assertThat(store.conversation(clientId, replyToBot))
        .get()
        .extracting(DingTalkModels.Binding::workflowId)
        .isEqualTo(binding.workflowId());
    assertThat(store.binding(binding.workflowId()))
        .get()
        .extracting(DingTalkModels.Binding::eventCursor)
        .isEqualTo(10L);

    store.recordEvent(clientId, binding.workflowId(), 11, null, null, null, null, true);
    assertThat(store.active(clientId, message("after-terminal"))).isEmpty();
    assertThat(store.binding(binding.workflowId()))
        .get()
        .extracting(DingTalkModels.Binding::status)
        .isEqualTo("terminal");
    assertThat(store.conversation(clientId, question)).isPresent();
  }

  @Test
  void assistantReplyAndItsCardRefreshArePersistedAtomically() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    store.initialize(clientId);
    DingTalkModels.StartReservation reservation =
        store.reserveStart(clientId, message("assistant-card-trigger"));
    store.markSubmitted(reservation.workflowId());

    ObjectNode text = objectMapper.createObjectNode().put("text", "质量审查正在执行。");
    ObjectNode card = objectMapper.createObjectNode().put("markdown", "**最新助手回复**\n质量审查正在执行。");
    assertThat(
            store.recordAssistantCompleted(
                reservation.workflowId(),
                8,
                "assistant-text-" + reservation.workflowId(),
                "question-1",
                text,
                "质量审查正在执行。",
                "assistant-card-" + reservation.workflowId(),
                card))
        .isTrue();
    assertThat(
            store.recordAssistantCompleted(
                reservation.workflowId(),
                8,
                "assistant-text-" + reservation.workflowId(),
                "question-1",
                text,
                "重复回复",
                "assistant-card-" + reservation.workflowId(),
                card))
        .isFalse();

    assertThat(store.latestAssistantReply(reservation.workflowId())).contains("质量审查正在执行。");
    assertThat(store.binding(reservation.workflowId()))
        .get()
        .extracting(DingTalkModels.Binding::eventCursor)
        .isEqualTo(8L);
    List<DingTalkModels.Outbox> messages =
        drainOutbox().stream()
            .filter(item -> reservation.workflowId().equals(item.workflowId()))
            .toList();
    assertThat(messages)
        .extracting(DingTalkModels.Outbox::messageKind)
        .containsExactlyInAnyOrder("text", "card");
    assertThat(messages.stream().filter(item -> "card".equals(item.messageKind())).findFirst())
        .get()
        .satisfies(
            item ->
                assertThat(item.payload().path("card").path("markdown").asText())
                    .contains("最新助手回复"));
  }

  private String createTask() {
    return createTask(null);
  }

  @Test
  void namedTaskNeedsNoNotificationTarget() {
    String taskId = createTask();
    var result = store.reserveStart("unbound-app", message("unbound-start"));
    assertThat(result.outcome()).isEqualTo("started");
    assertThat(taskLaunches.activeWorkflowId(taskId)).contains(result.workflowId());
  }

  @Test
  void crossGroupReplyDoesNotMoveProactiveNotificationTarget() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    String workflowId = taskLaunches.reserveLatest(taskId).prepared().workflowId();
    store.reserveProactive(clientId, taskId, workflowId, "web");
    store.markSubmitted(workflowId);
    var question =
        new DingTalkModels.Message(
            "cross-" + UUID.randomUUID(),
            "another-group",
            "2",
            "bob",
            "为什么",
            true,
            false,
            null,
            "另一个群",
            List.of(),
            "https://oapi.dingtalk.com/robot/sendBySession?session=test");
    store.ensureConversation(clientId, workflowId, taskId, question, "running");
    var incoming = store.registerInbound(clientId, store.route(workflowId, question), question);
    store.completeReply(workflowId, incoming.workflowMessageId(), 10, "回答", "action-1");
    var outgoing =
        store.claimDue().stream()
            .filter(item -> workflowId.equals(item.workflowId()))
            .findFirst()
            .orElseThrow();
    assertThat(outgoing.targetExternalId()).isEqualTo("another-group");
    assertThat(outgoing.payload().path("atUserId").asText()).isEqualTo("bob");
    assertThat(outgoing.payload().path("text").asText()).contains(workflowId, "回答");
    assertThat(store.binding(workflowId).orElseThrow().targetExternalId()).isEqualTo("chat-1");
    assertThat(store.binding(workflowId).orElseThrow().triggerSource()).isEqualTo("web");
    store.markOutboxSent(outgoing.id(), "real-reply-id");
    var quoted =
        new DingTalkModels.Message(
            "quote", "another-group", "2", "bob", "确认执行", true, false, "real-reply-id");
    assertThat(store.conversation(clientId, quoted).orElseThrow().workflowId())
        .isEqualTo(workflowId);
    assertThat(store.quotedAction(quoted)).isEqualTo("action-1");
    // Session replies may return no usable message ID: parsed nested text must match the sent
    // reply.
    var callback = objectMapper.createObjectNode();
    callback.put("msgId", "nested-question").put("conversationId", "another-group");
    var text = callback.putObject("text").put("content", "是谁创建的").put("isReplyMsg", true);
    text.putObject("repliedMsg")
        .put("msgId", "unavailable-reply-id")
        .putObject("content")
        .put("text", outgoing.payload().path("text").asText());
    var parsed =
        new OfficialDingTalkTransport(new DingTalkProperties(), objectMapper)
            .toMessage(callback.toString());
    assertThat(store.conversation(clientId, parsed).orElseThrow().workflowId())
        .isEqualTo(workflowId);
    assertThat(store.quotedAction(parsed)).isEqualTo("action-1");
    assertThat(store.conversation("another-client", parsed)).isEmpty();
    callback.put("conversationId", "unrelated-group");
    var unrelated =
        new OfficialDingTalkTransport(new DingTalkProperties(), objectMapper)
            .toMessage(callback.toString());
    assertThat(store.conversation(clientId, unrelated)).isEmpty();
    assertThat(store.quotedAction(unrelated)).isNull();
  }

  private String createTask(String clientId) {
    String targetId = clientId == null ? null : createGroupTarget(clientId, "chat-1", "测试群");
    return createBoundTask(createSop(), targetId);
  }

  @Test
  void processRepliesKeepConversationAndInsertionOrderEvenWithEqualTimestamps() {
    String client = "ordered-" + UUID.randomUUID();
    createTask(client);
    new TransactionTemplate(transactions)
        .executeWithoutResult(
            transaction -> {
              var start =
                  new DingTalkModels.Message(
                      "start-" + client,
                      "launch-group",
                      "2",
                      "alice",
                      lastTaskName,
                      true,
                      false,
                      null,
                      "启动群",
                      List.of(),
                      "https://oapi.dingtalk.com/robot/sendBySession?session=launch");
              String workflow = store.reserveStart(client, start).workflowId();
              var question =
                  new DingTalkModels.Message(
                      "ask-" + client,
                      "question-group",
                      "2",
                      "bob",
                      "检查",
                      true,
                      false,
                      null,
                      "提问群",
                      List.of(),
                      "https://oapi.dingtalk.com/robot/sendBySession?session=question");
              var inbound =
                  store.registerInbound(client, store.route(workflow, question), question);
              store.recordProcess(workflow, null, 1, "步骤开始", false, false);
              store.recordProcess(workflow, inbound.workflowMessageId(), 2, "工具开始", false, false);
              store.recordProcess(workflow, inbound.workflowMessageId(), 3, "思考摘要", false, false);
              store.recordProcess(workflow, inbound.workflowMessageId(), 3, "重复事件", false, false);
              store.completeReply(workflow, inbound.workflowMessageId(), 4, "最终回答", null);
              store.recordProcess(workflow, null, 5, "任务完成", true, true);
              jdbc.update(
                  "update codex_sop_dingtalk_outbox set created_at = '2026-01-01 00:00:00' where workflow_id = ?",
                  workflow);
              var replies =
                  drainOutbox().stream()
                      .filter(item -> workflow.equals(item.workflowId()))
                      .toList();
              assertThat(replies).hasSize(6);
              assertThat(
                      replies.stream().map(item -> item.payload().path("text").asText()).toList())
                  .containsExactly(
                      "工作流编号：" + workflow + "\n步骤开始",
                      "工作流编号：" + workflow + "\n工具开始",
                      "工作流编号：" + workflow + "\n思考摘要",
                      "工作流编号：" + workflow + "\n最终回答",
                      "工作流编号：" + workflow + "\n任务完成",
                      "工作流编号：" + workflow + "\n任务状态已更新，请查看上方消息。");
              assertThat(replies.stream().map(DingTalkModels.Outbox::targetExternalId).toList())
                  .containsExactly(
                      "launch-group",
                      "question-group",
                      "question-group",
                      "question-group",
                      "launch-group",
                      "launch-group");
              assertThat(replies.get(0).messageKind()).isEqualTo("text");
              assertThat(replies.get(4).messageKind()).isEqualTo("text");
              assertThat(replies.get(4).payload().has("sessionWebhook")).isFalse();
              assertThat(replies.get(0).payload().path("atUserId").asText()).isEmpty();
              assertThat(replies.get(3).payload().path("atUserId").asText()).isEqualTo("bob");
              assertThat(replies.get(5).payload().path("atUserId").asText()).isEqualTo("alice");
              replies.forEach(item -> store.markOutboxSent(item.id(), "sent-" + item.id()));
              store.markOutboxFailed(replies.get(5).id(), new IllegalStateException("会话已过期"));
              entityManager.flush();
              assertThat(
                      jdbc.queryForObject(
                          "select status from codex_sop_dingtalk_outbox where id = ?",
                          String.class,
                          replies.get(4).id()))
                  .isEqualTo("sent");
              transaction.setRollbackOnly();
            });
  }

  private List<DingTalkModels.Outbox> drainOutbox() {
    var delivered = new java.util.ArrayList<DingTalkModels.Outbox>();
    for (int i = 0; i < 200; i++) {
      var batch = store.claimDue(1);
      if (batch.isEmpty()) return delivered;
      batch.forEach(item -> store.markOutboxSent(item.id(), "sent-" + item.id()));
      delivered.addAll(batch);
    }
    throw new AssertionError("发送队列未在测试上限内清空");
  }

  @Test
  @org.springframework.transaction.annotation.Transactional
  void failedOrSendingHeadBlocksOnlyItsWorkflowAndConversation() {
    String client = "ordered-" + UUID.randomUUID();
    createTask();
    String firstWorkflow = store.reserveStart(client, message("start-" + client)).workflowId();
    createTask();
    String otherWorkflow = store.reserveStart(client, message("other-" + client)).workflowId();
    String group = "group-" + client;
    store.enqueueTargetText(client + "1", firstWorkflow, group, "GROUP", group, null, "开始");
    store.enqueueTargetText(client + "2", firstWorkflow, group, "GROUP", group, null, "完成");
    store.enqueueTargetText(client + "3", otherWorkflow, group, "GROUP", group, null, "其他任务");
    store.enqueueTargetText(
        client + "4", firstWorkflow, group + "2", "GROUP", group + "2", null, "其他群");
    var batch =
        store.claimDue().stream().filter(item -> item.conversationId().startsWith(group)).toList();
    assertThat(batch)
        .extracting(item -> item.payload().path("text").asText())
        .containsExactly("开始", "其他任务", "其他群");
    assertThat(store.claimDue()).noneMatch(item -> group.equals(item.conversationId()));
    var first = batch.get(0);
    store.markOutboxFailed(first.id(), new IllegalStateException("临时失败"));
    entityManager.flush();
    jdbc.update(
        "update codex_sop_dingtalk_outbox set next_attempt_at = '2099-01-01 00:00:00' where id = ?",
        first.id());
    assertThat(store.claimDue()).noneMatch(item -> group.equals(item.conversationId()));
    jdbc.update(
        "update codex_sop_dingtalk_outbox set next_attempt_at = '2000-01-01 00:00:00' where id = ?",
        first.id());
    entityManager.clear();
    var retried =
        store.claimDue().stream().filter(item -> group.equals(item.conversationId())).toList();
    assertThat(retried).extracting(DingTalkModels.Outbox::id).containsExactly(first.id());
    store.markOutboxSent(first.id(), "sent");
    assertThat(
            store.claimDue().stream().filter(item -> group.equals(item.conversationId())).toList())
        .extracting(item -> item.payload().path("text").asText())
        .containsExactly("完成");
  }

  @Test
  @org.springframework.transaction.annotation.Transactional
  void unboundChunksRemainOrderedAfterRecoveryAndAbandonedReplyDoesNotBlock() {
    String group = "chunks-" + UUID.randomUUID();
    String body = "甲".repeat(850) + "乙".repeat(850) + "丙".repeat(300);
    store.enqueueTargetText(group, null, group, "GROUP", group, null, body);
    var first =
        store.claimDue().stream()
            .filter(item -> group.equals(item.conversationId()))
            .findFirst()
            .orElseThrow();
    assertThat(first.payload().path("text").asText()).isEqualTo("甲".repeat(850));
    assertThat(store.claimDue()).noneMatch(item -> group.equals(item.conversationId()));
    store.initialize("app");
    var recovered =
        store.claimDue().stream()
            .filter(item -> group.equals(item.conversationId()))
            .findFirst()
            .orElseThrow();
    assertThat(recovered.id()).isEqualTo(first.id());
    store.markOutboxSent(first.id(), "sent");
    var rest = drainOutbox().stream().filter(item -> group.equals(item.conversationId())).toList();
    assertThat(rest)
        .extracting(item -> item.payload().path("text").asText())
        .containsExactly("乙".repeat(850), "丙".repeat(300));

    store.enqueueReply(
        group + "reply",
        null,
        new DingTalkModels.Message(group, group, "2", "user", "问题", true, false, null),
        "会话回复");
    store.enqueueTargetText(group + "next", null, group, "GROUP", group, null, "后续正文");
    var reply =
        store.claimDue().stream()
            .filter(item -> group.equals(item.conversationId()))
            .findFirst()
            .orElseThrow();
    assertThat(reply.messageKind()).isEqualTo("reply");
    store.markOutboxFailed(reply.id(), new IllegalStateException("会话已过期"));
    assertThat(
            store.claimDue().stream().filter(item -> group.equals(item.conversationId())).toList())
        .extracting(item -> item.payload().path("text").asText())
        .containsExactly("后续正文");
  }

  @Test
  @org.springframework.transaction.annotation.Transactional
  void finishingMessagesKeepsWaitingUntilTheLastAcceptedMessageAndFiltersPolling() {
    String client = "poll-" + UUID.randomUUID();
    createTask();
    String workflow = store.reserveStart(client, message("start-" + client)).workflowId();
    var binding = store.binding(workflow).orElseThrow();
    var first = store.registerInbound(client, binding, message("first-" + client));
    var second = store.registerInbound(client, binding, message("second-" + client));
    store.reconcileRuntimeStatus(client, workflow, "completed");
    assertThat(store.pollable(client))
        .extracting(DingTalkModels.Binding::workflowId)
        .containsExactly(workflow);
    store.markInboundFinished(workflow, first.workflowMessageId(), false);
    assertThat(store.binding(workflow).orElseThrow().waitingAssistant()).isTrue();
    store.markInboundFinished(workflow, second.workflowMessageId(), true);
    assertThat(store.binding(workflow).orElseThrow().waitingAssistant()).isFalse();
    assertThat(store.pollable(client)).isEmpty();
    // 其他工作流的未完成消息不能让已完成工作流继续轮询。
    createTask();
    String other = store.reserveStart(client, message("other-" + client)).workflowId();
    store.registerInbound(client, store.binding(other).orElseThrow(), message("pending-" + client));
    store.markInboundFinished(workflow, first.workflowMessageId(), false);
    assertThat(store.pollable(client))
        .extracting(DingTalkModels.Binding::workflowId)
        .containsExactly(other);
    store.markSubmitted(other);
    assertThat(store.pollable(client))
        .extracting(DingTalkModels.Binding::workflowId)
        .containsExactly(other);
    assertThat(store.pollable(client + "unrelated")).isEmpty();
  }

  private String createSop() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "钉钉步骤", roleId, "完成测试", null, null, "local", null, null, null, null, null, Set.of(),
            Set.of());
    ObjectNode sop =
        config.createSop(
            new SopSaveRequest(
                "钉钉SOP-" + UUID.randomUUID(),
                null,
                "local",
                null,
                null,
                true,
                3,
                "semi_automatic",
                null,
                null,
                List.of(step)));
    return sop.path("id").asText();
  }

  private String createGroupTarget(String clientId, String conversationId, String name) {
    DingTalkTargetDirectory.TargetView target =
        targets.discoverGroup(clientId, conversationId, name);
    return targets.update(clientId, target.id(), target.displayName(), true).id();
  }

  private String createBoundTask(String sopId, String targetId) {
    lastTaskName = "钉钉任务-" + UUID.randomUUID();
    String conversationId =
        targetId == null
            ? "chat-1"
            : jdbc.queryForObject(
                "select external_id from codex_sop_dingtalk_targets where id = ?",
                String.class,
                targetId);
    namesByConversation.put(conversationId, lastTaskName);
    return config
        .createTask(
            new TaskDefinitionSaveRequest(lastTaskName, "验证钉钉任务启动", sopId, null, true, targetId))
        .path("id")
        .asText();
  }

  private DingTalkModels.Message message(String messageId) {
    return message(messageId, "chat-1");
  }

  private DingTalkModels.Message message(String messageId, String conversationId) {
    return new DingTalkModels.Message(
        messageId,
        conversationId,
        "2",
        "user-1",
        namesByConversation.getOrDefault(conversationId, lastTaskName),
        true,
        false,
        null);
  }
}
