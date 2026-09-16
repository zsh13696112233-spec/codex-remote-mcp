package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.dto.*;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

@SpringBootTest(
    properties =
        "spring.datasource.url=jdbc:h2:mem:shared-groups;MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1")
@Transactional
class SharedGroupsIntegrationTest {
  static final String A = "00000000-0000-0000-0000-000000000001";
  static final String B = "00000000-0000-0000-0000-000000000002";
  @Autowired ConfigService configs;
  @Autowired GroupService groups;
  @Autowired WorkflowRunStore runs;
  @Autowired TaskScheduleStore schedules;
  @Autowired RunCatalogStore catalog;
  @Autowired TaskLaunchStore launches;
  @Autowired DingTalkTaskBindingDirectory directory;
  @Autowired JdbcTemplate jdbc;
  @MockitoBean GatewayClient gateway;

  @BeforeEach
  void setUp() {
    ObjectNode list = JsonNodeFactory.instance.objectNode();
    list.putArray("groups").addObject().put("id", A).put("name", "甲组").put("skillCount", 3);
    list.withArray("groups").addObject().put("id", B).put("name", "乙组");
    when(gateway.get("/agent-groups")).thenReturn(list);
    ObjectNode machines = JsonNodeFactory.instance.objectNode();
    machines
        .putArray("agents")
        .addObject()
        .put("agentId", "machine-a")
        .put("groupId", A)
        .putArray("capabilities")
        .add("supervisor")
        .add("executor");
    when(gateway.get("/agents")).thenReturn(machines);
    when(gateway.post(eq("/agents/validate"), any()))
        .thenAnswer(
            call -> {
              JsonNode body = call.getArgument(1);
              if (!A.equals(body.path("groupId").asText())) throw new ConflictFailure("机器分组不符。");
              return JsonNodeFactory.instance.objectNode();
            });
  }

  ObjectNode role(String group) {
    return configs.createRole(
        new RoleSaveRequest("角色" + UUID.randomUUID(), "职责", true, null, group));
  }

  @Test
  void skillCountsComeFromCentralDirectory() {
    assertThat(groups.catalog().path("groups").get(0).path("skillCount").asInt()).isEqualTo(3);
  }

  ObjectNode sop(String group, String role) {
    SopStepRequest step =
        new SopStepRequest(
            "步骤",
            role,
            "执行要求",
            null,
            "local",
            "machine-a",
            null,
            false,
            null,
            null,
            60,
            java.util.Set.of(),
            java.util.Set.of());
    return configs.createSop(
        new SopSaveRequest(
            "流程" + UUID.randomUUID(),
            null,
            "machine-a",
            60,
            null,
            true,
            10,
            "automatic",
            "legacy_text",
            null,
            List.of(step),
            group));
  }

  ObjectNode task(String group, String sop) {
    return configs.createTask(
        new TaskDefinitionSaveRequest(
            "任务" + UUID.randomUUID(),
            "目标",
            sop,
            null,
            true,
            null,
            false,
            null,
            null,
            null,
            false,
            group));
  }

