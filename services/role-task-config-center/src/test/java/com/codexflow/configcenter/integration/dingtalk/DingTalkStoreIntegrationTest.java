package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import com.codexflow.configcenter.domain.ConfigService;
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
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 验证钉钉单任务锁、消息幂等、事件游标和可靠发送状态。 */
@SpringBootTest
class DingTalkStoreIntegrationTest {

  @Autowired DingTalkStore store;
  @Autowired ConfigService config;
  @Autowired JdbcTemplate jdbc;
  @Autowired ObjectMapper objectMapper;
  @Autowired DingTalkBotAdminService adminService;

  @Test
  void pageSettingsPersistWithoutReturningTheSecret() {
    String taskId = createTask();
    String clientId = "cli_" + UUID.randomUUID().toString().replace("-", "");
    DingTalkBotAdminService.ConfigView saved =
        adminService.save(
            new DingTalkConfigSaveRequest(
                false, clientId, "secret-value", taskId, "progress.schema", 1500L));

    assertThat(saved.clientId()).isEqualTo(clientId);
    assertThat(saved.taskDefinitionId()).isEqualTo(taskId);
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
        adminService.save(
            new DingTalkConfigSaveRequest(false, clientId, "", taskId, "progress.schema", 2000L));
    assertThat(updated.eventPollIntervalMs()).isEqualTo(2000L);
    assertThat(
            jdbc.queryForObject(
                "select client_secret from codex_sop_dingtalk_bot_settings where id = 1",
                String.class))
        .isEqualTo("secret-value");
  }

  @Test
  void duplicateTriggerAndConcurrentStartsKeepOneActiveWorkflow() throws Exception {
    String taskId = createTask();
    String clientId = "app-" + UUID.randomUUID();
    store.initialize(clientId);
    CountDownLatch start = new CountDownLatch(1);
    ExecutorService executor = Executors.newFixedThreadPool(2);
    try {
      Future<DingTalkModels.StartReservation> first =
          executor.submit(
              () -> {
                start.await();
                return store.reserveStart(clientId, taskId, message("m-1"));
              });
      Future<DingTalkModels.StartReservation> second =
          executor.submit(
              () -> {
                start.await();
                return store.reserveStart(clientId, taskId, message("m-2"));
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
      assertThat(store.active(clientId))
          .get()
          .extracting(DingTalkModels.Binding::workflowId)
          .isEqualTo(started.workflowId());
      String triggerMessageId =
          jdbc.queryForObject(
              "select trigger_message_id from codex_sop_dingtalk_workflow_bindings where workflow_id = ?",
              String.class,
              started.workflowId());
      assertThat(store.reserveStart(clientId, taskId, message(triggerMessageId)).outcome())
          .isEqualTo("duplicate");
    } finally {
      executor.shutdownNow();
    }
  }

  @Test
  void inboundUuidEventCursorOutboxAndTerminalReleaseAreDurable() {
    String taskId = createTask();
    String clientId = "app-" + UUID.randomUUID();
    store.initialize(clientId);
    DingTalkModels.StartReservation reservation =
        store.reserveStart(clientId, taskId, message("trigger-1"));
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
    assertThat(store.active(clientId)).isEmpty();
    assertThat(store.binding(binding.workflowId()))
        .get()
        .extracting(DingTalkModels.Binding::status)
        .isEqualTo("terminal");
    assertThat(store.conversation(clientId, question)).isPresent();
  }

  private String createTask() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "钉钉步骤", roleId, "完成测试", null, null, "local", null, null, null, null, Set.of(),
            Set.of());
    ObjectNode sop =
        config.createSop(
            new SopSaveRequest(
                "钉钉SOP-" + UUID.randomUUID(),
                null,
                null,
                null,
                true,
                3,
                "semi_automatic",
                List.of(step)));
    return config
        .createTask(
            new TaskDefinitionSaveRequest(
                "钉钉任务-" + UUID.randomUUID(), "验证钉钉任务启动", sop.path("id").asText(), null, true))
        .path("id")
        .asText();
  }

  private static DingTalkModels.Message message(String messageId) {
    return new DingTalkModels.Message(messageId, "chat-1", "2", "user-1", "运行", true, false, null);
  }
}
