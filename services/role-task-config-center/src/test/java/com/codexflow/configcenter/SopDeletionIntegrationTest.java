package com.codexflow.configcenter;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.NotFoundFailure;
import com.codexflow.configcenter.dto.RoleSaveRequest;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceContext;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.node.ObjectNode;

/** 验证 SOP 删除在任务软删除和历史外键存在时仍保持稳定。 */
@SpringBootTest
@Transactional
class SopDeletionIntegrationTest {

  @Autowired JdbcTemplate jdbc;
  @Autowired ConfigService service;
  @PersistenceContext EntityManager entityManager;

  /** 已软删除任务不再阻止 SOP 删除，SOP 本身保留为不可见的历史关联记录。 */
  @Test
  void deletesSopReferencedOnlyBySoftDeletedTask() {
    ObjectNode sop = createSop("历史任务引用的 SOP");
    String sopId = sop.path("id").asText();
    ObjectNode task =
        service.createTask(new TaskDefinitionSaveRequest("待删除任务", "验证 SOP 软删除", sopId, null, true));

    service.deleteTask(task.path("id").asText());
    service.deleteSop(sopId);
    entityManager.flush();

    assertThat(
            jdbc.queryForObject(
                "select deleted from codex_sop_sops where id = ?", Boolean.class, sopId))
        .isTrue();
    assertThat(
            jdbc.queryForObject(
                "select enabled from codex_sop_sops where id = ?", Boolean.class, sopId))
        .isFalse();
    assertThat(service.listSops("")).noneMatch(item -> sopId.equals(item.path("id").asText()));
    assertThatThrownBy(() -> service.getSop(sopId)).isInstanceOf(NotFoundFailure.class);
  }

  /** 有效任务仍引用 SOP 时继续拒绝删除，避免任务配置失效。 */
  @Test
  void rejectsDeletingSopReferencedByActiveTask() {
    ObjectNode sop = createSop("有效任务引用的 SOP");
    String sopId = sop.path("id").asText();
    service.createTask(new TaskDefinitionSaveRequest("有效任务", "验证删除保护", sopId, null, true));

    assertThatThrownBy(() -> service.deleteSop(sopId))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("任务定义引用");
    assertThat(service.getSop(sopId).path("id").asText()).isEqualTo(sopId);
  }

  /** SOP 软删除后，其历史步骤不再阻止角色删除。 */
  @Test
  void deletesRoleReferencedOnlyBySoftDeletedSop() {
    ObjectNode role = service.createRole(new RoleSaveRequest("待删除历史角色", "验证历史 SOP 引用", true, null));
    String roleId = role.path("id").asText();
    ObjectNode sop = createSop("引用待删除角色的 SOP", roleId);

    service.deleteSop(sop.path("id").asText());
    service.deleteRole(roleId);
    entityManager.flush();

    assertThat(
            jdbc.queryForObject(
                "select deleted from codex_sop_roles where id = ?", Boolean.class, roleId))
        .isTrue();
    assertThat(
            jdbc.queryForObject(
                "select enabled from codex_sop_roles where id = ?", Boolean.class, roleId))
        .isFalse();
    assertThat(service.listRoles("")).noneMatch(item -> roleId.equals(item.path("id").asText()));
    assertThatThrownBy(() -> service.deleteRole(roleId)).isInstanceOf(NotFoundFailure.class);
  }

  /** 未删除 SOP 仍引用角色时继续拒绝删除。 */
  @Test
  void rejectsDeletingRoleReferencedByActiveSop() {
    ObjectNode role =
        service.createRole(new RoleSaveRequest("有效 SOP 使用的角色", "验证角色删除保护", true, null));
    String roleId = role.path("id").asText();
    createSop("有效角色引用 SOP", roleId);

    assertThatThrownBy(() -> service.deleteRole(roleId))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("SOP 引用");
    assertThat(service.listRoles("有效 SOP 使用的角色"))
        .anyMatch(item -> roleId.equals(item.path("id").asText()));
  }

  private ObjectNode createSop(String name) {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    return createSop(name, roleId);
  }

  private ObjectNode createSop(String name, String roleId) {
    SopStepRequest step =
        new SopStepRequest(
            "执行步骤", roleId, "完成删除测试", null, "local", "local", null, false, null, null, 1800,
            Set.of(), Set.of());
    return service.createSop(new SopSaveRequest(name, null, null, null, true, List.of(step)));
  }
}
