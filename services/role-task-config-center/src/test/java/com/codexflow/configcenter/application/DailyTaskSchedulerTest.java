package com.codexflow.configcenter.application;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.TaskScheduleStore;
import java.time.ZonedDateTime;
import java.util.List;
import org.junit.jupiter.api.Test;

/** 验证单个定时任务冲突不会排队，也不会阻断其他到期任务。 */
class DailyTaskSchedulerTest {

  @Test
  void slowReconciliationDoesNotBlockScheduledLaunches() throws Exception {
    TaskScheduleStore schedules = mock(TaskScheduleStore.class);
    WorkflowRunService workflowRuns = mock(WorkflowRunService.class);
    DailyTaskScheduler scheduler = new DailyTaskScheduler(schedules, workflowRuns);
    var entered = new java.util.concurrent.CountDownLatch(1);
    var release = new java.util.concurrent.CountDownLatch(1);
    var launched = new java.util.concurrent.CountDownLatch(1);
    org.mockito.Mockito.doAnswer(
            invocation -> {
              entered.countDown();
              release.await();
              return null;
            })
        .when(workflowRuns)
        .reconcileActiveRuns();
    when(schedules.claim(
            org.mockito.ArgumentMatchers.any(ZonedDateTime.class),
            org.mockito.ArgumentMatchers.anyInt()))
        .thenReturn(List.of("due-task"));
    when(workflowRuns.runScheduled("due-task"))
        .thenAnswer(
            invocation -> {
              launched.countDown();
              return null;
            });
    try {
      scheduler.reconcile();
      org.junit.jupiter.api.Assertions.assertTrue(
          entered.await(2, java.util.concurrent.TimeUnit.SECONDS));
      scheduler.tick();
      org.junit.jupiter.api.Assertions.assertTrue(
          launched.await(2, java.util.concurrent.TimeUnit.SECONDS));
    } finally {
      release.countDown();
      scheduler.close();
    }
  }

  @Test
  void busyScheduledTaskIsSkippedAndLaterClaimsStillRun() {
    TaskScheduleStore schedules = mock(TaskScheduleStore.class);
    WorkflowRunService workflowRuns = mock(WorkflowRunService.class);
    DailyTaskScheduler scheduler = new DailyTaskScheduler(schedules, workflowRuns);
    when(schedules.claim(any(ZonedDateTime.class), anyInt()))
        .thenReturn(List.of("busy-task", "idle-task"));
    doThrow(new ConflictFailure("当前任务仍在运行")).when(workflowRuns).runScheduled("busy-task");

    try {
      scheduler.tick();
      verify(workflowRuns, timeout(2000)).runScheduled("busy-task");
      verify(workflowRuns, timeout(2000)).runScheduled("idle-task");
    } finally {
      scheduler.close();
    }
  }
}
