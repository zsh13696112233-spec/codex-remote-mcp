package com.codexflow.configcenter.domain;

import com.codexflow.configcenter.client.GatewayClient;
import java.util.List;
import java.util.Objects;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

/** 中央分组目录及配置中心跨实例写入互斥；本地不复制分组目录。 */
@Service
public class GroupService {
  private final GatewayClient gateway;
  private final JdbcTemplate jdbc;
  private final tools.jackson.databind.ObjectMapper mapper;

  public GroupService(
      GatewayClient gateway, JdbcTemplate jdbc, tools.jackson.databind.ObjectMapper mapper) {
    this.gateway = gateway;
    this.jdbc = jdbc;
    this.mapper = mapper;
  }

  public void lock() {
    jdbc.queryForObject(
        "SELECT id FROM codex_group_write_lock WHERE id=1 FOR UPDATE", Integer.class);
  }

  public String require(String id) {
    validateId(id);
    for (JsonNode group : gateway.get("/agent-groups").path("groups")) {
      if (id.equals(group.path("id").asText())) return group.path("name").asText();
    }
    throw new ConflictFailure("所选分组不存在，请刷新分组列表。");
  }

  public static void validateId(String id) {
    if (id == null
        || !id.matches(
            "[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"))
      throw new IllegalArgumentException("请先选择有效分组。");
  }

  public static void validateFilter(String id) {
    if (id != null && !id.isEmpty() && !"unassigned".equals(id)) validateId(id);
  }

  public static boolean matches(String actual, String filter) {
    validateFilter(filter);
    return filter == null
        || filter.isEmpty()
        || ("unassigned".equals(filter) ? actual == null : filter.equals(actual));
  }

  public static void same(String group, String other) {
    if (group == null || !group.equals(other)) throw new ConflictFailure("关联数据必须属于同一分组，请先完成归组。");
  }

  long count(String sql, Object... args) {
    return jdbc.queryForObject(sql, Long.class, args);
  }

  @Transactional(readOnly = true)
  public ObjectNode catalog() {
    ObjectNode result = JsonNodeFactory.instance.objectNode();
    var groups = result.putArray("groups");
    JsonNode machines = gateway.get("/agents").path("agents");
    for (JsonNode group : gateway.get("/agent-groups").path("groups")) {
      ObjectNode row = (ObjectNode) group.deepCopy();
      String id = row.path("id").asText();
      counts(row, id);
      long machineCount = 0;
      for (JsonNode machine : machines)
        if (id.equals(machine.path("groupId").asText())) machineCount++;
      row.put("machineCount", machineCount);
      groups.add(row);
    }
    ObjectNode unassigned = result.putObject("unassigned");
    counts(unassigned, null);
    unassigned.put("machineCount", 0);
    return result;
  }

  private void counts(ObjectNode row, String id) {
    String predicate = id == null ? " IS NULL" : " = ?";
    Object[] args = id == null ? new Object[0] : new Object[] {id};
    String[] tables = {"roles", "sops", "task_definitions"};
    String[] names = {"roleCount", "sopCount", "taskCount"};
    for (int i = 0; i < tables.length; i++)
      row.put(
          names[i],
          count(
              "SELECT COUNT(*) FROM codex_sop_"
                  + tables[i]
                  + " WHERE deleted=FALSE AND group_id"
                  + predicate,
              args));
    row.put(
        "scheduleCount",
        count(
            "SELECT COUNT(*) FROM codex_task_schedules s JOIN codex_sop_task_definitions t ON t.id=s.task_definition_id WHERE t.deleted=FALSE AND t.group_id"
                + predicate,
            args));
  }

  @Transactional
  public JsonNode save(String id, JsonNode body) {
    lock();
    if (id != null) validateId(id);
    return id == null
        ? gateway.post("/agent-groups", body)
        : gateway.put("/agent-groups/" + id, body);
  }

  @Transactional
  public JsonNode delete(String id) {
    validateId(id);
    lock();
    for (String table : List.of("roles", "sops", "task_definitions", "task_runs")) {
      if (count("SELECT COUNT(*) FROM codex_sop_" + table + " WHERE group_id=?", id) > 0)
        throw new ConflictFailure("分组仍有配置或运行历史引用，不能删除。");
    }
    return gateway.delete("/agent-groups/" + id);
  }

  public void validateMachines(String groupId, String supervisor, List<String> executors) {
    ObjectNode body =
        JsonNodeFactory.instance
            .objectNode()
            .put("groupId", groupId)
            .put("supervisorId", supervisor);
    var array = body.putArray("executorIds");
    executors.forEach(array::add);
    gateway.post("/agents/validate", body);
  }

  public void checkMachineMove(String id, JsonNode body) {
    require(body.path("groupId").asText());
    if (id == null) return;
    for (JsonNode machine : gateway.get("/agents").path("agents")) {
      if (!id.equals(machine.path("agentId").asText())
          || Objects.equals(body.path("groupId").asText(), machine.path("groupId").asText()))
        continue;
      if (count(
              "SELECT COUNT(*) FROM codex_sop_sops s WHERE s.deleted=FALSE AND (s.supervisor_agent_id=? OR EXISTS (SELECT 1 FROM codex_sop_steps n WHERE n.sop_id=s.id AND n.agent_id=?))",
              id,
              id)
          > 0) throw new ConflictFailure("机器已被有效 SOP 引用，请先调整 SOP 后再改组。");
      // 活动历史可能来自已删除或已更换机器的配置；只读取提交快照中的机器编号。
      for (String payload :
          jdbc.queryForList(
              "SELECT submitted_json FROM codex_sop_task_runs WHERE status IN ('submitting','queued','running','cancelling')",
              String.class)) {
        JsonNode spec = mapper.readTree(payload);
        boolean referenced = id.equals(spec.path("supervisorAgentId").asText());
        for (JsonNode node : spec.path("nodes"))
          referenced |=
              id.equals(node.path("executor").path("agentId").asText())
                  || id.equals(node.path("agentId").asText());
        if (referenced) throw new ConflictFailure("机器仍有关联任务运行，请结束运行后再改组。");
      }
    }
  }
}
