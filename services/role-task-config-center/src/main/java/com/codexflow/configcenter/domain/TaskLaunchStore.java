package com.codexflow.configcenter.domain;

import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 为所有任务启动来源提供同一套任务级运行占用。 */
@Service
public class TaskLaunchStore {

  private final TaskDefinitionRepository tasks;
  private final TaskRunRepository runs;
  private final WorkflowRunStore workflowRuns;
  private final TaskScheduleStore schedules;

  TaskLaunchStore(
      TaskDefinitionRepository tasks,
      TaskRunRepository runs,
      WorkflowRunStore workflowRuns,
      TaskScheduleStore schedules) {
    this.tasks = tasks;
    this.runs = runs;
    this.workflowRuns = workflowRuns;
    this.schedules = schedules;
  }

  /** 锁定任务定义，使用最新配置创建运行，并立即占用任务运行槽。 */
  @Transactional
  public LaunchReservation reserveLatest(String taskId) {
    return reserveLatest(taskId, "web");
  }

  @Transactional
  public LaunchReservation reserveLatest(String taskId, String triggerSource) {
    return reserveLatest(taskId, triggerSource, null);
  }

  @Transactional
  public LaunchReservation reserveLatest(
      String taskId, String triggerSource, String dispatchToken) {
    TaskDefinitionEntity task = requiredTaskForUpdate(taskId);
    validateStartable(task);
    requireStartSlot(task);
    if ("schedule".equals(triggerSource)) schedules.requireEnabled(taskId, dispatchToken);
    PreparedRun prepared = workflowRuns.prepareLatest(taskId, triggerSource);
    if (task.sop.jiraJql == null) task.activeWorkflowId = prepared.workflowId();
    tasks.saveAndFlush(task);
    return new LaunchReservation(task.id, prepared, task.notifyDingTalk);
  }

  @Transactional
  public List<LaunchReservation> reserveJiraBatch(String taskId, String token, int count) {
    if (count < 1 || count > 100) throw new IllegalArgumentException("批次数量无效。");
    TaskDefinitionEntity task = requiredTaskForUpdate(taskId);
    validateStartable(task);
    if (task.sop.jiraJql == null) throw new ConflictFailure("任务已关闭 Jira 批量领取，请等待下一次定时触发。");
    requireStartSlot(task);
    schedules.requireEnabled(taskId, token);
    var result = new java.util.ArrayList<LaunchReservation>();
    for (int i = 0; i < count; i++)
      result.add(
          new LaunchReservation(
              task.id, workflowRuns.prepareLatest(taskId, "schedule"), task.notifyDingTalk));
    return result;
  }

  @Transactional
  public void deferSchedule(String taskId, String token) {
    validateStartable(requiredTaskForUpdate(taskId));
    schedules.deferResources(taskId, token);
  }

  @Transactional(readOnly = true)
  public boolean isParallelRun(String taskId, String workflowId) {
    PreparedRun prepared = workflowRuns.getPrepared(workflowId);
    return taskId.equals(prepared.payload().path("taskDefinitionId").asText())
        && prepared.payload().path("jira").isObject();
  }

  /** 按历史快照创建重试运行，并占用来源任务的运行槽。 */
  @Transactional
  public LaunchReservation reserveRetry(String sourceWorkflowId) {
    TaskRunEntity source =
        runs.findById(sourceWorkflowId)
            .orElseThrow(() -> new NotFoundFailure("找不到运行记录：" + sourceWorkflowId));
    TaskDefinitionEntity task = requiredTaskForUpdate(source.taskDefinition.id);
    requireIdle(task);
    if (isParallelRun(task.id, sourceWorkflowId)
        && !java.util.Set.of("completed", "failed", "cancelled", "submit_failed")
            .contains(source.status)) throw new ConflictFailure("来源 Jira 运行尚未结束，请先等待结束。");
    PreparedRun prepared = workflowRuns.prepareRetry(sourceWorkflowId);
    task.activeWorkflowId = prepared.workflowId();
    tasks.saveAndFlush(task);
    return new LaunchReservation(task.id, prepared, false);
  }

