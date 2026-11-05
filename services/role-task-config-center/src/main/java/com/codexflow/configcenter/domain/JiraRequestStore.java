package com.codexflow.configcenter.domain;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.node.ObjectNode;

/** 持久化采集与启动意图；工单互斥仍只在中央网关。 */
@Service
public class JiraRequestStore {
  private final JdbcTemplate jdbc;
  private final WorkflowRunStore runs;
  private final TaskDefinitionRepository tasks;
  private final TaskScheduleStore schedules;
  private final DomainJsonMapper json;
  private final TaskLaunchStore launches;

  JiraRequestStore(
      JdbcTemplate jdbc,
      WorkflowRunStore runs,
      TaskDefinitionRepository tasks,
      TaskScheduleStore schedules,
      DomainJsonMapper json,
      TaskLaunchStore launches) {
    this.jdbc = jdbc;
    this.runs = runs;
    this.tasks = tasks;
    this.schedules = schedules;
    this.json = json;
    this.launches = launches;
  }

  private ObjectNode spec(String taskId) {
    var task = tasks.findForUpdate(taskId).orElseThrow(() -> new NotFoundFailure("任务不存在。"));
    if (task.deleted || !task.enabled || task.sop.deleted || !task.sop.enabled)
      throw new ConflictFailure("任务或 SOP 已停用。");
    ObjectNode value = runs.jiraSchedulingSpec(taskId);
    if (value == null) throw new ConflictFailure("任务已关闭 Jira 领取。");
    value.remove("workflowId");
    return value;
  }

  private String schedule(String taskId) {
    var rows =
        jdbc.queryForList(
            "SELECT * FROM codex_task_schedules WHERE task_definition_id=? AND enabled=TRUE",
            taskId);
    if (rows.isEmpty()) throw new ConflictFailure("定时规则已停止。");
    var row = rows.get(0);
    // 调度游标和历史检查状态不是配置版本。保留旧检查配置列参与快照比较，
    // 避免升级后使已有待派发请求失效；这些列不再控制执行。
    for (String key :
        List.of(
            "next_at",
            "dispatch_pending",
            "dispatch_token",
            "dispatch_expires_at",
            "resource_pending",
            "last_check_at",
            "last_check_status",
            "last_check_reason")) row.remove(key);
    return row.toString();
  }

  @Transactional
  public ObjectNode enqueue(String taskId, String source, String token) {
    jdbc.queryForObject(
        "SELECT id FROM codex_group_write_lock WHERE id=1 FOR UPDATE", Integer.class);
    ObjectNode value = spec(taskId);
    String scheduleValue = null;
    if (source.equals("schedule")) {
      schedules.consumeForJiraPool(taskId, token);
      scheduleValue = schedule(taskId);
    }
    var existing =
        jdbc.queryForList(
            "SELECT id FROM codex_jira_requests WHERE task_definition_id=? AND source=? AND state IN ('pending','waiting','dispatching') ORDER BY created_at",
            taskId,
            source);
    if (!source.equals("schedule") && !existing.isEmpty())
      return status(existing.get(0).get("id").toString());
    String id = UUID.randomUUID().toString();
    var now = Timestamp.from(Instant.now());
    jdbc.update(
        "INSERT INTO codex_jira_requests(id,task_definition_id,source,spec_json,schedule_json,state,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
        id,
        taskId,
        source,
        value.toString(),
        scheduleValue,
        now,
        now);
    return status(id);
  }

  public tools.jackson.databind.node.ArrayNode scheduleOverview(String group) {
    var result = json.newObject().arrayNode();
    for (var row :
        jdbc.queryForList(
            "SELECT s.name,s.next_at,t.group_id FROM codex_task_schedules s JOIN codex_sop_task_definitions t ON t.id=s.task_definition_id WHERE s.enabled=TRUE AND (?='' OR t.group_id=?)",
            group,
            group)) {
      var item = json.newObject().put("name", row.get("name").toString());
      if (row.get("next_at") != null)
        item.put("nextAt", ((Timestamp) row.get("next_at")).toInstant().toString());
      result.add(item);
    }
    return result;
  }

  public List<String> pending() {
    return jdbc.queryForList(
        "SELECT id FROM codex_jira_requests WHERE state IN ('pending','waiting','dispatching') ORDER BY updated_at LIMIT 20",
        String.class);
  }

