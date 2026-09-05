package com.codexflow.configcenter.domain;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalTime;
import java.time.ZonedDateTime;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 按北京时间领取每日或固定分钟间隔的定时任务。 */
@Service
public class TaskScheduleStore {

  /** 允许覆盖 30 秒扫描跨分钟以及少量调度抖动，同时避免服务长时间停机后补跑。 */
  private static final Duration INTERVAL_TRIGGER_GRACE = Duration.ofMinutes(1);

  private final TaskDefinitionRepository tasks;

  TaskScheduleStore(TaskDefinitionRepository tasks) {
    this.tasks = tasks;
  }

  /** 在事务中标记当天已处理，并返回所属 SOP 仍可用的任务 ID。 */
  @Transactional
  public List<String> claim(LocalDate scheduleDate, LocalTime scheduleTime) {
    return claimDaily(scheduleDate, scheduleTime, Integer.MAX_VALUE);
  }

  private List<String> claimDaily(LocalDate scheduleDate, LocalTime scheduleTime, int limit) {
    List<String> claimed = new ArrayList<>();
    for (TaskDefinitionEntity task : tasks.findDueSchedulesForUpdate(scheduleDate, scheduleTime)) {
      if (claimed.size() >= limit) break;
      task.lastScheduleDate = scheduleDate;
      if (!task.sop.deleted && task.sop.enabled) claimed.add(task.id);
    }
    return claimed;
  }

  /** 领取当前扫描周期到期的两类任务；超过正常扫描延迟的间隔只推进时间，不补跑。 */
  @Transactional
  public List<String> claim(ZonedDateTime now) {
    return claim(now, Integer.MAX_VALUE);
  }

  @Transactional
  public List<String> claim(ZonedDateTime now, int limit) {
    if (limit <= 0) return List.of();
    LocalTime minute = now.toLocalTime().truncatedTo(ChronoUnit.MINUTES);
    List<String> claimed = new ArrayList<>(claimDaily(now.toLocalDate(), minute, limit));
    Instant currentInstant = now.toInstant();
    for (TaskDefinitionEntity task : tasks.findDueIntervalSchedulesForUpdate(currentInstant)) {
      if (claimed.size() >= limit) break;
      Instant scheduledAt = task.nextIntervalAt;
      int intervalMinutes = task.scheduleIntervalMinutes;
      Duration lateness = Duration.between(scheduledAt, currentInstant);
      long elapsedMinutes = Math.max(0, lateness.toMinutes());
      long intervalsToAdvance = elapsedMinutes / intervalMinutes + 1;
      task.nextIntervalAt =
          scheduledAt.plus(intervalsToAdvance * intervalMinutes, ChronoUnit.MINUTES);
      if (lateness.compareTo(INTERVAL_TRIGGER_GRACE) <= 0
          && !task.sop.deleted
          && task.sop.enabled) {
        claimed.add(task.id);
      }
    }
    return claimed;
  }
}
