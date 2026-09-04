package com.codexflow.configcenter.domain;

import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** 封装工作流运行记录和不可变快照的事务性持久化操作。 */
@Service
public class WorkflowRunStore {

  private final TaskDefinitionRepository tasks;
  private final TaskRunRepository runs;
  private final DomainJsonMapper json;

  /** 注入任务定义、运行记录数据访问组件和 JSON 映射器。 */
  WorkflowRunStore(TaskDefinitionRepository tasks, TaskRunRepository runs, DomainJsonMapper json) {
    this.tasks = tasks;
    this.runs = runs;
    this.json = json;
  }

  /** 使用任务定义的最新配置创建一条待提交运行记录。 */
  @Transactional
  public PreparedRun prepareLatest(String taskId) {
    TaskDefinitionEntity task = findTask(taskId, false);
    if (!task.enabled) throw new ConflictFailure("任务定义已停用。");
    if (!task.sop.enabled) throw new ConflictFailure("所选 SOP 已停用。");
    String workflowId = UUID.randomUUID().toString();
    ObjectNode payload = workflowPayload(task, workflowId);
    ObjectNode snapshot = taskSnapshot(task, payload);
    return persistPrepared(task, null, payload, snapshot);
  }

  /** 从指定历史运行的不可变快照创建一条重试记录。 */
  @Transactional
  public PreparedRun prepareRetry(String sourceWorkflowId) {
    TaskRunEntity source = findRun(sourceWorkflowId);
    ObjectNode payload = (ObjectNode) json.read(source.submittedJson);
    String workflowId = UUID.randomUUID().toString();
    payload.put("workflowId", workflowId);
    ObjectNode snapshot = (ObjectNode) json.read(source.snapshotJson);
    snapshot.put("workflowId", workflowId);
    snapshot.put("sourceWorkflowId", sourceWorkflowId);
    snapshot.set("submittedJson", payload.deepCopy());
    return persistPrepared(source.taskDefinition, sourceWorkflowId, payload, snapshot);
  }

  /** 读取已经持久化的提交载荷，用于提交响应丢失后的幂等恢复。 */
  @Transactional(readOnly = true)
  public PreparedRun getPrepared(String workflowId) {
    TaskRunEntity run = findRun(workflowId);
    return new PreparedRun(workflowId, (ObjectNode) json.read(run.submittedJson));
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

  /** 返回运行记录最近一次已知状态。 */
  @Transactional(readOnly = true)
  public String runStatus(String workflowId) {
    return findRun(workflowId).status;
  }

  /** 查询指定任务的历史运行列表。 */
  @Transactional(readOnly = true)
  public List<ObjectNode> listRuns(String taskId) {
    findTask(taskId, true);
    return runs.findByTaskDefinitionIdOrderBySubmittedAtDesc(taskId).stream()
        .map(json::run)
        .toList();
  }

  /** 保存网关响应及其状态，并返回更新后的运行 JSON。 */
  @Transactional
  public ObjectNode recordGatewayStatus(
      String workflowId, JsonNode response, String defaultStatus) {
    TaskRunEntity run = findRun(workflowId);
    run.status = response.path("status").asText(defaultStatus);
    run.gatewayResponseJson = json.write(response);
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
      TaskDefinitionEntity task, String sourceWorkflowId, ObjectNode payload, ObjectNode snapshot) {
    TaskRunEntity run = new TaskRunEntity();
    run.workflowId = payload.path("workflowId").asText();
    run.taskDefinition = task;
    run.sourceWorkflowId = sourceWorkflowId;
    run.status = "submitting";
    run.snapshotJson = json.write(snapshot);
    run.submittedJson = json.write(payload);
    run.submittedAt = Instant.now();
    run.updatedAt = run.submittedAt;
    runs.saveAndFlush(run);
    return new PreparedRun(run.workflowId, payload);
  }

  /** 将任务定义及其 SOP 步骤转换为工作流网关请求载荷。 */
  private ObjectNode workflowPayload(TaskDefinitionEntity task, String workflowId) {
    if (task.sop.steps.isEmpty()) {
      throw new ConflictFailure("所选 SOP 没有可执行步骤，请先编辑并保存 SOP。");
    }
    ObjectNode root = json.newObject();
    root.put("workflowId", workflowId);
    root.put("name", task.name);
    root.put("supervisorAgentId", task.sop.supervisorAgentId);
    root.put("failurePolicy", "stop");
    root.put("handoffMode", task.sop.handoffMode);
    root.put("supervisorTimeoutSec", task.sop.supervisorTimeoutSec);
    root.put("maxRetryCount", task.sop.maxRetryCount);
    root.put("advanceMode", task.sop.advanceMode);
    var nodes = root.putArray("nodes");
    String previous = null;
    int stepNumber = 1;
    for (SopStepEntity step : task.sop.steps) {
      ObjectNode node = nodes.addObject();
      node.put("id", step.id);
      node.put("displayName", step.displayName);
      node.put("roleName", step.role.name);
      var executor = node.putObject("executor");
      executor.put("type", step.executorType);
      executor.put("agentId", step.agentId);
      node.put("prompt", basePrompt(task, step, stepNumber));
      var dependencies = node.putArray("dependsOn");
      if (previous != null) dependencies.add(previous);
      previous = step.id;
      if (step.workingDirectory != null) node.put("cwd", step.workingDirectory);
      node.put("write", step.writeEnabled);
      node.put("permissionProfile", step.permissionProfile);
      node.put(
          "model", step.modelOverride == null ? task.sop.defaultStepModel : step.modelOverride);
      node.put("timeoutSec", step.timeoutSec);
      stepNumber++;
    }
    return root;
  }

  /** 根据任务目标、角色职责和步骤要求生成单步骤提示词。 */
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
        + "\n\n你的职责：\n"
        + step.role.duty
        + "\n\n本步骤执行要求：\n"
        + step.instruction
        + "\n\n期望输出：\n"
        + step.expectedOutput
        + "\n\n请只完成当前步骤，不要代替其他步骤执行。\n请使用普通用户容易理解的语言返回结果。";
  }

  /** 生成包含任务、SOP 和实际提交载荷的不可变运行快照。 */
  private ObjectNode taskSnapshot(TaskDefinitionEntity task, ObjectNode submitted) {
    ObjectNode snapshot = json.task(task);
    snapshot.put("workflowId", submitted.path("workflowId").asText());
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
