package com.codexflow.configcenter;

import static org.assertj.core.api.Assertions.*;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.dto.RoleSaveRequest;
import jakarta.persistence.EntityManager;
import jakarta.validation.Validator;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest
@Transactional
class RoleNameReuseIntegrationTest extends GroupedFixtureSupport {
  @Autowired ConfigService service;
  @Autowired EntityManager entityManager;
  @Autowired JdbcTemplate jdbc;
  @Autowired Validator validator;

  @Test
  void longDutyPersistsAndUpdatesWithoutTruncation() {
    String duty = "职责说明。".repeat(10000);
    var request = grouped(new RoleSaveRequest("长职责角色", duty, true, null));
    assertThat(validator.validate(request)).isEmpty();
    var role = service.createRole(request);
    entityManager.flush();
    entityManager.clear();
    String id = role.path("id").asText();
    assertThat(jdbc.queryForObject("SELECT duty FROM codex_sop_roles WHERE id=?", String.class, id))
        .isEqualTo(duty);
    String updated = "新" + duty.substring(1);
    service.updateRole(
        id, grouped(new RoleSaveRequest("长职责角色", updated, true, role.path("version").asLong())));
    entityManager.flush();
    entityManager.clear();
    assertThat(jdbc.queryForObject("SELECT duty FROM codex_sop_roles WHERE id=?", String.class, id))
        .isEqualTo(updated);
  }

  @Test
  void dutyRejectsOverLimitAndBlankText() {
    for (String duty : new String[] {"字".repeat(50001), " \n "}) {
      assertThat(validator.validate(grouped(new RoleSaveRequest("角色", duty, true, null))))
          .extracting(violation -> violation.getPropertyPath().toString())
          .containsExactly("duty");
    }
  }

  @Test
  void deletedNamesCanBeReusedRepeatedlyWithoutChangingHistory() {
    String name = "可重复使用角色";
    for (int i = 0; i < 2; i++) {
      var role = service.createRole(grouped(new RoleSaveRequest(name, "旧职责", true, null)));
      service.deleteRole(role.path("id").asText());
      entityManager.flush();
    }
    var replacement = service.createRole(grouped(new RoleSaveRequest(name, "新职责", false, null)));
    entityManager.flush();
    assertThat(service.listRoles(name)).hasSize(1);
    assertThat(
            jdbc.queryForObject(
                "SELECT COUNT(*) FROM codex_sop_roles WHERE name=? AND deleted=TRUE",
                Long.class,
                name))
        .isEqualTo(2L);
    assertThatThrownBy(
            () -> service.createRole(grouped(new RoleSaveRequest(name, "重复", true, null))))
        .isInstanceOf(ConflictFailure.class)
        .hasMessage("角色名称已存在。");
    service.updateRole(
        replacement.path("id").asText(),
        grouped(new RoleSaveRequest(name, "更新职责", true, replacement.path("version").asLong())));
  }

  @Test
  void renameCanUseDeletedNameButStillRejectsActiveNameIgnoringCase() {
    var old = service.createRole(grouped(new RoleSaveRequest("ReusableRole", "旧职责", true, null)));
    service.deleteRole(old.path("id").asText());
    entityManager.flush();
    var other = service.createRole(grouped(new RoleSaveRequest("OtherRole", "职责", true, null)));
    service.updateRole(
        other.path("id").asText(),
        grouped(new RoleSaveRequest("ReusableRole", "职责", true, other.path("version").asLong())));
    assertThatThrownBy(
            () ->
                service.createRole(grouped(new RoleSaveRequest("reusablerole", "职责", true, null))))
        .isInstanceOf(ConflictFailure.class);
  }
}
