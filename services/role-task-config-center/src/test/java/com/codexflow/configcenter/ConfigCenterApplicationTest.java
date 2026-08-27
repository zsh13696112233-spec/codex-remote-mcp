package com.codexflow.configcenter;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.domain.PreparedRun;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import com.codexflow.configcenter.dto.RoleSaveRequest;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import jakarta.validation.Validator;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import tools.jackson.databind.node.ObjectNode;

/** 验证配置中心的数据库迁移、JPA 关系加载和请求参数校验。 */
@SpringBootTest
class ConfigCenterApplicationTest {
  @Autowired JdbcTemplate jdbc;
  @Autowired ConfigService service;
  @Autowired WorkflowRunStore runStore;
  @Autowired Validator validator;

  /** 确认 Flyway 建表成功、默认角色已初始化且运行记录表初始为空。 */
  @Test
  void contextLoadsWithFlywaySchemaAndSeedData() {
    assertThat(jdbc.queryForObject("select count(*) from codex_sop_roles", Integer.class))
        .isEqualTo(3);
    assertThat(jdbc.queryForObject("select count(*) from codex_sop_task_runs", Integer.class))
        .isZero();
    assertThat(
            jdbc.queryForObject(
                "select count(*) from information_schema.columns "
                    + "where table_name = 'codex_sop_sops' and column_name = 'max_retry_count'",
                Integer.class))
        .isEqualTo(1);
    assertThat(
            jdbc.queryForObject(
                "select count(*) from information_schema.columns "
                    + "where table_name = 'codex_sop_sops' and column_name = 'advance_mode'",
                Integer.class))
        .isEqualTo(1);
    assertThat(
            jdbc.queryForObject(
                "select count(*) from information_schema.tables "
                    + "where table_name in ('codex_sop_feishu_bot_state', "
                    + "'codex_sop_feishu_workflow_bindings', "
                    + "'codex_sop_feishu_inbound_messages', 'codex_sop_feishu_outbox', "
                    + "'codex_sop_feishu_bot_settings', 'codex_sop_dingtalk_bot_state', "
                    + "'codex_sop_dingtalk_workflow_bindings', "
                    + "'codex_sop_dingtalk_inbound_messages', 'codex_sop_dingtalk_outbox', "
                    + "'codex_sop_dingtalk_bot_settings')",
                Integer.class))
        .isEqualTo(10);
  }

  /** 确认跨事务加载的 SOP、角色和任务关系可用于构建完整 API 快照。 */
  @Test
  void loadedRelationshipsAreAvailableWhenBuildingApiSnapshots() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "执行步骤", roleId, "完成测试步骤", null, null, "local", null, null, null, null, Set.of(),
            Set.of());
    SopSaveRequest sopBody = new SopSaveRequest("关系加载回归测试", null, null, null, null, List.of(step));
    ObjectNode createdSop = service.createSop(sopBody);

    TaskDefinitionSaveRequest taskBody =
        new TaskDefinitionSaveRequest(
            "关系加载任务", "验证任务关联的 SOP 可在新事务中加载", createdSop.path("id").asText(), null, null);
    ObjectNode createdTask = service.createTask(taskBody);

    ObjectNode loadedSop = service.getSop(createdSop.path("id").asText());
    ObjectNode loadedTask = service.getTask(createdTask.path("id").asText());
    assertThat(loadedSop.path("steps").get(0).path("roleId").asText()).isEqualTo(roleId);
    assertThat(loadedSop.path("steps").get(0).path("roleName").asText()).isNotBlank();
    assertThat(loadedTask.path("sopId").asText()).isEqualTo(createdSop.path("id").asText());
    assertThat(loadedTask.path("sopName").asText()).isEqualTo("关系加载回归测试");
  }

  /** 确认 Bean Validation 会拒绝名称和职责均为空的角色请求。 */
  @Test
  void requestValidationRejectsBlankRoleFields() {
    RoleSaveRequest request = new RoleSaveRequest(" ", "", null, null);

    assertThat(validator.validate(request))
        .extracting(violation -> violation.getPropertyPath().toString())
        .containsExactlyInAnyOrder("name", "duty");
  }

  /** 确认重跑额度会冻结到网关载荷，整项任务重试沿用上限但不会携带已使用次数。 */
  @Test
  void retryLimitIsValidatedAndFrozenIntoFreshRunPayloads() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "执行步骤", roleId, "完成测试步骤", null, null, "local", null, null, null, null, Set.of(),
            Set.of());
    SopSaveRequest sopBody = new SopSaveRequest("额度快照测试", null, null, null, true, 7, List.of(step));
    ObjectNode createdSop = service.createSop(sopBody);
    ObjectNode createdTask =
        service.createTask(
            new TaskDefinitionSaveRequest(
                "额度任务", "验证运行额度快照", createdSop.path("id").asText(), null, true));

    PreparedRun first = runStore.prepareLatest(createdTask.path("id").asText());
    PreparedRun retried = runStore.prepareRetry(first.workflowId());

    assertThat(first.payload().path("maxRetryCount").asInt()).isEqualTo(7);
    assertThat(retried.payload().path("maxRetryCount").asInt()).isEqualTo(7);
    assertThat(first.payload().path("advanceMode").asText()).isEqualTo("automatic");
    assertThat(first.payload().path("handoffMode").asText()).isEqualTo("cumulative_files");
    assertThat(retried.payload().path("handoffMode").asText()).isEqualTo("cumulative_files");
    assertThat(retried.workflowId()).isNotEqualTo(first.workflowId());
    assertThat(retried.payload().has("usedRetryCount")).isFalse();
    ObjectNode legacyPayload = first.payload().deepCopy();
    legacyPayload.remove("handoffMode");
    jdbc.update(
        "update codex_sop_task_runs set submitted_json = ? where workflow_id = ?",
        legacyPayload.toString(),
        first.workflowId());
    PreparedRun legacyRetry = runStore.prepareRetry(first.workflowId());
    assertThat(legacyRetry.payload().has("handoffMode")).isFalse();
    assertThatThrownBy(
            () ->
                service.createSop(
                    new SopSaveRequest("非法额度", null, null, null, true, 101, List.of(step))))
        .hasMessageContaining("maxRetryCount");
  }

  /** 确认半自动模式会保存并冻结到原快照重试。 */
  @Test
  void semiAutomaticAdvanceModeIsValidatedAndFrozen() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "执行步骤", roleId, "完成测试步骤", null, null, "local", null, null, null, null, Set.of(),
            Set.of());
    ObjectNode sop =
        service.createSop(
            new SopSaveRequest(
                "半自动快照", null, null, null, true, 10, "semi_automatic", List.of(step)));
    ObjectNode task =
        service.createTask(
            new TaskDefinitionSaveRequest(
                "半自动任务", "验证流转模式快照", sop.path("id").asText(), null, true));

    PreparedRun first = runStore.prepareLatest(task.path("id").asText());
    PreparedRun retried = runStore.prepareRetry(first.workflowId());
    assertThat(sop.path("advanceMode").asText()).isEqualTo("semi_automatic");
    assertThat(first.payload().path("advanceMode").asText()).isEqualTo("semi_automatic");
    assertThat(retried.payload().path("advanceMode").asText()).isEqualTo("semi_automatic");
    assertThatThrownBy(
            () ->
                service.createSop(
                    new SopSaveRequest(
                        "非法模式", null, null, null, true, 10, "manual", List.of(step))))
        .hasMessageContaining("advanceMode");
  }
}
