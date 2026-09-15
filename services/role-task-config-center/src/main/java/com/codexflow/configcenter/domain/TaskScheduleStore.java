package com.codexflow.configcenter.domain;

import com.codexflow.configcenter.dto.TaskScheduleRequest;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 独立定时配置与持久化领取；所有时间按北京时间解释。 */
@Service
public class TaskScheduleStore {
  private static final ZoneId ZONE = ZoneId.of("Asia/Shanghai");
  private final JdbcTemplate jdbc;
  private final TaskDefinitionRepository tasks;

  public TaskScheduleStore(JdbcTemplate jdbc, TaskDefinitionRepository tasks) {
    this.jdbc = jdbc;
    this.tasks = tasks;
  }

  private record Rule(
      String id,
      String name,
      String taskId,
      String mode,
      String time,
      Integer minutes,
      boolean enabled,
      Instant next) {}

  private List<Rule> rules(String suffix, Object... args) {
    return jdbc.query(
        "SELECT * FROM codex_task_schedules " + suffix,
        (rs, n) ->
            new Rule(
                rs.getString("id"),
                rs.getString("name"),
                rs.getString("task_definition_id"),
                rs.getString("mode"),
                rs.getString("daily_time"),
                (Integer) rs.getObject("interval_minutes"),
                rs.getBoolean("enabled"),
                rs.getTimestamp("next_at") == null ? null : rs.getTimestamp("next_at").toInstant()),
        args);
  }

  @Transactional(readOnly = true)
  public List<Map<String, Object>> list(String query) {
    return list(query, "");
  }

  @Transactional(readOnly = true)
  public List<Map<String, Object>> list(String query, String groupId) {
    GroupService.validateFilter(groupId);
    String filter = "";
    Object[] args = new Object[0];
    if ("unassigned".equals(groupId))
      filter =
          "WHERE task_definition_id IN (SELECT id FROM codex_sop_task_definitions WHERE group_id IS NULL) ";
    else if (groupId != null && !groupId.isEmpty()) {
      filter =
          "WHERE task_definition_id IN (SELECT id FROM codex_sop_task_definitions WHERE group_id=?) ";
      args = new Object[] {groupId};
    }
    String q = query.trim().toLowerCase(Locale.ROOT);
    return rules(filter + "ORDER BY name, id", args).stream()
        .filter(r -> r.name().toLowerCase(Locale.ROOT).contains(q))
        .map(this::view)
        .toList();
  }

  private Map<String, Object> view(Rule r) {
    TaskDefinitionEntity task = tasks.findById(r.taskId()).orElseThrow();
    boolean available =
        task.groupId != null
            && task.groupId.equals(task.sop.groupId)
            && !task.deleted
            && task.enabled
            && !task.sop.deleted
            && task.sop.enabled;
    Map<String, Object> v = new LinkedHashMap<>();
    v.put("id", r.id());
    v.put("groupId", task.groupId);
    v.put("name", r.name());
    v.put("taskDefinitionId", r.taskId());
    v.put("taskName", task.name);
    v.put("sopId", task.sop.id);
    v.put("sopName", task.sop.name);
    v.put("mode", r.mode());
    v.put("dailyTime", r.time());
    v.put("intervalMinutes", r.minutes());
    v.put("enabled", r.enabled());
    v.put("available", available);
    v.put(
        "nextScheduleAt",
        r.enabled() && available && r.next() != null ? r.next().toString() : null);
    return v;
  }

  @Transactional
  public Map<String, Object> save(String id, TaskScheduleRequest b) {
    if (b.name() == null
        || b.name().isBlank()
        || b.name().trim().length() > 160
        || b.enabled() == null) throw new IllegalArgumentException("请填写名称和启停状态。");
    TaskDefinitionEntity task =
        tasks
            .findForUpdate(b.taskDefinitionId())
            .orElseThrow(() -> new NotFoundFailure("找不到任务定义。"));
    GroupService.same(b.groupId(), task.groupId);
    GroupService.same(task.groupId, task.sop.groupId);
    if (task.deleted || !Objects.equals(task.sop.id, b.sopId()))
      throw new ConflictFailure("请选择所选 SOP 下的有效任务定义。");
    if (b.enabled() && (!task.enabled || task.sop.deleted || !task.sop.enabled))
      throw new ConflictFailure("任务定义或 SOP 已停用。");
    Rule old = id == null ? null : required(id);
    if (old != null && !old.taskId().equals(task.id))
      throw new ConflictFailure("已有定时配置不能更换任务，请删除后重新创建。");
    if (old == null && !rules("WHERE task_definition_id = ?", task.id).isEmpty())
      throw new ConflictFailure("该任务已有定时配置，请编辑现有配置。");
    String time = null;
    Integer minutes = null;
    if ("daily".equals(b.mode())) {
      if (b.dailyTime() == null || !b.dailyTime().matches("([01]\\d|2[0-3]):[0-5]\\d"))
        throw new IllegalArgumentException("请填写每天执行时间（HH:mm）。");
      time = b.dailyTime();
    } else if ("interval".equals(b.mode())) {
      minutes = b.intervalMinutes();
      if (minutes == null || minutes < 5 || minutes > 1440)
        throw new IllegalArgumentException("间隔分钟数必须在 5 到 1440 之间。");
    } else throw new IllegalArgumentException("请选择每天定时或分钟间隔。");
    boolean changed =
        old == null
            || !old.mode().equals(b.mode())
            || !Objects.equals(old.time(), time)
            || !Objects.equals(old.minutes(), minutes)
            || old.enabled() != b.enabled();
    Instant next =
        !b.enabled() ? null : changed ? next(b.mode(), time, minutes, Instant.now()) : old.next();
    String key = id == null ? UUID.randomUUID().toString() : id;
    if (old == null)
      jdbc.update(
          "INSERT INTO codex_task_schedules (id,name,task_definition_id,mode,daily_time,interval_minutes,enabled,next_at) VALUES (?,?,?,?,?,?,?,?)",
          key,
          b.name().trim(),
          task.id,
          b.mode(),
          time,
          minutes,
          b.enabled(),
          stamp(next));
    else
      jdbc.update(
          "UPDATE codex_task_schedules SET name=?,mode=?,daily_time=?,interval_minutes=?,enabled=?,next_at=? WHERE id=?",
          b.name().trim(),
          b.mode(),
          time,
          minutes,
          b.enabled(),
          stamp(next),
          key);
    if (changed)
      jdbc.update("UPDATE codex_task_schedules SET dispatch_pending=FALSE WHERE id=?", key);
    return view(required(key));
  }