  @Test
  void requiresGroupAndRejectsCrossGroupReferences() {
    assertThatThrownBy(() -> role(null)).isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> role(UUID.randomUUID().toString()))
        .isInstanceOf(ConflictFailure.class);
    String other = role(B).path("id").asText();
    assertThatThrownBy(() -> sop(A, other)).hasMessageContaining("同一分组");
    String r = role(A).path("id").asText(), s = sop(A, r).path("id").asText();
    assertThatThrownBy(() -> task(B, s)).hasMessageContaining("同一分组");
  }

  @Test
  void sharedCatalogCountsDisabledEntriesAndDualCapabilityMachineOnce() {
    ObjectNode r = role(A);
    String id = r.path("id").asText();
    configs.updateRole(
        id,
        new RoleSaveRequest(r.path("name").asText(), "职责", false, r.path("version").asLong(), A));
    ObjectNode count = groups.catalog();
    assertThat(count.path("groups").get(0).path("roleCount").asInt()).isEqualTo(1);
    assertThat(count.path("groups").get(0).path("machineCount").asInt()).isEqualTo(1);
    configs.deleteRole(id);
    entities.flush();
    assertThat(groups.catalog().path("groups").get(0).path("roleCount").asInt()).isZero();
    assertThatThrownBy(() -> groups.delete(A)).hasMessageContaining("引用");
    groups.delete(B);
    verify(gateway).delete("/agent-groups/" + B);
  }

  @Test
  void referencedRecordsCannotMove() {
    ObjectNode r = role(A), free = role(A);
    String id = r.path("id").asText();
    ObjectNode s = sop(A, id), t = task(A, s.path("id").asText());
    assertThatThrownBy(() -> configs.assignGroups("roles", List.of(id), B))
        .hasMessageContaining("引用");
    assertThatThrownBy(() -> configs.assignGroups("sops", List.of(s.path("id").asText()), B))
        .hasMessageContaining("引用");
    schedules.save(
        null,
        new TaskScheduleRequest(
            "每日", s.path("id").asText(), t.path("id").asText(), "daily", "09:00", null, true, A));
    assertThatThrownBy(() -> configs.assignGroups("tasks", List.of(t.path("id").asText()), B))
        .hasMessageContaining("定时规则");
    assertThatThrownBy(
            () ->
                groups.checkMachineMove(
                    "machine-a", JsonNodeFactory.instance.objectNode().put("groupId", B)))
        .hasMessageContaining("SOP");
  }

  @Test
  void freezesRunGroupAndLegacyRunsRemainUnassigned() {
    String r = role(A).path("id").asText(),
        s = sop(A, r).path("id").asText(),
        t = task(A, s).path("id").asText();
    String run = runs.prepareLatest(t, "schedule").workflowId();
    ObjectNode detail = runs.runDetail(run);
    assertThat(detail.path("groupId").asText()).isEqualTo(A);
    assertThat(detail.path("snapshot").path("groupName").asText()).isEqualTo("甲组");
    assertThat(detail.path("submittedJson").path("groupId").asText()).isEqualTo(A);
    assertThat(catalog.list("", "schedule", "", "", 0, 20, A).path("total").asInt()).isEqualTo(1);
    assertThat(catalog.list("", "schedule", "", "", 0, 20, B).path("total").asInt()).isZero();
    String retry = runs.prepareRetry(run).workflowId();
    assertThat(runs.runDetail(retry).path("groupName").asText()).isEqualTo("甲组");
    jdbc.update(
        "UPDATE codex_sop_task_runs SET group_id=NULL,group_name=NULL WHERE workflow_id=?", run);
    // Native migration updates must be observed outside the persistence-context cache.
    entities.flush();
    entities.clear();
    assertThatThrownBy(() -> runs.prepareRetry(run)).hasMessageContaining("旧运行未归组");
    assertThat(catalog.list("", "schedule", "", "", 0, 20, "unassigned").path("total").asInt())
        .isEqualTo(1);
  }

  @Autowired jakarta.persistence.EntityManager entities;

  @Test
  void oldConfigurationsPauseNewLaunchesUntilOrderedAssignment() {
    ObjectNode r = role(A), s = sop(A, r.path("id").asText()), t = task(A, s.path("id").asText());
    String taskId = t.path("id").asText();
    schedules.save(
        null,
        new TaskScheduleRequest("间隔", s.path("id").asText(), taskId, "interval", null, 5, true, A));
    entities.flush();
    for (String table : List.of("roles", "sops", "task_definitions"))
      jdbc.update("UPDATE codex_sop_" + table + " SET group_id=NULL");
    entities.clear();
    assertThatThrownBy(() -> runs.prepareLatest(taskId)).hasMessageContaining("选择有效分组");
    assertThatThrownBy(() -> directory.reserveNamed(t.path("name").asText()))
        .hasMessageContaining("选择有效分组");
    assertThatThrownBy(() -> configs.assignGroups("sops", List.of(s.path("id").asText()), A))
        .hasMessageContaining("同一分组");
    jdbc.update("UPDATE codex_task_schedules SET next_at=?", Instant.now().minusSeconds(20));
    assertThat(schedules.claim(ZonedDateTime.now(ZoneId.of("Asia/Shanghai")))).isEmpty();
    configs.assignGroups("roles", List.of(r.path("id").asText()), A);
    configs.assignGroups("sops", List.of(s.path("id").asText()), A);
    configs.assignGroups("tasks", List.of(taskId), A);
    assertThat(schedules.list("").get(0).get("available")).isEqualTo(true);
    assertThat(Instant.parse((String) schedules.list("").get(0).get("nextScheduleAt")))
        .isAfter(Instant.now());
    assertThat(runs.prepareLatest(taskId).workflowId()).isNotBlank();
  }
}
