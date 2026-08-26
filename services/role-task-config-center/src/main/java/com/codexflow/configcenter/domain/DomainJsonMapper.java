package com.codexflow.configcenter.domain;

import java.time.Instant;
import java.util.Collection;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 将持久化实体转换为 Web 前端使用的稳定 JSON 响应结构。 */
@Component
class DomainJsonMapper {

  private final ObjectMapper objectMapper;
  private final String monitorUrl;

  /** 注入 Jackson 映射器，并规范化监控中心基础地址。 */
  DomainJsonMapper(
      ObjectMapper objectMapper,
      @Value("${codex.monitor.base-url:http://127.0.0.1:8090}") String monitorUrl) {
    this.objectMapper = objectMapper;
    this.monitorUrl = monitorUrl.replaceAll("/+$", "");
  }

  /** 创建使用项目统一 Jackson 配置的空 JSON 对象。 */
  ObjectNode newObject() {
    return objectMapper.createObjectNode();
  }

  /** 将角色实体转换为角色响应 JSON。 */
  ObjectNode role(RoleEntity role) {
    ObjectNode result = newObject();
    result.put("id", role.id);
    result.put("name", role.name);
    result.put("duty", role.duty);
    result.put("enabled", role.enabled);
    result.put("version", role.version);
    addTimes(result, role.createdAt, role.updatedAt);
    return result;
  }

  /** 将 SOP 聚合及其步骤转换为 SOP 响应 JSON。 */
  ObjectNode sop(SopEntity sop) {
    ObjectNode result = newObject();
    result.put("id", sop.id);
    result.put("name", sop.name);
    putNullable(result, "description", sop.description);
    result.put("supervisorAgentId", sop.supervisorAgentId);
    result.put("failurePolicy", sop.failurePolicy);
    result.put("supervisorTimeoutSec", sop.supervisorTimeoutSec);
    result.put("maxRetryCount", sop.maxRetryCount);
    result.put("defaultStepModel", sop.defaultStepModel);
    result.put("enabled", sop.enabled);
    var steps = result.putArray("steps");
    for (SopStepEntity step : sop.steps) {
      ObjectNode item = steps.addObject();
      item.put("id", step.id);
      item.put("order", step.positionNo + 1);
      item.put("displayName", step.displayName);
      item.put("roleId", step.role.id);
      item.put("roleName", step.role.name);
      item.put("roleDuty", step.role.duty);
      item.put("instruction", step.instruction);
      item.put("expectedOutput", step.expectedOutput);
      item.put("executorType", step.executorType);
      item.put("agentId", step.agentId);
      putNullable(item, "workingDirectory", step.workingDirectory);
      item.put("writeEnabled", step.writeEnabled);
      putNullable(item, "modelOverride", step.modelOverride);
      item.put(
          "effectiveModel", step.modelOverride == null ? sop.defaultStepModel : step.modelOverride);
      item.put("timeoutSec", step.timeoutSec);
      addArray(item, "skills", step.skills);
      addArray(item, "mcps", step.mcps);
    }
    addTimes(result, sop.createdAt, sop.updatedAt);
    return result;
  }

  /** 将任务定义实体转换为任务响应 JSON。 */
  ObjectNode task(TaskDefinitionEntity task) {
    ObjectNode result = newObject();
    result.put("id", task.id);
    result.put("name", task.name);
    result.put("objective", task.objective);
    result.put("sopId", task.sop.id);
    result.put("sopName", task.sop.name);
    putNullable(result, "additionalNotes", task.additionalNotes);
    result.put("enabled", task.enabled);
    result.put("deleted", task.deleted);
    addTimes(result, task.createdAt, task.updatedAt);
    return result;
  }

  /** 将运行记录及其快照字段转换为运行响应 JSON。 */
  ObjectNode run(TaskRunEntity run) {
    ObjectNode result = newObject();
    result.put("workflowId", run.workflowId);
    result.put("taskDefinitionId", run.taskDefinition.id);
    result.put("monitorUrl", monitorUrl + "/?workflowId=" + run.workflowId);
    putNullable(result, "sourceWorkflowId", run.sourceWorkflowId);
    result.put("status", run.status);
    result.set("snapshot", read(run.snapshotJson));
    result.set("submittedJson", read(run.submittedJson));
    if (run.gatewayResponseJson != null) {
      result.set("gatewayResponse", read(run.gatewayResponseJson));
    }
    putNullable(result, "error", run.errorMessage);
    result.put("submittedAt", run.submittedAt.toString());
    result.put("updatedAt", run.updatedAt.toString());
    return result;
  }

  /** 拼接指定工作流对应的监控中心访问地址。 */
  String monitorUrl(String workflowId) {
    return monitorUrl + "/?workflowId=" + workflowId;
  }

  /** 将 JSON 节点序列化为数据库可保存的字符串。 */
  String write(JsonNode node) {
    try {
      return objectMapper.writeValueAsString(node);
    } catch (Exception error) {
      throw new IllegalStateException("无法序列化工作流快照。", error);
    }
  }

  /** 将数据库中的 JSON 字符串反序列化为 JSON 节点。 */
  JsonNode read(String value) {
    try {
      return objectMapper.readTree(value);
    } catch (Exception error) {
      throw new IllegalStateException("无法读取工作流快照。", error);
    }
  }

  /** 将非空创建时间和更新时间写入目标 JSON。 */
  private static void addTimes(ObjectNode target, Instant createdAt, Instant updatedAt) {
    if (createdAt != null) target.put("createdAt", createdAt.toString());
    if (updatedAt != null) target.put("updatedAt", updatedAt.toString());
  }

  /** 将可空字符串写入目标 JSON，空引用保留为 JSON null。 */
  private static void putNullable(ObjectNode target, String field, String value) {
    if (value == null) target.putNull(field);
    else target.put(field, value);
  }

  /** 将字符串集合写入目标 JSON 数组。 */
  private static void addArray(ObjectNode target, String field, Collection<String> values) {
    var array = target.putArray(field);
    values.forEach(array::add);
  }
}
