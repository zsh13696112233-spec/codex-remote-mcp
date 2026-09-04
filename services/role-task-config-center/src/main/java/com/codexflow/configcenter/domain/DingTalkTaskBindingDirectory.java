package com.codexflow.configcenter.domain;

import java.util.Optional;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 任务定义与钉钉通知对象的一对一绑定，以及每个绑定独立的运行占用状态。 */
@Service
public class DingTalkTaskBindingDirectory {

  private final TaskDefinitionRepository tasks;
  private final TaskLaunchStore taskLaunches;

  DingTalkTaskBindingDirectory(TaskDefinitionRepository tasks, TaskLaunchStore taskLaunches) {
    this.tasks = tasks;
    this.taskLaunches = taskLaunches;
  }

  @Transactional
  public StartRoute reserveStart(String clientId, String targetType, String externalId) {
    Optional<TaskDefinitionEntity> candidate =
        tasks.findDingTalkBindingForUpdate(clientId, targetType, externalId);
    if (candidate.isEmpty()) return new StartRoute("unauthorized", null, null, null, null);
    TaskDefinitionEntity task = candidate.get();
    validateStartable(task, clientId);
    if (task.dingtalkActiveWorkflowId != null || task.activeWorkflowId != null) {
      String active =
          task.dingtalkActiveWorkflowId == null
              ? task.activeWorkflowId
              : task.dingtalkActiveWorkflowId;
      return new StartRoute("busy", task.id, active, view(task), null);
    }
    PreparedRun prepared = taskLaunches.reserveLatest(task.id).prepared();
    task.dingtalkActiveWorkflowId = prepared.workflowId();
    tasks.saveAndFlush(task);
    return new StartRoute("started", task.id, prepared.workflowId(), view(task), prepared);
  }

  @Transactional(readOnly = true)
  public Optional<ActiveRoute> active(String clientId, String targetType, String externalId) {
    return tasks
        .findDingTalkBinding(clientId, targetType, externalId)
        .filter(task -> task.dingtalkActiveWorkflowId != null)
        .map(task -> new ActiveRoute(task.id, task.dingtalkActiveWorkflowId));
  }

  @Transactional
  public Optional<String> acquireForRestart(String taskId, String workflowId) {
    TaskDefinitionEntity task = requiredForUpdate(taskId);
    String active =
        task.dingtalkActiveWorkflowId == null
            ? task.activeWorkflowId
            : task.dingtalkActiveWorkflowId;
    if (active != null && !workflowId.equals(active)) {
      return Optional.of(active);
    }
    task.dingtalkActiveWorkflowId = workflowId;
    task.activeWorkflowId = workflowId;
    return Optional.empty();
  }

  /** 为网页或定时启动占用钉钉通知槽，并返回冻结目标。 */
  @Transactional
  public DingTalkTargetDirectory.TargetView reserveProactive(
      String taskId, String workflowId, String clientId) {
    TaskDefinitionEntity task = requiredForUpdate(taskId);
    validateStartable(task, clientId);
    if (!workflowId.equals(task.activeWorkflowId)) {
      throw new ConflictFailure("当前任务运行状态已变化，请稍后重试。");
    }
    if (task.dingtalkActiveWorkflowId != null
        && !workflowId.equals(task.dingtalkActiveWorkflowId)) {
      throw new ConflictFailure("当前任务已有钉钉通知运行，请等待完成后再次运行。");
    }
    task.dingtalkActiveWorkflowId = workflowId;
    return view(task);
  }

  /** 校验任务当前绑定的通知对象可用于指定钉钉应用。 */
  @Transactional(readOnly = true)
  public void validateProactive(String taskId, String clientId) {
    TaskDefinitionEntity task =
        tasks.findById(taskId).orElseThrow(() -> new NotFoundFailure("找不到任务定义：" + taskId));
    validateStartable(task, clientId);
  }

  @Transactional
  public boolean reconcile(String taskId, String workflowId, boolean active) {
    TaskDefinitionEntity task = requiredForUpdate(taskId);
    if (active) {
      if ((task.dingtalkActiveWorkflowId != null
              && !workflowId.equals(task.dingtalkActiveWorkflowId))
          || (task.activeWorkflowId != null && !workflowId.equals(task.activeWorkflowId))) {
        return false;
      }
      task.dingtalkActiveWorkflowId = workflowId;
      task.activeWorkflowId = workflowId;
      return true;
    } else if (workflowId.equals(task.dingtalkActiveWorkflowId)) {
      task.dingtalkActiveWorkflowId = null;
    }
    if (!active && workflowId.equals(task.activeWorkflowId)) {
      task.activeWorkflowId = null;
    }
    return true;
  }

  @Transactional(readOnly = true)
  public boolean hasActive(String clientId) {
    return tasks.existsActiveDingTalkBinding(clientId);
  }

  /** 清理由网关确认已结束、但本地尚未来得及释放的任务占用。 */
  @Transactional
  public void releaseFinished(String workflowId) {
    tasks.releaseWorkflow(workflowId);
  }

  private TaskDefinitionEntity requiredForUpdate(String taskId) {
    return tasks.findForUpdate(taskId).orElseThrow(() -> new NotFoundFailure("找不到任务定义：" + taskId));
  }

  private static void validateStartable(TaskDefinitionEntity task, String clientId) {
    DingTalkTargetEntity target = task.dingtalkTarget;
    if (task.deleted || !task.enabled) throw new ConflictFailure("任务定义已停用或删除。");
    if (!task.sop.enabled || task.sop.deleted) throw new ConflictFailure("所选 SOP 已停用或删除。");
    if (target == null
        || target.deleted
        || !target.enabled
        || !target.available
        || !clientId.equals(target.clientId)) {
      throw new ConflictFailure("任务定义绑定的钉钉通知对象未启用或当前不可用。");
    }
  }

  private static DingTalkTargetDirectory.TargetView view(TaskDefinitionEntity task) {
    return DingTalkTargetDirectory.view(task.dingtalkTarget);
  }

  public record ActiveRoute(String taskDefinitionId, String workflowId) {}

  public record StartRoute(
      String outcome,
      String taskDefinitionId,
      String workflowId,
      DingTalkTargetDirectory.TargetView target,
      PreparedRun prepared) {}
}