  public ObjectNode status(String id) {
    var rows = jdbc.queryForList("SELECT * FROM codex_jira_requests WHERE id=?", id);
    if (rows.isEmpty()) throw new NotFoundFailure("找不到启动请求。");
    var r = rows.get(0);
    ObjectNode out =
        json.newObject()
            .put("requestId", id)
            .put("taskDefinitionId", r.get("task_definition_id").toString())
            .put("source", r.get("source").toString())
            .put("state", r.get("state").toString())
            .put("reason", r.get("reason").toString());
    out.set("spec", json.read(r.get("spec_json").toString()));
    if (r.get("reservation_id") != null)
      out.put("reservationId", r.get("reservation_id").toString());
    if (r.get("workflow_id") != null) {
      out.put("workflowId", r.get("workflow_id").toString());
      out.put("monitorUrl", runs.monitorUrl(r.get("workflow_id").toString()));
      var binding =
          runs.getPrepared(r.get("workflow_id").toString()).payload().path("jiraReservation");
      out.set(
          "jiraIssue",
          json.newObject()
              .put("issueKey", binding.path("issueKey").asText())
              .put("title", binding.path("title").asText()));
      if (binding.path("people").isObject()) {
        var source = binding.path("people");
        var people =
            json.newObject()
                .put("assignee", source.path("assignee").asText())
                .put("collectedAt", source.path("collectedAt").asText());
        var names = people.putArray("developers");
        for (var name : source.path("developers")) if (name.isTextual()) names.add(name.asText());
        ((ObjectNode) out.path("jiraIssue")).set("people", people);
      }
    }
    return out;
  }

  @Transactional
  public void validate(String id) {
    var r = jdbc.queryForMap("SELECT * FROM codex_jira_requests WHERE id=? FOR UPDATE", id);
    if (!json.read(r.get("spec_json").toString())
        .equals(spec(r.get("task_definition_id").toString())))
      throw new ConflictFailure("任务配置已变化，旧待办停止派发。");
    if (r.get("schedule_json") != null
        && !r.get("schedule_json").equals(schedule(r.get("task_definition_id").toString())))
      throw new ConflictFailure("定时配置已变化，旧待办停止派发。");
  }

  public void state(String id, String state, String reason) {
    jdbc.update(
        "UPDATE codex_jira_requests SET state=?,reason=?,updated_at=? WHERE id=?",
        state,
        reason,
        Timestamp.from(Instant.now()),
        id);
  }

  @Transactional
  public String reservation(String id) {
    var r =
        jdbc.queryForMap(
            "SELECT reservation_id FROM codex_jira_requests WHERE id=? FOR UPDATE", id);
    if (r.get("reservation_id") != null) return r.get("reservation_id").toString();
    String value = UUID.randomUUID().toString();
    jdbc.update(
        "UPDATE codex_jira_requests SET reservation_id=?,state='dispatching' WHERE id=?",
        value,
        id);
    return value;
  }

  @Transactional
  public TaskLaunchStore.LaunchReservation prepare(String id, ObjectNode binding) {
    var r = jdbc.queryForMap("SELECT * FROM codex_jira_requests WHERE id=? FOR UPDATE", id);
    if (r.get("workflow_id") == null) validate(id);
    String taskId = r.get("task_definition_id").toString();
    var task = tasks.findForUpdate(taskId).orElseThrow();
    PreparedRun prepared =
        r.get("workflow_id") == null
            ? launches.reserveBound(taskId, r.get("source").toString(), binding).prepared()
            : runs.getPrepared(r.get("workflow_id").toString());
    jdbc.update(
        "UPDATE codex_jira_requests SET workflow_id=? WHERE id=?", prepared.workflowId(), id);
    return new TaskLaunchStore.LaunchReservation(
        taskId, prepared, task.notifyDingTalk && !"dingtalk".equals(r.get("source")));
  }

  @Transactional
  public void dispatched(String id, boolean remaining) {
    jdbc.update(
        "UPDATE codex_jira_requests SET state=?,reservation_id=NULL,updated_at=? WHERE id=?",
        remaining ? "waiting" : "started",
        Timestamp.from(Instant.now()),
        id);
    if (remaining) jdbc.update("UPDATE codex_jira_requests SET workflow_id=NULL WHERE id=?", id);
  }
}
