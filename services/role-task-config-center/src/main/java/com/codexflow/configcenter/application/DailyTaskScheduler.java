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

  DailyTaskScheduler(TaskScheduleStore schedules, WorkflowRunService workflowRuns) {
    this.schedules = schedules;
    this.workflowRuns = workflowRuns;
  }

  @Scheduled(fixedDelay = 30_000)
  void tick() {
    workflowRuns.reconcileActiveRuns();
    runAt(ZonedDateTime.now(ZONE));
  }

  /** 接受显式时间以便测试每日触发、重复扫描和错过不补跑语义。 */
  void runAt(ZonedDateTime now) {
    for (String taskId : schedules.claim(now)) {
      try {
        workflowRuns.runScheduled(taskId);
      } catch (RuntimeException error) {
        LOGGER.warn("定时任务本次未启动，taskDefinitionId={}，原因={}。", taskId, error.getMessage());
      }
    }
  }
}
