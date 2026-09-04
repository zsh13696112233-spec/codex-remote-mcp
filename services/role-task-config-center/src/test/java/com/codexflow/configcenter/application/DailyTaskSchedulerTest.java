package com.codexflow.configcenter.application;

import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.TaskScheduleStore;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.List;
import org.junit.jupiter.api.Test;

/** 验证单个定时任务冲突不会排队，也不会阻断其他到期任务。 */
class DailyTaskSchedulerTest {

  @Test
  void busyScheduledTaskIsSkippedAndLaterClaimsStillRun() {
    TaskScheduleStore schedules = mock(TaskScheduleStore.class);
    WorkflowRunService workflowRuns = mock(WorkflowRunService.class);
    DailyTaskScheduler scheduler = new DailyTaskScheduler(schedules, workflowRuns);
    ZonedDateTime now = ZonedDateTime.of(2026, 9, 4, 3, 17, 20, 0, ZoneId.of("Asia/Shanghai"));
    when(schedules.claim(now)).thenReturn(List.of("busy-task", "idle-task"));
    doThrow(new ConflictFailure("当前任务仍在运行")).when(workflowRuns).runScheduled("busy-task");

    scheduler.runAt(now);

    verify(workflowRuns).runScheduled("busy-task");
    verify(workflowRuns).runScheduled("idle-task");
  }
}
