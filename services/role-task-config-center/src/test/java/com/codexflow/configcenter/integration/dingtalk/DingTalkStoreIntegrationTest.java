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

  @Autowired DingTalkStore store;
  @Autowired ConfigService config;
  @Autowired JdbcTemplate jdbc;
  @Autowired ObjectMapper objectMapper;
  @Autowired DingTalkBotAdminService adminService;
  @Autowired DingTalkTargetDirectory targets;
  @Autowired TaskLaunchStore taskLaunches;
  @Autowired PlatformTransactionManager transactions;

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
  void oneTargetCannotBindTwoTaskDefinitions() {
    String clientId = "app-" + UUID.randomUUID();
    String sopId = createSop();
    String targetId = createGroupTarget(clientId, "chat-unique", "唯一绑定群");
    createBoundTask(sopId, targetId);

    assertThatThrownBy(() -> createBoundTask(sopId, targetId))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("已绑定其他任务定义");
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
  void rejectsStartFromGroupOtherThanSopTarget() {
    String clientId = "app-" + UUID.randomUUID();
    String taskId = createTask(clientId);
    store.initialize(clientId);
    DingTalkModels.Message wrongGroup =
        new DingTalkModels.Message(
            "wrong-group-trigger", "chat-2", "2", "user-2", "运行", true, false, null);

    assertThat(store.reserveStart(clientId, wrongGroup).outcome()).isEqualTo("unauthorized");
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
        store.claimDue().stream()
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

  private String createTask(String clientId) {
    String targetId = clientId == null ? null : createGroupTarget(clientId, "chat-1", "测试群");
    return createBoundTask(createSop(), targetId);
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
    return config
        .createTask(
            new TaskDefinitionSaveRequest(
                "钉钉任务-" + UUID.randomUUID(), "验证钉钉任务启动", sopId, null, true, targetId))
        .path("id")
        .asText();
  }

  private static DingTalkModels.Message message(String messageId) {
    return message(messageId, "chat-1");
  }

  private static DingTalkModels.Message message(String messageId, String conversationId) {
    return new DingTalkModels.Message(
        messageId, conversationId, "2", "user-1", "运行", true, false, null);
  }
}