  private Rule required(String id) {
    return rules("WHERE id = ? FOR UPDATE", id).stream()
        .findFirst()
        .orElseThrow(() -> new NotFoundFailure("找不到定时任务。"));
  }

  @Transactional
  public void delete(String id) {
    Rule r =
        rules("WHERE id = ?", id).stream()
            .findFirst()
            .orElseThrow(() -> new NotFoundFailure("找不到定时任务。"));
    tasks.findForUpdate(r.taskId()).orElseThrow();
    jdbc.update("DELETE FROM codex_task_schedules WHERE id = ?", id);
  }

  @Transactional
  public void disableTask(String taskId) {
    jdbc.update(
        "UPDATE codex_task_schedules SET enabled=FALSE,next_at=NULL,dispatch_pending=FALSE WHERE task_definition_id=?",
        taskId);
  }

  public void resetAfterGrouping(String taskId) {
    for (Rule r : rules("WHERE task_definition_id=? FOR UPDATE", taskId)) {
      jdbc.update(
          "UPDATE codex_task_schedules SET next_at=?,dispatch_pending=FALSE WHERE id=?",
          r.enabled() ? stamp(next(r.mode(), r.time(), r.minutes(), Instant.now())) : null,
          r.id());
    }
  }

  public void requireEnabled(String taskId) {
    if (rules(
            "WHERE task_definition_id=? AND enabled=TRUE AND dispatch_pending=TRUE FOR UPDATE",
            taskId)
        .isEmpty()) throw new ConflictFailure("定时配置已变更或本次触发已失效，不再启动。");
    jdbc.update(
        "UPDATE codex_task_schedules SET dispatch_pending=FALSE WHERE task_definition_id=?",
        taskId);
  }

  private static Timestamp stamp(Instant value) {
    return value == null ? null : Timestamp.from(value);
  }

  private static Instant next(String mode, String time, Integer minutes, Instant now) {
    if ("interval".equals(mode)) return now.plus(minutes, ChronoUnit.MINUTES);
    ZonedDateTime candidate =
        now.atZone(ZONE).toLocalDate().atTime(LocalTime.parse(time)).atZone(ZONE);
    if (!candidate.toInstant().isAfter(now)) candidate = candidate.plusDays(1);
    return candidate.toInstant();
  }

  @Transactional
  public List<String> claim(LocalDate date, LocalTime time) {
    return claim(date.atTime(time).atZone(ZONE));
  }

  @Transactional
  public List<String> claim(ZonedDateTime now) {
    return claim(now, Integer.MAX_VALUE);
  }

  @Transactional
  public List<String> claim(ZonedDateTime now, int limit) {
    List<String> claimed = new ArrayList<>();
    if (limit <= 0) return claimed;
    for (Rule r :
        rules(
            "WHERE enabled = TRUE AND next_at <= ? ORDER BY next_at, id FOR UPDATE",
            stamp(now.toInstant()))) {
      if (claimed.size() >= limit) break;
      Instant future;
      if ("interval".equals(r.mode())) {
        long elapsed = Duration.between(r.next(), now.toInstant()).toMinutes();
        future = r.next().plus((elapsed / r.minutes() + 1) * r.minutes(), ChronoUnit.MINUTES);
      } else future = next(r.mode(), r.time(), r.minutes(), now.toInstant());

      TaskDefinitionEntity task = tasks.findById(r.taskId()).orElseThrow();
      boolean ready =
          task.groupId != null
              && task.groupId.equals(task.sop.groupId)
              && !task.deleted
              && task.enabled
              && !task.sop.deleted
              && task.sop.enabled
              && Duration.between(r.next(), now.toInstant()).compareTo(Duration.ofMinutes(1)) <= 0;
      jdbc.update(
          "UPDATE codex_task_schedules SET next_at=?,dispatch_pending=? WHERE id=?",
          stamp(future),
          ready,
          r.id());
      if (ready) claimed.add(r.taskId());
    }
    return claimed;
  }
}
