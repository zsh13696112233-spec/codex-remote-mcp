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

  TaskLaunchStore(
      TaskDefinitionRepository tasks, TaskRunRepository runs, WorkflowRunStore workflowRuns) {
    this.tasks = tasks;
    this.runs = runs;
    this.workflowRuns = workflowRuns;
  }

  /** 锁定任务定义，使用最新配置创建运行，并立即占用任务运行槽。 */
  @Transactional
  public LaunchReservation reserveLatest(String taskId) {
    TaskDefinitionEntity task = requiredTaskForUpdate(taskId);
    validateStartable(task);
    requireIdle(task);
    PreparedRun prepared = workflowRuns.prepareLatest(taskId);
    task.activeWorkflowId = prepared.workflowId();
    tasks.saveAndFlush(task);
    return new LaunchReservation(task.id, prepared, task.notifyDingTalk);
  }

  /** 锁定任务定义；空闲时创建运行，否则返回当前占用者。 */
  @Transactional
  public LaunchAttempt reserveLatestOrActive(String taskId) {
    TaskDefinitionEntity task = requiredTaskForUpdate(taskId);
    validateStartable(task);
    if (task.activeWorkflowId != null) {
      return new LaunchAttempt(task.activeWorkflowId, null);
    }
    PreparedRun prepared = workflowRuns.prepareLatest(taskId);
    task.activeWorkflowId = prepared.workflowId();
    tasks.saveAndFlush(task);
    return new LaunchAttempt(null, new LaunchReservation(task.id, prepared, task.notifyDingTalk));
  }

  /** 按历史快照创建重试运行，并占用来源任务的运行槽。 */
  @Transactional
  public LaunchReservation reserveRetry(String sourceWorkflowId) {
    TaskRunEntity source =
        runs.findById(sourceWorkflowId)
            .orElseThrow(() -> new NotFoundFailure("找不到运行记录：" + sourceWorkflowId));
    TaskDefinitionEntity task = requiredTaskForUpdate(source.taskDefinition.id);
    requireIdle(task);
    PreparedRun prepared = workflowRuns.prepareRetry(sourceWorkflowId);
    task.activeWorkflowId = prepared.workflowId();
    tasks.saveAndFlush(task);
    return new LaunchReservation(task.id, prepared, false);
  }

  /** 为一个已有工作流重新占用任务运行槽；存在其他占用者时返回其 ID。 */
  @Transactional
  public Optional<String> acquireExisting(String taskId, String workflowId) {
    TaskDefinitionEntity task = requiredTaskForUpdate(taskId);
    if (task.activeWorkflowId != null && !workflowId.equals(task.activeWorkflowId)) {
      return Optional.of(task.activeWorkflowId);
    }
    task.activeWorkflowId = workflowId;
    tasks.saveAndFlush(task);
    return Optional.empty();
  }

  /** 返回任务当前占用的工作流 ID。 */
  @Transactional(readOnly = true)
  public Optional<String> activeWorkflowId(String taskId) {
    return tasks.findById(taskId).map(task -> task.activeWorkflowId).filter(value -> value != null);
  }

  /** 返回所有待确认终态的任务占用快照。 */
  @Transactional(readOnly = true)
  public List<ActiveLaunch> activeLaunches() {
    return tasks.findByActiveWorkflowIdIsNotNull().stream()
        .map(task -> new ActiveLaunch(task.id, task.activeWorkflowId))
        .toList();
  }

  /** 仅当工作流仍是当前占用者时释放运行槽。 */
  @Transactional
  public void release(String workflowId) {
    tasks.releaseWorkflow(workflowId);
  }

  private TaskDefinitionEntity requiredTaskForUpdate(String taskId) {
    return tasks.findForUpdate(taskId).orElseThrow(() -> new NotFoundFailure("找不到任务定义：" + taskId));
  }

  private static void validateStartable(TaskDefinitionEntity task) {
    if (task.deleted || !task.enabled) throw new ConflictFailure("任务定义已停用或删除。");
    if (task.sop.deleted || !task.sop.enabled) throw new ConflictFailure("所选 SOP 已停用或删除。");
  }

  private static void requireIdle(TaskDefinitionEntity task) {
    if (task.activeWorkflowId != null) {
      throw new ConflictFailure("当前任务仍在运行，请等待完成后再次运行。");
    }
  }

  /** 一次已持久化且已占用任务运行槽的启动准备结果。 */
  public record LaunchReservation(
      String taskDefinitionId, PreparedRun prepared, boolean notifyDingTalk) {}

  /** 飞书等消息入口使用的空闲预约或当前占用结果。 */
  public record LaunchAttempt(String activeWorkflowId, LaunchReservation reservation) {}

  /** 当前活动任务占用的只读快照。 */
  public record ActiveLaunch(String taskDefinitionId, String workflowId) {}
}
