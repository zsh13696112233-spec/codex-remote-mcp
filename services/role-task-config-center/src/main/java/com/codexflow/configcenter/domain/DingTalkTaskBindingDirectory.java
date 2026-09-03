package com.codexflow.configcenter.domain;

import java.util.Optional;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 任务定义与钉钉通知对象的一对一绑定，以及每个绑定独立的运行占用状态。 */
@Service
public class DingTalkTaskBindingDirectory {

  private final TaskDefinitionRepository tasks;
  private final WorkflowRunStore workflowRuns;

  DingTalkTaskBindingDirectory(TaskDefinitionRepository tasks, WorkflowRunStore workflowRuns) {
    this.tasks = tasks;
    this.workflowRuns = workflowRuns;
  }

  @Transactional
  public StartRoute reserveStart(String clientId, String targetType, String externalId) {
    Optional<TaskDefinitionEntity> candidate =
        tasks.findDingTalkBindingForUpdate(clientId, targetType, externalId);
    if (candidate.isEmpty()) return new StartRoute("unauthorized", null, null, null, null);
    TaskDefinitionEntity task = candidate.get();
    validateStartable(task, clientId);
    if (task.dingtalkActiveWorkflowId != null) {
      return new StartRoute("busy", task.id, task.dingtalkActiveWorkflowId, view(task), null);
    }
    PreparedRun prepared = workflowRuns.prepareLatest(task.id);
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
    if (task.dingtalkActiveWorkflowId != null
        && !workflowId.equals(task.dingtalkActiveWorkflowId)) {
      return Optional.of(task.dingtalkActiveWorkflowId);
    }
    task.dingtalkActiveWorkflowId = workflowId;
    return Optional.empty();
  }

  @Transactional
  public boolean reconcile(String taskId, String workflowId, boolean active) {
    TaskDefinitionEntity task = requiredForUpdate(taskId);
    if (active) {
      if (task.dingtalkActiveWorkflowId == null
          || workflowId.equals(task.dingtalkActiveWorkflowId)) {
        task.dingtalkActiveWorkflowId = workflowId;
        return true;
      }
      return false;
    } else if (workflowId.equals(task.dingtalkActiveWorkflowId)) {
      task.dingtalkActiveWorkflowId = null;
    }
    return true;
  }

  @Transactional(readOnly = true)
  public boolean hasActive(String clientId) {
    return tasks.existsActiveDingTalkBinding(clientId);
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