  /** 为一个已有工作流重新占用任务运行槽；存在其他占用者时返回其 ID。 */
  @Transactional
  public Optional<String> acquireExisting(String taskId, String workflowId) {
    TaskDefinitionEntity task = requiredTaskForUpdate(taskId);
    if (isParallelRun(taskId, workflowId)) return Optional.empty();
    String busy = conflictingWorkflowId(task, workflowId);
    if (busy != null) return Optional.of(busy);
    task.activeWorkflowId = workflowId;
    tasks.saveAndFlush(task);
    return Optional.empty();
  }

  /** 返回任务当前占用的工作流 ID。 */
  @Transactional(readOnly = true)
  public Optional<String> activeWorkflowId(String taskId) {
    return tasks.findById(taskId).map(task -> conflictingWorkflowId(task, null));
  }

  /** 返回所有待确认终态的任务占用快照。 */
  @Transactional(readOnly = true)
  public List<ActiveLaunch> activeLaunches() {
    var result = new java.util.LinkedHashMap<String, ActiveLaunch>();
    for (var run : runs.activeRuns())
      result.put(
          run.getWorkflowId(), new ActiveLaunch(run.getTaskDefinitionId(), run.getWorkflowId()));
    for (var task : tasks.findOccupiedTasks()) {
      String id = conflictingWorkflowId(task, null);
      result.put(id, new ActiveLaunch(task.id, id));
    }
    return List.copyOf(result.values());
  }

  /** 仅当工作流仍是当前占用者时释放运行槽。 */
  @Transactional
  public void release(String workflowId) {
    // 使用同一套行锁和受管实体，避免批量 SQL 释放后持久化上下文仍保留旧占用。
    for (TaskDefinitionEntity task : tasks.findOccupantsForUpdate(workflowId)) {
      if (workflowId.equals(task.activeWorkflowId)) task.activeWorkflowId = null;
      if (workflowId.equals(task.dingtalkActiveWorkflowId)) task.dingtalkActiveWorkflowId = null;
    }
    tasks.flush();
  }

  private TaskDefinitionEntity requiredTaskForUpdate(String taskId) {
    return tasks.findForUpdate(taskId).orElseThrow(() -> new NotFoundFailure("找不到任务定义：" + taskId));
  }

  private static void validateStartable(TaskDefinitionEntity task) {
    if (task.deleted || !task.enabled) throw new ConflictFailure("任务定义已停用或删除。");
    if (task.sop.deleted || !task.sop.enabled) throw new ConflictFailure("所选 SOP 已停用或删除。");
  }

  private static void requireIdle(TaskDefinitionEntity task) {
    if (conflictingWorkflowId(task, null) != null) {
      throw new ConflictFailure("当前任务仍在运行，请等待完成后再次运行。");
    }
  }

  private void requireStartSlot(TaskDefinitionEntity task) {
    if (task.sop.jiraJql == null) {
      requireIdle(task);
      for (String id : runs.activeHistoryIds(task.id))
        if (isParallelRun(task.id, id)) throw new ConflictFailure("当前任务仍有 Jira 运行，请等待完成。");
    } else {
      for (String id : runs.activeHistoryIds(task.id))
        if (!isParallelRun(task.id, id)) throw new ConflictFailure("当前任务仍有旧模式运行，请等待完成。");
    }
  }

  /** 通用任务占用优先；尚未对账的钉钉通知关联也须保守保护，不能覆盖另一运行。 */
  private static String conflictingWorkflowId(TaskDefinitionEntity task, String workflowId) {
    if (task.activeWorkflowId != null && !task.activeWorkflowId.equals(workflowId)) {
      return task.activeWorkflowId;
    }
    if (task.dingtalkActiveWorkflowId != null
        && !task.dingtalkActiveWorkflowId.equals(workflowId)) {
      return task.dingtalkActiveWorkflowId;
    }
    return null;
  }

  /** 一次已持久化且已占用任务运行槽的启动准备结果。 */
  public record LaunchReservation(
      String taskDefinitionId, PreparedRun prepared, boolean notifyDingTalk) {}

  /** 当前活动任务占用的只读快照。 */
  public record ActiveLaunch(String taskDefinitionId, String workflowId) {}
}
