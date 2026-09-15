package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import tools.jackson.databind.node.ObjectNode;

/** 验证每日与间隔配置、幂等领取和任务级单实例占用。 */
@SpringBootTest
class TaskScheduleIntegrationTest extends com.codexflow.configcenter.GroupedFixtureSupport {

  @Autowired RunCatalogStore catalog;
  @Autowired ConfigService config;

  @Test
  void catalogFiltersFrozenNameSourcesAndBeijingDates() {
    ObjectNode task = createTask(false, null, false);
    String id = task.path("id").asText(), name = task.path("name").asText();
    String scheduled = workflowRuns.prepareLatest(id, "schedule").workflowId();
    String manual = workflowRuns.prepareLatest(id, "dingtalk").workflowId();
    assertThat(
            catalog
                .list(name, "schedule", "", "", 0, 20)
                .path("items")
                .get(0)
                .path("submittedAt")
                .asText())
        .isEqualTo(workflowRuns.runDetail(scheduled).path("submittedAt").asText());
    workflowRuns.prepareLatest(id);
    String retry = workflowRuns.prepareRetry(scheduled).workflowId();
    jdbc.update(
        "update codex_sop_task_runs set submitted_at = ? where workflow_id = ?",
        Instant.parse("2026-09-13T16:00:00Z"),
        scheduled);
    jdbc.update(
        "update codex_sop_task_runs set submitted_at = ? where workflow_id = ?",
        Instant.parse("2026-09-14T16:00:00Z"),
        manual);
    jdbc.update(
        "update codex_sop_task_definitions set name = ?, deleted = true where id = ?", "已改名", id);
    var rows = catalog.list(name, "", "", "", 0, 20);
    assertThat(rows.path("total").asInt()).isEqualTo(2);
    assertThat(rows.path("items").toString())
        .doesNotContain(retry, "snapshot", "submittedJson")
        .contains(name);
    assertThat(
            catalog.list(name, "schedule", "2026-09-14", "2026-09-14", 0, 20).path("total").asInt())
        .isEqualTo(1);
    assertThat(
            catalog.list(name, "dingtalk", "2026-09-14", "2026-09-14", 0, 20).path("total").asInt())
        .isZero();
    assertThat(
            catalog.list(name, "dingtalk", "2026-09-15", "2026-09-15", 0, 20).path("total").asInt())
        .isEqualTo(1);
    assertThat(catalog.list("已改名", "", "", "", 0, 20).path("total").asInt()).isZero();
  }

