package com.codexflow.configcenter.domain;

import java.time.LocalDate;
import java.time.LocalTime;
import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 按北京时间日期和分钟领取每日定时任务。 */
@Service
public class TaskScheduleStore {

  private final TaskDefinitionRepository tasks;

  TaskScheduleStore(TaskDefinitionRepository tasks) {
    this.tasks = tasks;
  }

  /** 在事务中标记当天已处理，并返回所属 SOP 仍可用的任务 ID。 */
  @Transactional
  public List<String> claim(LocalDate scheduleDate, LocalTime scheduleTime) {
    List<String> claimed = new ArrayList<>();
    for (TaskDefinitionEntity task : tasks.findDueSchedulesForUpdate(scheduleDate, scheduleTime)) {
      task.lastScheduleDate = scheduleDate;
      if (!task.sop.deleted && task.sop.enabled) claimed.add(task.id);
    }
    return claimed;
  }
}
