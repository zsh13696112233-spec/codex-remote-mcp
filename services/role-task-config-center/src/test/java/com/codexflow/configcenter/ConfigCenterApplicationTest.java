package com.codexflow.configcenter;

import static org.assertj.core.api.Assertions.assertThat;

import com.codexflow.configcenter.domain.ConfigService;
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
  @Autowired Validator validator;

  /** 确认 Flyway 建表成功、默认角色已初始化且运行记录表初始为空。 */
  @Test
  void contextLoadsWithFlywaySchemaAndSeedData() {
    assertThat(jdbc.queryForObject("select count(*) from codex_sop_roles", Integer.class))
        .isEqualTo(3);
    assertThat(jdbc.queryForObject("select count(*) from codex_sop_task_runs", Integer.class))
        .isZero();
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
}
