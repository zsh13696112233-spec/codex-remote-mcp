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
  void rejectedDispatchIsRecordedWithoutRunningItsCheck() throws Exception {
    var schedules = mock(TaskScheduleStore.class);
    var workflowRuns = mock(WorkflowRunService.class);
    var checks = mock(ScheduleCheckService.class);
    var entered = new java.util.concurrent.CountDownLatch(1);
    var release = new java.util.concurrent.CountDownLatch(1);
    var first = dispatch("same-task");
    var second = new TaskScheduleStore.Dispatch("same-task", "next-token", "group", first.check());
    when(schedules.claimDispatches(any(ZonedDateTime.class), anyInt()))
        .thenReturn(List.of(first), List.of(second));
    when(checks.passes(first))
        .thenAnswer(
            invocation -> {
              entered.countDown();
              release.await(10, java.util.concurrent.TimeUnit.SECONDS);
              return false;
            });
    var scheduler = new DailyTaskScheduler(schedules, workflowRuns, checks);
    try {
      scheduler.tick();
      org.junit.jupiter.api.Assertions.assertTrue(
          entered.await(2, java.util.concurrent.TimeUnit.SECONDS));
      scheduler.tick();
      verify(schedules).abandonDispatch(second);
      verify(checks, org.mockito.Mockito.never()).passes(second);
      verify(workflowRuns).deferScheduledForResources(first.taskId(), first.token());
      org.mockito.Mockito.verifyNoMoreInteractions(workflowRuns);
    } finally {
      release.countDown();
      scheduler.close();
    }
  }

  private static TaskScheduleStore.Dispatch dispatch(String id) {
    return new TaskScheduleStore.Dispatch(
        id,
        "token",
        "group",
        new com.codexflow.configcenter.dto.ScheduleCheckRequest(false, null, null, null, 60));
  }

  private static ScheduleCheckService checks() {
    var checks = mock(ScheduleCheckService.class);
    when(checks.passes(any())).thenReturn(true);
    return checks;
  }

  @Test
  void slowReconciliationDoesNotBlockScheduledLaunches() throws Exception {
    TaskScheduleStore schedules = mock(TaskScheduleStore.class);
    WorkflowRunService workflowRuns = mock(WorkflowRunService.class);
    DailyTaskScheduler scheduler = new DailyTaskScheduler(schedules, workflowRuns, checks());
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
    when(schedules.claimDispatches(
            org.mockito.ArgumentMatchers.any(ZonedDateTime.class),
            org.mockito.ArgumentMatchers.anyInt()))
        .thenReturn(List.of(dispatch("due-task")));
    when(workflowRuns.runScheduled("due-task", "token"))
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
    DailyTaskScheduler scheduler = new DailyTaskScheduler(schedules, workflowRuns, checks());
    when(schedules.claimDispatches(any(ZonedDateTime.class), anyInt()))
        .thenReturn(List.of(dispatch("busy-task"), dispatch("idle-task")));
    doThrow(new ConflictFailure("当前任务仍在运行")).when(workflowRuns).runScheduled("busy-task", "token");

    try {
      scheduler.tick();
      verify(workflowRuns, timeout(2000)).runScheduled("busy-task", "token");
      verify(workflowRuns, timeout(2000)).runScheduled("idle-task", "token");
    } finally {
      scheduler.close();
    }
  }
}
