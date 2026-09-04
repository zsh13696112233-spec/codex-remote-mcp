package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import java.time.LocalDate;
import java.time.LocalTime;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import tools.jackson.databind.node.ObjectNode;

/** 验证每日时间配置、同日幂等领取和任务级单实例占用。 */
@SpringBootTest
class TaskScheduleIntegrationTest {

  @Autowired ConfigService config;
  @Autowired TaskScheduleStore schedules;
  @Autowired TaskLaunchStore launches;
  @Autowired JdbcTemplate jdbc;

  @Test
  void dailyScheduleIsClaimedOncePerDateAndOnlyAtConfiguredMinute() {
    String scheduleTime = "03:17";
    ObjectNode task = createTask(true, scheduleTime, false);
    LocalDate firstDate = LocalDate.of(2026, 9, 4);

    assertThat(task.path("scheduleEnabled").asBoolean()).isTrue();
    assertThat(task.path("scheduleTime").asText()).isEqualTo(scheduleTime);
    assertThat(task.path("nextScheduleAt").asText())
        .matches("\\d{4}-\\d{2}-\\d{2}T03:17:00\\+08:00");
    assertThat(schedules.claim(firstDate, LocalTime.of(3, 16))).isEmpty();
    assertThat(schedules.claim(firstDate, LocalTime.of(3, 17)))
        .containsExactly(task.path("id").asText());
    config.updateTask(
        task.path("id").asText(),
        new TaskDefinitionSaveRequest(
            task.path("name").asText(),
            task.path("objective").asText(),
            task.path("sopId").asText(),
            null,
            true,
            null,
            true,
            scheduleTime,
            false));
    assertThat(schedules.claim(firstDate, LocalTime.of(3, 17))).isEmpty();
    assertThat(schedules.claim(firstDate, LocalTime.of(3, 18))).isEmpty();
    assertThat(schedules.claim(firstDate.plusDays(1), LocalTime.of(3, 17)))
        .containsExactly(task.path("id").asText());
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
  void enabledScheduleRequiresTimeAndCopyKeepsOnlyTheTime() {
    ObjectNode task = createTask(true, "08:45", false);
    String taskId = task.path("id").asText();

    assertThatThrownBy(
            () ->
                config.updateTask(
                    taskId,
                    new TaskDefinitionSaveRequest(
                        task.path("name").asText(),
                        task.path("objective").asText(),
                        task.path("sopId").asText(),
                        null,
                        true,
                        null,
                        true,
                        null,
                        false)))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("必须填写每天执行时间");

    ObjectNode copy = config.copyTask(taskId);
    assertThat(copy.path("scheduleTime").asText()).isEqualTo("08:45");
    assertThat(copy.path("scheduleEnabled").asBoolean()).isFalse();
    assertThat(copy.path("notifyDingTalk").asBoolean()).isFalse();
    assertThat(copy.path("dingtalkTargetId").isNull()).isTrue();
  }

  private ObjectNode createTask(
      boolean scheduleEnabled, String scheduleTime, boolean notifyDingTalk) {
    String suffix = UUID.randomUUID().toString();
    return config.createTask(
        new TaskDefinitionSaveRequest(
            "定时任务-" + suffix,
            "验证每日定时运行",
            createSop(suffix),
            null,
            true,
            null,
            scheduleEnabled,
            scheduleTime,
            notifyDingTalk));
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
                List.of(step)))
        .path("id")
        .asText();
  }
}