  @Test
  void catalogPaginationIsStableAndNameWildcardsAreLiteral() {
    ObjectNode task = createTask(false, null, false);
    String id = task.path("id").asText();
    List<String> ids = new java.util.ArrayList<>();
    for (int i = 0; i < 23; i++) ids.add(workflowRuns.prepareLatest(id, "schedule").workflowId());
    String name = "测试%_!" + id;
    jdbc.update(
        "update codex_sop_task_runs set run_name = ?, submitted_at = ? where task_definition_id = ?",
        name,
        Instant.parse("2026-09-14T00:00:00Z"),
        id);
    var first = catalog.list(name, "", "", "", 0, 20);
    var second = catalog.list(name, "", "", "", 1, 20);
    assertThat(first.path("items").size()).isEqualTo(20);
    assertThat(second.path("items").size()).isEqualTo(3);
    List<String> actual = new java.util.ArrayList<>();
    first.path("items").forEach(row -> actual.add(row.path("workflowId").asText()));
    second.path("items").forEach(row -> actual.add(row.path("workflowId").asText()));
    assertThat(actual)
        .containsExactlyElementsOf(
            ids.stream().sorted(java.util.Comparator.reverseOrder()).toList());
    assertThat(catalog.list("测试%_!不存在", "", "", "", 0, 20).path("total").asInt()).isZero();
    assertThatThrownBy(() -> catalog.list("", "web", "", "", 0, 20))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> catalog.list("", "", "2026-02-30", "", 0, 20))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> catalog.list("", "", "2026-09-15", "2026-09-14", 0, 20))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> catalog.list("", "", "", "", -1, 20))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Autowired TaskScheduleStore schedules;
  @Autowired TaskLaunchStore launches;
  @Autowired JdbcTemplate jdbc;
  @Autowired WorkflowRunStore workflowRuns;

  @Test
  void historyProjectionIsPagedAndSnapshotsAreLoadedOnlyOnDetail() {
    String taskId = createTask(false, null, false).path("id").asText();
    List<String> ids = new java.util.ArrayList<>();
    for (int i = 0; i < 25; i++) ids.add(workflowRuns.prepareLatest(taskId).workflowId());
    // 相同时间也必须以工作流编号稳定排序。
    jdbc.update(
        "update codex_sop_task_runs set submitted_at = ? where task_definition_id = ?",
        Instant.parse("2026-09-05T00:00:00Z"),
        taskId);
    var first = workflowRuns.listRunSummaries(taskId, 0, 20);
    var second = workflowRuns.listRunSummaries(taskId, 1, 20);
    assertThat(first).hasSize(20);
    assertThat(second).hasSize(5);
    assertThat(first)
        .allSatisfy(
            row -> {
              assertThat(row.has("snapshot")).isFalse();
              assertThat(row.has("submittedJson")).isFalse();
              assertThat(row.has("gatewayResponse")).isFalse();
            });
    var all =
        java.util.stream.Stream.concat(first.stream(), second.stream())
            .map(row -> row.path("workflowId").asText())
            .toList();
    assertThat(all)
        .doesNotHaveDuplicates()
        .containsExactlyElementsOf(
            ids.stream().sorted(java.util.Comparator.reverseOrder()).toList());
    assertThat(
            workflowRuns
                .runDetail(ids.get(0))
                .path("submittedJson")
                .path("taskDefinitionId")
                .asText())
        .isEqualTo(taskId);
    assertThat(workflowRuns.pendingRuntimeScopes(taskId)).hasSize(25);
    workflowRuns.markRuntimeScopes(ids);
    assertThat(workflowRuns.pendingRuntimeScopes(taskId)).isEmpty();
    assertThatThrownBy(() -> workflowRuns.listRunSummaries(taskId, -1, 20))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void pendingHistoryCheckExcludesCurrentRunAndOtherTasks() {
    String taskId = createTask(false, null, false).path("id").asText();
    String current = workflowRuns.prepareLatest(taskId).workflowId();
    String otherTask = createTask(false, null, false).path("id").asText();
    workflowRuns.prepareLatest(otherTask);
    assertThat(workflowRuns.hasPendingRuntimeHistory(taskId, current)).isFalse();

    String old = workflowRuns.prepareLatest(taskId).workflowId();
    assertThat(workflowRuns.hasPendingRuntimeHistory(taskId, current)).isTrue();
    workflowRuns.markRuntimeScopes(List.of(old));
    assertThat(workflowRuns.hasPendingRuntimeHistory(taskId, current)).isFalse();
    assertThat(workflowRuns.pendingRuntimeScopes(taskId)).containsExactly(current);
  }

  @Test
  void oneTaskDefinitionKeepsOnlyOneActiveWorkflow() {
    String taskId = createTask(false, null, false).path("id").asText();
    TaskLaunchStore.LaunchReservation first = launches.reserveLatest(taskId);

    assertThatThrownBy(() -> launches.reserveLatest(taskId))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("当前任务仍在运行");

    launches.release(first.prepared().workflowId());
    TaskLaunchStore.LaunchReservation second = launches.reserveLatest(taskId);
    assertThat(second.prepared().workflowId()).isNotEqualTo(first.prepared().workflowId());
  }

  @Test
  void notificationRequiresASelectableDingTalkTarget() {
    assertThatThrownBy(() -> createTask(false, null, true))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("必须选择钉钉通知对象");
  }

  @Test
  void independentIntervalKeepsCadenceAndResetsOnlyOnRuleChanges() {
    ObjectNode task = createTask(false, null, false);
    String taskId = task.path("id").asText(), sopId = task.path("sopId").asText();
    var request =
        grouped(
            new com.codexflow.configcenter.dto.TaskScheduleRequest(
                "间隔", sopId, taskId, "interval", null, 40, true));
    var saved = schedules.save(null, request);
    String id = (String) saved.get("id");
    Instant initial = Instant.parse((String) saved.get("nextScheduleAt"));
    assertThat(Duration.between(Instant.now(), initial).toMinutes()).isBetween(39L, 40L);
    assertThat(
            schedules
                .save(
                    id,
                    grouped(
                        new com.codexflow.configcenter.dto.TaskScheduleRequest(
                            "改名", sopId, taskId, "interval", null, 40, true)))
                .get("nextScheduleAt"))
        .isEqualTo(saved.get("nextScheduleAt"));
    assertThatThrownBy(() -> schedules.save(null, request)).isInstanceOf(ConflictFailure.class);
    jdbc.update("update codex_task_schedules set enabled=false where id<>?", id);
    Instant due = Instant.parse("2030-01-01T00:00:00Z");
    jdbc.update("update codex_task_schedules set next_at=? where id=?", due, id);
    assertThat(schedules.claim(due.minusSeconds(1).atZone(ZoneId.of("Asia/Shanghai")))).isEmpty();
    assertThat(schedules.claim(due.plusSeconds(30).atZone(ZoneId.of("Asia/Shanghai"))))
        .containsExactly(taskId);
    assertThat(schedules.claim(due.plusSeconds(35).atZone(ZoneId.of("Asia/Shanghai")))).isEmpty();
    assertThat(
            jdbc.queryForObject(
                    "select next_at from codex_task_schedules where id=?",
                    java.sql.Timestamp.class,
                    id)
                .toInstant())
        .isEqualTo(due.plusSeconds(2400));
    assertThat(schedules.claim(due.plusSeconds(5000).atZone(ZoneId.of("Asia/Shanghai")))).isEmpty();
    schedules.save(
        id,
        grouped(
            new com.codexflow.configcenter.dto.TaskScheduleRequest(
                "间隔", sopId, taskId, "interval", null, 40, false)));
    assertThatThrownBy(() -> launches.reserveLatest(taskId, "schedule"))
        .isInstanceOf(ConflictFailure.class);
    assertThat(schedules.save(id, request).get("nextScheduleAt")).isNotNull();
    config.deleteTask(taskId);
    assertThat(
            schedules.list("间隔").stream()
                .filter(r -> r.get("id").equals(id))
                .findFirst()
                .orElseThrow()
                .get("enabled"))
        .isEqualTo(false);
    schedules.delete(id);
  }

  @Test
  void dailyClaimsOnceAndDeletedRulesCannotLaunch() {
    ObjectNode task = createTask(false, null, false);
    String taskId = task.path("id").asText(), sopId = task.path("sopId").asText();
    var request =
        grouped(
            new com.codexflow.configcenter.dto.TaskScheduleRequest(
                "每日", sopId, taskId, "daily", "09:00", null, true));
    String id = (String) schedules.save(null, request).get("id");
    jdbc.update("update codex_task_schedules set enabled=false where id<>?", id);
    ZonedDateTime due = ZonedDateTime.of(2030, 1, 1, 9, 0, 0, 0, ZoneId.of("Asia/Shanghai"));
    jdbc.update("update codex_task_schedules set next_at=? where id=?", due.toInstant(), id);
    assertThat(schedules.claim(due, 1)).containsExactly(taskId);
    assertThat(schedules.claim(due.plusSeconds(20), 1)).isEmpty();
    assertThat(schedules.claim(due.plusDays(1), 1)).containsExactly(taskId);
    jdbc.update("update codex_sop_task_definitions set enabled=false where id=?", taskId);
    assertThat(schedules.claim(due.plusDays(2), 1)).isEmpty();
    jdbc.update("update codex_sop_task_definitions set enabled=true where id=?", taskId);
    schedules.delete(id);
    assertThatThrownBy(() -> launches.reserveLatest(taskId, "schedule"))
        .isInstanceOf(ConflictFailure.class);
    assertThat(schedules.save(null, request)).isNotNull();
  }

  @Test
  void rejectsLegacyEnablingAndInvalidRules() {
    assertThatThrownBy(() -> createTask(true, "09:00", false))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("定时任务管理");
    ObjectNode task = createTask(false, null, false);
    String taskId = task.path("id").asText(), sopId = task.path("sopId").asText();
    assertThatThrownBy(
            () ->
                schedules.save(
                    null,
                    grouped(
                        new com.codexflow.configcenter.dto.TaskScheduleRequest(
                            "错误", sopId, taskId, "interval", null, 4, true))))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(
            () ->
                schedules.save(
                    null,
                    grouped(
                        new com.codexflow.configcenter.dto.TaskScheduleRequest(
                            "错误", sopId, taskId, "daily", "25:00", null, true))))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(
            () ->
                schedules.save(
                    null,
                    grouped(
                        new com.codexflow.configcenter.dto.TaskScheduleRequest(
                            "错误", "other", taskId, "daily", "09:00", null, true))))
        .isInstanceOf(ConflictFailure.class);
    assertThat(config.copyTask(taskId).path("scheduleEnabled").asBoolean()).isFalse();
  }

  @Test
  void concurrentScansClaimDueRuleOnlyOnce() throws Exception {
    ObjectNode task = createTask(false, null, false);
    String taskId = task.path("id").asText();
    String id =
        (String)
            schedules
                .save(
                    null,
                    grouped(
                        new com.codexflow.configcenter.dto.TaskScheduleRequest(
                            "并发", task.path("sopId").asText(), taskId, "interval", null, 40, true)))
                .get("id");
    jdbc.update("update codex_task_schedules set enabled=false where id<>?", id);
    var due = ZonedDateTime.of(2031, 1, 1, 9, 0, 0, 0, ZoneId.of("Asia/Shanghai"));
    jdbc.update("update codex_task_schedules set next_at=? where id=?", due.toInstant(), id);
    var pool = java.util.concurrent.Executors.newFixedThreadPool(2);
    var start = new java.util.concurrent.CountDownLatch(1);
    try {
      java.util.concurrent.Callable<List<String>> scan =
          () -> {
            start.await();
            return schedules.claim(due, 1);
          };
      var first = pool.submit(scan);
      var second = pool.submit(scan);
      start.countDown();
      var all =
          new java.util.ArrayList<String>(first.get(10, java.util.concurrent.TimeUnit.SECONDS));
      all.addAll(second.get(10, java.util.concurrent.TimeUnit.SECONDS));
      assertThat(all).containsExactly(taskId);
    } finally {
      pool.shutdownNow();
      schedules.delete(id);
    }
  }

  @Test
  void ruleChangesResetTimeAndRunsFreezeLatestTaskConfiguration() {
    ObjectNode task = createTask(false, null, false);
    String taskId = task.path("id").asText(), sopId = task.path("sopId").asText();
    String id =
        (String)
            schedules
                .save(
                    null,
                    grouped(
                        new com.codexflow.configcenter.dto.TaskScheduleRequest(
                            "规则", sopId, taskId, "daily", "09:00", null, true)))
                .get("id");
    var interval =
        schedules.save(
            id,
            grouped(
                new com.codexflow.configcenter.dto.TaskScheduleRequest(
                    "规则", sopId, taskId, "interval", null, 40, true)));
    assertThat(
            Duration.between(Instant.now(), Instant.parse((String) interval.get("nextScheduleAt")))
                .toMinutes())
        .isBetween(39L, 40L);
    String newSop = createSop(UUID.randomUUID().toString());
    config.updateTask(
        taskId, grouped(new TaskDefinitionSaveRequest("最新任务", "最新目标", newSop, null, true)));
    assertThat(
            schedules.list("规则").stream()
                .filter(r -> r.get("id").equals(id))
                .findFirst()
                .orElseThrow()
                .get("sopId"))
        .isEqualTo(newSop);
    Instant trigger = Instant.parse((String) interval.get("nextScheduleAt"));
    assertThat(schedules.claim(trigger.atZone(ZoneId.of("Asia/Shanghai")))).contains(taskId);
    schedules.save(
        id,
        grouped(
            new com.codexflow.configcenter.dto.TaskScheduleRequest(
                "规则", newSop, taskId, "interval", null, 40, false)));
    var reenabled =
        schedules.save(
            id,
            grouped(
                new com.codexflow.configcenter.dto.TaskScheduleRequest(
                    "规则", newSop, taskId, "interval", null, 40, true)));
    assertThatThrownBy(() -> launches.reserveLatest(taskId, "schedule"))
        .isInstanceOf(ConflictFailure.class);
    assertThat(
            schedules.claim(
                Instant.parse((String) reenabled.get("nextScheduleAt"))
                    .atZone(ZoneId.of("Asia/Shanghai"))))
        .contains(taskId);
    var run = launches.reserveLatest(taskId, "schedule");
    assertThat(run.prepared().payload().toString()).contains("最新目标");
    assertThatThrownBy(() -> launches.reserveLatest(taskId, "schedule"))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("仍在运行");
    schedules.delete(id);
    assertThat(workflowRuns.runDetail(run.prepared().workflowId()).toString()).contains("最新任务");
    launches.release(run.prepared().workflowId());
  }

  private ObjectNode createTask(
      boolean scheduleEnabled, String scheduleTime, boolean notifyDingTalk) {
    String suffix = UUID.randomUUID().toString();
    return config.createTask(
        grouped(
            new TaskDefinitionSaveRequest(
                "定时任务-" + suffix,
                "验证每日定时运行",
                createSop(suffix),
                null,
                true,
                null,
                scheduleEnabled,
                scheduleTime,
                notifyDingTalk)));
  }

  private String createSop(String suffix) {
    String roleId =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    SopStepRequest step =
        new SopStepRequest(
            "定时步骤", roleId, "完成测试", null, null, "local", null, null, null, null, null, Set.of(),
            Set.of());
    return config
        .createSop(
            grouped(
                new SopSaveRequest(
                    "定时SOP-" + suffix,
                    null,
                    "local",
                    null,
                    null,
                    true,
                    3,
                    "automatic",
                    null,
                    null,
                    List.of(step))))
        .path("id")
        .asText();
  }
}
