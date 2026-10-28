package com.codexflow.configcenter.domain;

import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** 封装工作流运行记录和不可变快照的事务性持久化操作。 */
@Service
public class WorkflowRunStore {

  private final TaskDefinitionRepository tasks;
  private final GroupService groups;
  private final TaskRunRepository runs;
  private final DomainJsonMapper json;
  private final BlockedNotificationStore blockedNotifications;

  /** 注入任务定义、运行记录数据访问组件和 JSON 映射器。 */
  WorkflowRunStore(
      TaskDefinitionRepository tasks,
      TaskRunRepository runs,
      DomainJsonMapper json,
      GroupService groups,
      BlockedNotificationStore blockedNotifications) {
    this.blockedNotifications = blockedNotifications;
    this.tasks = tasks;
    this.groups = groups;
    this.runs = runs;
    this.json = json;
  }

  /** 使用任务定义的最新配置创建一条待提交运行记录。 */
  @Transactional
  public PreparedRun prepareLatest(String taskId) {
    return prepareLatest(taskId, "web");
  }

  @Transactional
  public PreparedRun prepareLatest(String taskId, String triggerSource) {
    if (!java.util.Set.of("web", "schedule", "dingtalk").contains(triggerSource))
      throw new IllegalArgumentException("运行来源无效。");
    TaskDefinitionEntity task = findTask(taskId, false);
    if (!task.enabled) throw new ConflictFailure("任务定义已停用。");
    if (!task.sop.enabled) throw new ConflictFailure("所选 SOP 已停用。");
    String workflowId = UUID.randomUUID().toString();
    String groupName = groups.require(task.groupId);
    GroupService.same(task.groupId, task.sop.groupId);
    for (SopStepEntity step : task.sop.steps) GroupService.same(task.groupId, step.role.groupId);
    ObjectNode payload = workflowPayload(task, workflowId);
    ObjectNode snapshot = taskSnapshot(task, payload);
    snapshot.put("groupName", groupName);
    return persistPrepared(task, null, payload, snapshot, triggerSource);
  }

  /** 从指定历史运行的不可变快照创建一条重试记录。 */
  @Transactional
  public PreparedRun prepareRetry(String sourceWorkflowId) {
    TaskRunEntity source = findRun(sourceWorkflowId);
    if (source.groupId == null) throw new ConflictFailure("旧运行未归组，请使用已归组任务的最新配置运行。");
    groups.require(source.groupId);
    ObjectNode payload = (ObjectNode) json.read(source.submittedJson);
    String workflowId = UUID.randomUUID().toString();
    payload.put("workflowId", workflowId);
    if (payload.path("jira").isObject()) payload.put("jiraRetryOf", sourceWorkflowId);
    if (!payload.has("resultProtocolVersion")) payload.put("resultProtocolVersion", 1);
    payload.put("taskDefinitionId", source.taskDefinition.id);
    ObjectNode snapshot = (ObjectNode) json.read(source.snapshotJson);
    snapshot.put("workflowId", workflowId);
    snapshot.put("sourceWorkflowId", sourceWorkflowId);
    snapshot.set("submittedJson", payload.deepCopy());
    return persistPrepared(source.taskDefinition, sourceWorkflowId, payload, snapshot, "web");
  }

  /** 读取已经持久化的提交载荷，用于提交响应丢失后的幂等恢复。 */
  @Transactional(readOnly = true)
  public PreparedRun getPrepared(String workflowId) {
    TaskRunEntity run = findRun(workflowId);
    return new PreparedRun(workflowId, (ObjectNode) json.read(run.submittedJson));
  }

  /** 读取运行时冻结的钉钉展示偏好；旧快照保持原有输出。 */
  @Transactional(readOnly = true)
  public boolean dingtalkShowExecutionDetails(String workflowId) {
    return json.read(findRun(workflowId).snapshotJson)
        .path("sop")
        .path("dingtalkShowExecutionDetails")
        .asBoolean(true);
  }

  /** 返回运行所属的任务定义 ID。 */
  @Transactional(readOnly = true)
  public String taskDefinitionId(String workflowId) {
    return findRun(workflowId).taskDefinition.id;
  }

  /** 返回任务定义是否要求网页和定时运行主动通知钉钉。 */
  @Transactional(readOnly = true)
  public boolean notifyDingTalk(String taskId) {
    return findTask(taskId, false).notifyDingTalk;
  }

