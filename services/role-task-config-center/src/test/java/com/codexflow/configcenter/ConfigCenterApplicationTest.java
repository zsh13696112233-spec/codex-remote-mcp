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
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
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
                "select count(*) from information_schema.columns "
                    + "where table_name = 'codex_sop_sops' and column_name = 'handoff_mode'",
                Integer.class))
        .isEqualTo(1);
    assertThat(
            jdbc.queryForObject(
                "select count(*) from information_schema.columns "
                    + "where table_name = 'codex_sop_steps' and column_name = 'permission_profile'",
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
                    + "'codex_sop_dingtalk_bot_settings', 'codex_sop_dingtalk_targets', "
                    + "'codex_sop_dingtalk_departments', "
                    + "'codex_sop_dingtalk_target_departments')",
                Integer.class))
        .isEqualTo(13);
  }

  /** 确认跨事务加载的 SOP、角色和任务关系可用于构建完整 API 快照。 */
  @Test
  void loadedRelationshipsAreAvailableWhenBuildingApiSnapshots() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "执行步骤", roleId, "完成测试步骤", null, null, "local", null, null, null, null, null, Set.of(),
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

  /** 独立运行状态入口必须展示四类主监督状态，并通过配置中心代理定时刷新。 */
  @Test
  void staticPageIncludesStandaloneRuntimeStatusDashboard() throws IOException {
    String index = readClasspath("static/index.html");
    String script = readClasspath("static/app.js");

    assertThat(index).contains("data-page=\"runtime\">运行状态");
    assertThat(script)
        .contains("/api/gateway/ready")
        .contains("在线空闲")
        .contains("在线忙碌")
        .contains("离线")
        .contains("未知或停用")
        .contains("setInterval(refreshAgentRuntimeStatuses,10000)");
  }

  @Test
  void staticPageIncludesDingTalkTargetsAndTaskSingleSelection() throws IOException {
    String index = readClasspath("static/index.html");
    String script = readClasspath("static/app.js");

    assertThat(index).contains("data-page=\"dingtalk-targets\">钉钉通知对象");
    assertThat(index).contains("name=\"dingtalkTargetId\"");
    assertThat(script)
        .contains("/api/dingtalk/targets/sync-people")
        .contains("/api/dingtalk/targets/directory")
        .contains("data-department-toggle")
        .contains("data-person-search")
        .contains("人员启用状态已自动保存")
        .contains("dingtalkTargetId:f.dingtalkTargetId.value||null")
        .contains("首次同步的人员默认停用");
  }

  @Test
  void requestValidationRejectsInvalidSupervisorAgentId() {
    SopSaveRequest blank =
        new SopSaveRequest(
            "测试", null, " ", null, null, true, 10, "automatic", "legacy_text", List.of());
    SopSaveRequest tooLong =
        new SopSaveRequest(
            "测试",
            null,
            "s".repeat(129),
            null,
            null,
            true,
            10,
            "automatic",
            "legacy_text",
            List.of());

    assertThat(validator.validate(blank))
        .extracting(violation -> violation.getPropertyPath().toString())
        .contains("supervisorAgentId");
    assertThat(validator.validate(tooLong))
        .extracting(violation -> violation.getPropertyPath().toString())
        .contains("supervisorAgentId");
  }

  /** 主监督 ID 只做配置校验，不依赖网关在线，并冻结到新运行及历史重试。 */
  @Test
  @Transactional
  void arbitrarySupervisorAgentIdIsSavedUpdatedAndFrozenIntoRuns() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "执行步骤",
            roleId,
            "完成测试步骤",
            null,
            "remote",
            "executor-offline",
            null,
            false,
            "read_only",
            null,
            1800,
            Set.of(),
            Set.of());
    SopSaveRequest firstBody =
        new SopSaveRequest(
            "多主监督快照",
            null,
            "supervisor-offline-a",
            7200,
            null,
            true,
            10,
            "automatic",
            "cumulative_files",
            List.of(step));
    ObjectNode sop = service.createSop(firstBody);
    ObjectNode task =
        service.createTask(
            new TaskDefinitionSaveRequest(
                "多主监督任务", "验证主监督快照", sop.path("id").asText(), null, true));
    PreparedRun original = runStore.prepareLatest(task.path("id").asText());

    SopSaveRequest updatedBody =
        new SopSaveRequest(
            "多主监督快照",
            null,
            "supervisor-offline-b",
            7200,
            null,
            true,
            10,
            "automatic",
            "cumulative_files",
            List.of(step));
    ObjectNode updated = service.updateSop(sop.path("id").asText(), updatedBody);
    PreparedRun latest = runStore.prepareLatest(task.path("id").asText());
    PreparedRun historicalRetry = runStore.prepareRetry(original.workflowId());

    assertThat(sop.path("supervisorAgentId").asText()).isEqualTo("supervisor-offline-a");
    assertThat(updated.path("supervisorAgentId").asText()).isEqualTo("supervisor-offline-b");
    assertThat(original.payload().path("supervisorAgentId").asText())
        .isEqualTo("supervisor-offline-a");
    assertThat(latest.payload().path("supervisorAgentId").asText())
        .isEqualTo("supervisor-offline-b");
    assertThat(historicalRetry.payload().path("supervisorAgentId").asText())
        .isEqualTo("supervisor-offline-a");
    assertThat(latest.payload().path("nodes").get(0).path("executor").path("agentId").asText())
        .isEqualTo("executor-offline");
  }

  /** 确认权限档位会派生兼容写入字段、冻结到运行快照，并拒绝矛盾或非法值。 */
  @Test
  @Transactional
  void permissionProfileIsValidatedAndFrozenIntoRunPayload() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest autoReview =
        new SopStepRequest(
            "自动审核步骤",
            roleId,
            "完成权限测试",
            null,
            "local",
            "local",
            null,
            true,
            "auto_review",
            null,
            1800,
            Set.of(),
            Set.of());
    ObjectNode sop =
        service.createSop(
            new SopSaveRequest("权限档位测试", null, null, null, true, List.of(autoReview)));
    ObjectNode task =
        service.createTask(
            new TaskDefinitionSaveRequest(
                "权限档位任务", "验证权限档位快照", sop.path("id").asText(), null, true));
    PreparedRun run = runStore.prepareLatest(task.path("id").asText());
    PreparedRun retried = runStore.prepareRetry(run.workflowId());

    assertThat(sop.path("steps").get(0).path("permissionProfile").asText())
        .isEqualTo("auto_review");
    assertThat(sop.path("steps").get(0).path("writeEnabled").asBoolean()).isTrue();
    assertThat(run.payload().path("nodes").get(0).path("permissionProfile").asText())
        .isEqualTo("auto_review");
    assertThat(run.payload().path("nodes").get(0).path("write").asBoolean()).isTrue();
    assertThat(retried.payload().path("nodes").get(0).path("permissionProfile").asText())
        .isEqualTo("auto_review");

    SopStepRequest contradictory =
        new SopStepRequest(
            "矛盾步骤",
            roleId,
            "不会保存",
            null,
            "local",
            "local",
            null,
            true,
            "read_only",
            null,
            1800,
            Set.of(),
            Set.of());
    assertThatThrownBy(
            () ->
                service.createSop(
                    new SopSaveRequest("矛盾权限", null, null, null, true, List.of(contradictory))))
        .hasMessageContaining("矛盾");
    SopStepRequest unknown =
        new SopStepRequest(
            "非法步骤",
            roleId,
            "不会保存",
            null,
            "local",
            "local",
            null,
            null,
            "danger_full_access",
            null,
            1800,
            Set.of(),
            Set.of());
    assertThatThrownBy(
            () ->
                service.createSop(
                    new SopSaveRequest("非法权限", null, null, null, true, List.of(unknown))))
        .hasMessageContaining("permissionProfile");
  }

  /** 确认重跑额度会冻结到网关载荷，整项任务重试沿用上限但不会携带已使用次数。 */
  @Test
  void retryLimitIsValidatedAndFrozenIntoFreshRunPayloads() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "执行步骤", roleId, "完成测试步骤", null, null, "local", null, null, null, null, null, Set.of(),
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
    assertThat(createdSop.path("handoffMode").asText()).isEqualTo("legacy_text");
    assertThat(first.payload().path("handoffMode").asText()).isEqualTo("legacy_text");
    assertThat(retried.payload().path("handoffMode").asText()).isEqualTo("legacy_text");
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
            "执行步骤", roleId, "完成测试步骤", null, null, "local", null, null, null, null, null, Set.of(),
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

  /** 确认 SOP 可选择文字或文件交接，并将选择冻结到新运行和原快照重试。 */
  @Test
  void handoffModeIsValidatedAndFrozen() {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "执行步骤", roleId, "完成测试步骤", null, null, "local", null, null, null, null, null, Set.of(),
            Set.of());
    ObjectNode sop =
        service.createSop(
            new SopSaveRequest(
                "文字交接快照", null, null, null, true, 10, "automatic", "legacy_text", List.of(step)));
    ObjectNode task =
        service.createTask(
            new TaskDefinitionSaveRequest(
                "文字交接任务", "验证文字交接模式快照", sop.path("id").asText(), null, true));

    PreparedRun first = runStore.prepareLatest(task.path("id").asText());
    PreparedRun retried = runStore.prepareRetry(first.workflowId());
    assertThat(sop.path("handoffMode").asText()).isEqualTo("legacy_text");
    assertThat(first.payload().path("handoffMode").asText()).isEqualTo("legacy_text");
    assertThat(retried.payload().path("handoffMode").asText()).isEqualTo("legacy_text");
    assertThatThrownBy(
            () ->
                service.createSop(
                    new SopSaveRequest(
                        "非法交接模式",
                        null,
                        null,
                        null,
                        true,
                        10,
                        "automatic",
                        "unsupported",
                        List.of(step))))
        .hasMessageContaining("handoffMode");
  }

  private static String readClasspath(String path) throws IOException {
    try (var input = new ClassPathResource(path).getInputStream()) {
      return new String(input.readAllBytes(), StandardCharsets.UTF_8);
    }
  }
}
