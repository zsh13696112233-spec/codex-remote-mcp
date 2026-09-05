package com.codexflow.configcenter.application;

import com.codexflow.configcenter.domain.TaskScheduleStore;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/** 每 30 秒检查一次北京时间下到达触发点的每日或间隔任务。 */
@Component
class DailyTaskScheduler {

  private static final Logger LOGGER = LoggerFactory.getLogger(DailyTaskScheduler.class);
  private static final ZoneId ZONE = ZoneId.of("Asia/Shanghai");

  private final TaskScheduleStore schedules;
  private final WorkflowRunService workflowRuns;
  private final BoundedWork launches = new BoundedWork("scheduled-task", 4, 16);
  private final BoundedWork reconciliation = new BoundedWork("workflow-reconciliation", 1, 1);
  private final BoundedWork scopeSync = new BoundedWork("workflow-scope-upgrade", 1, 1);

  DailyTaskScheduler(TaskScheduleStore schedules, WorkflowRunService workflowRuns) {
    this.schedules = schedules;
    this.workflowRuns = workflowRuns;
  }

  @Scheduled(fixedDelay = 30_000)
  void tick() {
    int available = launches.available();
    if (available == 0) return;
    for (String taskId : schedules.claim(ZonedDateTime.now(ZONE), available)) {
      launches.submit(taskId, () -> launch(taskId));
    }
  }

  @Scheduled(fixedDelay = 30_000)
  void reconcile() {
    reconciliation.submit("active-runs", workflowRuns::reconcileActiveRuns);
  }

  @jakarta.annotation.PreDestroy
  void close() {
    launches.close();
    reconciliation.close();
    scopeSync.close();
  }

  @Scheduled(fixedDelay = 15_000)
  void synchronizeScopes() {
    scopeSync.submit("scope-upgrade", workflowRuns::synchronizeRuntimeScopes);
  }

  private void launch(String taskId) {
    try {
      workflowRuns.runScheduled(taskId);
    } catch (RuntimeException error) {
      LOGGER.warn("定时任务本次未启动，taskDefinitionId={}，原因={}。", taskId, error.getMessage());
    }
  }
}