  /** 只读调度预览；每一条实际运行仍另行冻结完整快照。 */
  @Transactional(readOnly = true)
  public ObjectNode jiraSchedulingSpec(String taskId) {
    TaskDefinitionEntity task = findTask(taskId, false);
    return task.sop.jiraJql == null ? null : workflowPayload(task, UUID.randomUUID().toString());
  }

  /** 同批新运行自带任务归属，不互相等待升级登记；真正缺少归属的旧快照仍须先登记。 */
  @Transactional(readOnly = true)
  public boolean hasPendingLegacyRuntimeHistory(String taskId, String workflowId) {
    return runs.pendingScopePayloads(taskId, workflowId).stream()
        .anyMatch(payload -> !json.read(payload).hasNonNull("taskDefinitionId"));
  }

  /** 返回运行记录最近一次已知状态。 */
  @Transactional(readOnly = true)
  public String runStatus(String workflowId) {
    return findRun(workflowId).status;
  }

  /** 首次提交包含最长各 30 秒的提交与结果核对；恢复不抢占这一窗口。 */
  @Transactional(readOnly = true)
  public boolean submissionRecoveryDue(String workflowId) {
    TaskRunEntity run = findRun(workflowId);
    return "submitting".equals(run.status)
        && !run.submittedAt.plusSeconds(60).isAfter(Instant.now());
  }

  /** 查询指定任务的历史运行列表。 */
  @Transactional(readOnly = true)
  public List<ObjectNode> listRuns(String taskId) {
    findTask(taskId, true);
    return runs.findByTaskDefinitionIdOrderBySubmittedAtDesc(taskId).stream()
        .map(json::run)
        .toList();
  }

  @Transactional(readOnly = true)
  public List<ObjectNode> listRunSummaries(String taskId, int page, int size) {
    if (page < 0 || page > 100000 || size < 1 || size > 100) {
      throw new IllegalArgumentException("运行历史页码或每页数量无效。");
    }
    findTask(taskId, true);
    return runs.summaries(taskId, PageRequest.of(page, size)).stream()
        .map(row -> summary(taskId, row))
        .toList();
  }

  private ObjectNode summary(String taskId, TaskRunRepository.Summary row) {
    ObjectNode value = json.newObject();
    value.put("workflowId", row.getWorkflowId());
    value.put("groupId", row.getGroupId());
    value.put("groupName", row.getGroupName());
    value.put("taskDefinitionId", taskId);
    value.put("monitorUrl", monitorUrl(row.getWorkflowId()));
    value.put("sourceWorkflowId", row.getSourceWorkflowId());
    value.put("status", row.getStatus());
    if (row.getTerminationJson() != null)
      value.set("termination", json.read(row.getTerminationJson()));
    value.put("submittedAt", row.getSubmittedAt().toString());
    value.put("updatedAt", row.getUpdatedAt().toString());
    return value;
  }

  @Transactional(readOnly = true)
  public List<ObjectNode> activeHistory(String taskId) {
    return runs.activeHistoryIds(taskId).stream()
        .map(id -> json.newObject().put("workflowId", id))
        .toList();
  }

  @Transactional(readOnly = true)
  public ObjectNode runHistory(String taskId, String status, int page, int size) {
    if (page < 0 || page > 100000 || size < 1 || size > 100) {
      throw new IllegalArgumentException("运行历史页码或每页数量无效。");
    }
    if (!java.util.Set.of(
            "",
            "submitting",
            "queued",
            "running",
            "cancelling",
            "completed",
            "failed",
            "cancelled",
            "submit_failed")
        .contains(status)) {
      throw new IllegalArgumentException("运行状态无效。");
    }
    findTask(taskId, true);
    var result = runs.history(taskId, status, PageRequest.of(page, size));
    int lastPage = Math.max(0, result.getTotalPages() - 1);
    if (page > lastPage) {
      page = lastPage;
      result = runs.history(taskId, status, PageRequest.of(page, size));
    }
    ObjectNode value =
        json.newObject()
            .put("total", result.getTotalElements())
            .put("page", page)
            .put("size", size);
    var items = value.putArray("items");
    result.forEach(row -> items.add(summary(taskId, row)));
    return value;
  }

  @Transactional(readOnly = true)
  public ObjectNode runDetail(String workflowId) {
    ObjectNode detail = json.run(findRun(workflowId));
    var notification = blockedNotifications.view(workflowId);
    detail.set(
        "blockedNotification",
        json.newObject()
            .put("state", (String) notification.get("state"))
            .put("reason", (String) notification.get("reason")));
    return detail;
  }

