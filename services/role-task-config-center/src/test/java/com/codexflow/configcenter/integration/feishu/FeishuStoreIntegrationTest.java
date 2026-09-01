package com.codexflow.configcenter.integration.feishu;

import static org.assertj.core.api.Assertions.assertThat;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.dto.FeishuConfigSaveRequest;
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

/** 验证飞书单任务锁、消息幂等、事件游标和可靠发送状态。 */
@SpringBootTest
class FeishuStoreIntegrationTest {

  @Autowired FeishuStore store;
  @Autowired ConfigService config;
  @Autowired JdbcTemplate jdbc;
  @Autowired ObjectMapper objectMapper;
  @Autowired FeishuBotAdminService adminService;

  @Test
  void pageSettingsPersistWithoutReturningTheSecret() {
    String taskId = createTask();
    String appId = "cli_" + UUID.randomUUID().toString().replace("-", "");
    FeishuBotAdminService.ConfigView saved =
        adminService.save(new FeishuConfigSaveRequest(false, appId, "secret-value", taskId, 1500L));

    assertThat(saved.appId()).isEqualTo(appId);
    assertThat(saved.taskDefinitionId()).isEqualTo(taskId);
    assertThat(saved.eventPollIntervalMs()).isEqualTo(1500L);
    assertThat(saved.secretConfigured()).isTrue();
    assertThat(saved.persisted()).isTrue();
    assertThat(objectMapper.valueToTree(saved).has("appSecret")).isFalse();
    assertThat(
            jdbc.queryForObject(
                "select app_secret from codex_sop_feishu_bot_settings where id = 1", String.class))
        .isEqualTo("secret-value");

    FeishuBotAdminService.ConfigView updated =
        adminService.save(new FeishuConfigSaveRequest(false, appId, "", taskId, 2000L));
    assertThat(updated.eventPollIntervalMs()).isEqualTo(2000L);
    assertThat(
            jdbc.queryForObject(
                "select app_secret from codex_sop_feishu_bot_settings where id = 1", String.class))
        .isEqualTo("secret-value");
  }

  @Test
  void duplicateTriggerAndConcurrentStartsKeepOneActiveWorkflow() throws Exception {
    String taskId = createTask();
    String appId = "app-" + UUID.randomUUID();
    store.initialize(appId);
    CountDownLatch start = new CountDownLatch(1);
    ExecutorService executor = Executors.newFixedThreadPool(2);
    try {
      Future<FeishuModels.StartReservation> first =
          executor.submit(
              () -> {
                start.await();
                return store.reserveStart(appId, taskId, message("m-1"));
              });
      Future<FeishuModels.StartReservation> second =
          executor.submit(
              () -> {
                start.await();
                return store.reserveStart(appId, taskId, message("m-2"));
              });
      start.countDown();
      List<FeishuModels.StartReservation> results = List.of(first.get(), second.get());
      assertThat(results)
          .extracting(FeishuModels.StartReservation::outcome)
          .containsExactlyInAnyOrder("started", "busy");
      FeishuModels.StartReservation started =
          results.stream()
              .filter(value -> "started".equals(value.outcome()))
              .findFirst()
              .orElseThrow();
      assertThat(store.active(appId))
          .get()
          .extracting(FeishuModels.Binding::workflowId)
          .isEqualTo(started.workflowId());
      String triggerMessageId =
          jdbc.queryForObject(
              "select trigger_message_id from codex_sop_feishu_workflow_bindings where workflow_id = ?",
              String.class,
              started.workflowId());
      assertThat(store.reserveStart(appId, taskId, message(triggerMessageId)).outcome())
          .isEqualTo("duplicate");
    } finally {
      executor.shutdownNow();
    }
  }

  @Test
  void inboundUuidEventCursorOutboxAndTerminalReleaseAreDurable() {
    String taskId = createTask();
    String appId = "app-" + UUID.randomUUID();
    store.initialize(appId);
    FeishuModels.StartReservation reservation =
        store.reserveStart(appId, taskId, message("trigger-1"));
    store.markSubmitted(reservation.workflowId());
    FeishuModels.Binding binding = store.binding(reservation.workflowId()).orElseThrow();

    FeishuModels.Message question =
        new FeishuModels.Message(
            "question-1",
            "chat-1",
            "group",
            "user-2",
            "现在进度如何",
            false,
            false,
            "trigger-1",
            "thread-1",
            "trigger-1");
    FeishuModels.Inbound first = store.registerInbound(appId, binding, question);
    FeishuModels.Inbound retried = store.registerInbound(appId, binding, question);
    assertThat(retried.workflowMessageId()).isEqualTo(first.workflowMessageId());
    assertThat(first.workflowMessageId()).matches("[0-9a-f-]{36}");
    store.markInboundFinished(binding.workflowId(), first.workflowMessageId(), false);
    assertThat(store.registerInbound(appId, binding, question).status()).isEqualTo("completed");
    assertThat(store.binding(binding.workflowId()))
        .get()
        .extracting(FeishuModels.Binding::waitingAssistant)
        .isEqualTo(false);

    ObjectNode payload = objectMapper.createObjectNode().put("text", "完成回复");
    assertThat(
            store.recordEvent(
                appId,
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
                appId,
                binding.workflowId(),
                10,
                "event-10",
                "text",
                question.messageId(),
                payload,
                false))
        .isFalse();
    assertThat(store.claimDue()).hasSize(1);
    assertThat(store.binding(binding.workflowId()))
        .get()
        .extracting(FeishuModels.Binding::eventCursor)
        .isEqualTo(10L);

    store.recordEvent(appId, binding.workflowId(), 11, null, null, null, null, true);
    assertThat(store.active(appId)).isEmpty();
    assertThat(store.binding(binding.workflowId()))
        .get()
        .extracting(FeishuModels.Binding::status)
        .isEqualTo("terminal");
    assertThat(store.conversation(appId, question)).isPresent();
  }

  private String createTask() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "飞书步骤", roleId, "完成测试", null, null, "local", null, null, null, null, null, Set.of(),
            Set.of());
    ObjectNode sop =
        config.createSop(
            new SopSaveRequest(
                "飞书SOP-" + UUID.randomUUID(),
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
                "飞书任务-" + UUID.randomUUID(), "验证飞书任务启动", sop.path("id").asText(), null, true))
        .path("id")
        .asText();
  }

  private static FeishuModels.Message message(String messageId) {
    return new FeishuModels.Message(
        messageId, "chat-1", "group", "user-1", "运行", true, false, null, null, null);
  }
}