  @Transactional(readOnly = true)
  public List<String> pendingRuntimeScopes(String taskId) {
    return runs.pendingScopes(taskId, PageRequest.of(0, 200));
  }

  /** 仅检查其他历史运行；当前待提交运行会随提交原子登记，不应阻塞自身。 */
  @Transactional(readOnly = true)
  public boolean hasPendingRuntimeHistory(String taskId, String workflowId) {
    return runs.existsByTaskDefinitionIdAndRuntimeScopeRegisteredFalseAndWorkflowIdNot(
        taskId, workflowId);
  }

  @Transactional(readOnly = true)
  public List<String> pendingRuntimeScopeTasks() {
    return runs.pendingScopeTasks(PageRequest.of(0, 20));
  }

  @Transactional
  public void markRuntimeScopes(List<String> ids) {
    runs.markScopes(ids);
  }

  @Transactional
  public void recordSummaryStatuses(JsonNode values) {
    values
        .properties()
        .forEach(
            entry -> {
              if (java.util.Set.of(
                      "queued", "running", "cancelling", "completed", "failed", "cancelled")
                  .contains(entry.getValue().asText())) {
                runs.updateStatus(entry.getKey(), entry.getValue().asText(), Instant.now());
              }
            });
  }

  @Transactional
  public void recordTerminations(JsonNode values) {
    if (!values.isObject()) return;
    values
        .properties()
        .forEach(
            entry -> {
              if (entry.getValue().isObject())
                runs.updateTermination(entry.getKey(), json.write(entry.getValue()));
            });
  }

  /** 保存网关响应及其状态，并返回更新后的运行 JSON。 */
  @Transactional
  public ObjectNode recordGatewayStatus(
      String workflowId, JsonNode response, String defaultStatus) {
    TaskRunEntity run = findRun(workflowId);
    run.status = response.path("status").asText(defaultStatus);
    run.gatewayResponseJson = json.write(response);
    if (response.path("termination").isObject())
      run.terminationJson = json.write(response.path("termination"));
    run.updatedAt = Instant.now();
    runs.save(run);
    return json.run(run);
  }

  /** 保存轮询得到的实时运行状态和网关响应。 */
  @Transactional
  public void recordLiveStatus(String workflowId, String status, JsonNode response) {
    TaskRunEntity run = findRun(workflowId);
    run.status = status;
    run.gatewayResponseJson = json.write(response);
    if (response.path("termination").isObject())
      run.terminationJson = json.write(response.path("termination"));
    run.updatedAt = Instant.now();
    runs.save(run);
  }

  /** 将网关提交异常记录为 {@code submit_failed} 状态。 */
  @Transactional
  public void recordSubmissionFailure(String workflowId, RuntimeException error) {
    TaskRunEntity run = findRun(workflowId);
    run.status = "submit_failed";
    run.errorMessage = error.getMessage();
    run.updatedAt = Instant.now();
    runs.save(run);
  }

  /** 生成可在监控中心打开指定工作流的 URL。 */
  public String monitorUrl(String workflowId) {
    return json.monitorUrl(workflowId);
  }

  /** 持久化尚未提交到网关的运行记录及其冻结快照。 */
  private PreparedRun persistPrepared(
      TaskDefinitionEntity task,
      String sourceWorkflowId,
      ObjectNode payload,
      ObjectNode snapshot,
      String triggerSource) {
    TaskRunEntity run = new TaskRunEntity();
    run.workflowId = payload.path("workflowId").asText();
    run.taskDefinition = task;
    run.sourceWorkflowId = sourceWorkflowId;
    run.status = "submitting";
    run.triggerSource = triggerSource;
    run.runName = snapshot.path("name").asText("");
    run.groupId = snapshot.path("groupId").asText(null);
    run.groupName = snapshot.path("groupName").asText(null);
    run.snapshotJson = json.write(snapshot);
    run.submittedJson = json.write(payload);
    run.submittedAt = Instant.now();
    run.updatedAt = run.submittedAt;
    runs.saveAndFlush(run);
    blockedNotifications.reserve(run.workflowId, snapshot.path("blockedNotification"));
    return new PreparedRun(run.workflowId, payload);
  }

  /** 将任务定义及其 SOP 步骤转换为工作流网关请求载荷。 */
  private ObjectNode workflowPayload(TaskDefinitionEntity task, String workflowId) {
    if (task.sop.steps.isEmpty()) {
      throw new ConflictFailure("所选 SOP 没有可执行步骤，请先编辑并保存 SOP。");
    }
    ObjectNode root = json.newObject();
    root.put("workflowId", workflowId);
    root.put("resultProtocolVersion", "automatic".equals(task.sop.advanceMode) ? 3 : 5);
    root.put("taskDefinitionId", task.id);
    if (task.sop.jiraJql != null) {
      root.putObject("jira").put("jql", task.sop.jiraJql).put("account", task.sop.jiraAccount);
    }
    root.put("groupId", task.groupId);
    root.put("name", task.name);
    boolean grouped = "group".equals(task.sop.dispatchMode);
    if (grouped) root.put("dispatchMode", "group");
    else root.put("supervisorAgentId", task.sop.supervisorAgentId);
    root.put("failurePolicy", "stop");
    root.put("supervisorTimeoutSec", task.sop.supervisorTimeoutSec);
    root.put("advanceMode", task.sop.advanceMode);
    if ("semi_automatic".equals(task.sop.advanceMode)) {
      root.putObject("waitPolicy").put("version", 2);
    }
    var nodes = root.putArray("nodes");
    String previous = null;
    int stepNumber = 1;
    for (SopStepEntity step : task.sop.steps) {
      ObjectNode node = nodes.addObject();
      node.put("id", step.id);
      node.put("displayName", step.displayName);
      node.put("roleName", step.role.name);
      if (!grouped) {
        var executor = node.putObject("executor");
        executor.put("type", step.executorType);
        executor.put("agentId", step.agentId);
      }
      node.put("prompt", basePrompt(task, step, stepNumber));
      var dependencies = node.putArray("dependsOn");
      if (previous != null) dependencies.add(previous);
      previous = step.id;
      if (!grouped && step.workingDirectory != null) node.put("cwd", step.workingDirectory);
      node.put("write", step.writeEnabled);
      node.put("permissionProfile", step.permissionProfile);
      node.put(
          "model", step.modelOverride == null ? task.sop.defaultStepModel : step.modelOverride);
      String effort =
          step.reasoningEffortOverride == null
              ? task.sop.defaultStepReasoningEffort
              : step.reasoningEffortOverride;
      if (effort != null) node.put("reasoningEffort", effort);
      node.put("timeoutSec", step.timeoutSec);
      stepNumber++;
    }
    return root;
  }

  /** 根据任务目标和步骤有效提示词生成单步骤提示词；空步骤要求继承角色职责。 */
  private String basePrompt(TaskDefinitionEntity task, SopStepEntity step, int stepNumber) {
    String objective =
        task.objective + (task.additionalNotes == null ? "" : "\n\n补充说明：\n" + task.additionalNotes);
    return "任务名称：\n"
        + task.name
        + "\n\n任务目标：\n"
        + objective
        + "\n\n你当前负责：\n第"
        + stepNumber
        + "步："
        + step.displayName
        + "\n\n你的角色：\n"
        + step.role.name
        + "\n\n本步骤执行要求：\n"
        + (step.instruction.isBlank() ? step.role.duty : step.instruction)
        + "\n\n期望输出：\n"
        + step.expectedOutput
        + "\n\n请只完成当前步骤，不要代替其他步骤执行。\n请使用普通用户容易理解的语言返回结果。";
  }

  /** 生成包含任务、SOP 和实际提交载荷的不可变运行快照。 */
  private ObjectNode taskSnapshot(TaskDefinitionEntity task, ObjectNode submitted) {
    ObjectNode snapshot = json.task(task);
    snapshot.put("workflowId", submitted.path("workflowId").asText());
    snapshot.set(
        "blockedNotification", blockedNotifications.freezeGroup(task.blockedNotificationGroupId));
    snapshot.set("sop", json.sop(task.sop));
    snapshot.set("submittedJson", submitted.deepCopy());
    return snapshot;
  }

  /** 查询任务定义，并按调用场景决定是否接受软删除记录。 */
  private TaskDefinitionEntity findTask(String id, boolean includeDeleted) {
    TaskDefinitionEntity task =
        tasks.findById(id).orElseThrow(() -> new NotFoundFailure("找不到任务定义：" + id));
    if (task.deleted && !includeDeleted) {
      throw new NotFoundFailure("找不到任务定义：" + id);
    }
    return task;
  }

  /** 根据工作流 ID 查询运行记录，不存在时抛出领域未找到异常。 */
  private TaskRunEntity findRun(String id) {
    return runs.findById(id).orElseThrow(() -> new NotFoundFailure("找不到运行记录：" + id));
  }
}
