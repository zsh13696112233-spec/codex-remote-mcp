package com.codexflow.configcenter.domain;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 阻断通知独立事务记录；不占用任务运行槽，不依赖聊天绑定。 */
@Service
public class BlockedNotificationStore {
  private final JdbcTemplate db;
  private final ObjectMapper json;
  private final DingTalkTargetRepository targets;

  public BlockedNotificationStore(
      JdbcTemplate db, ObjectMapper json, DingTalkTargetRepository targets) {
    this.db = db;
    this.json = json;
    this.targets = targets;
  }

  public record Mapping(String targetId, String accountType, String accountId) {}

  public record Notice(
      String id,
      String workflowId,
      JsonNode group,
      JsonNode person,
      JsonNode payload,
      String state,
      String reason,
      int attempts) {}

  @Transactional(readOnly = true)
  public List<Mapping> mappings(String clientId) {
    return db.query(
        "SELECT m.* FROM codex_jira_developer_mappings m JOIN codex_sop_dingtalk_targets t ON t.id=m.target_id WHERE t.client_id=? AND t.deleted=false ORDER BY t.display_name",
        (r, n) ->
            new Mapping(
                r.getString("target_id"), r.getString("account_type"), r.getString("account_id")),
        clientId);
  }

  @Transactional
  public void saveMapping(String clientId, Mapping mapping) {
    lockConfiguration();
    var target =
        targets
            .findForUpdate(mapping.targetId())
            .orElseThrow(() -> new NotFoundFailure("找不到钉钉人员。"));
    if (!target.clientId.equals(clientId) || !"PERSON".equals(target.targetType) || target.deleted)
      throw new IllegalArgumentException("请选择当前机器人的钉钉人员。");
    String account = mapping.accountId() == null ? "" : mapping.accountId().trim();
    if (account.isEmpty()) {
      db.update("DELETE FROM codex_jira_developer_mappings WHERE target_id=?", target.id);
      return;
    }
    if (!Set.of("accountId", "key", "name").contains(mapping.accountType())
        || account.length() > 256) throw new IllegalArgumentException("Jira 账号类型或长度无效。");
    String identity = identity(clientId, mapping.accountType(), account);
    if (!db.queryForList(
            "SELECT target_id FROM codex_jira_developer_mappings WHERE identity_hash=? AND target_id<>?",
            identity,
            target.id)
        .isEmpty()) throw new ConflictFailure("该 Jira 账号已关联其他钉钉人员。");
    db.update("DELETE FROM codex_jira_developer_mappings WHERE target_id=?", target.id);
    db.update(
        "INSERT INTO codex_jira_developer_mappings(target_id,identity_hash,account_type,account_id) VALUES (?,?,?,?)",
        target.id,
        identity,
        mapping.accountType(),
        account);
  }

  private static String identity(String client, String type, String account) {
    try {
      return HexFormat.of()
          .formatHex(
              MessageDigest.getInstance("SHA-256")
                  .digest(
                      (client + "\n" + type + "\n" + account).getBytes(StandardCharsets.UTF_8)));
    } catch (java.security.NoSuchAlgorithmException error) {
      throw new IllegalStateException(error);
    }
  }

  @Transactional(readOnly = true)
  public String template() {
    return db.queryForObject(
        "SELECT template_id FROM codex_blocked_notification_settings WHERE id=1", String.class);
  }

  @Transactional
  public void saveTemplate(String templateId) {
    if (templateId == null || !templateId.trim().matches("[A-Za-z0-9._-]{0,256}"))
      throw new IllegalArgumentException("卡片模板标识无效。");
    db.update(
        "UPDATE codex_blocked_notification_settings SET template_id=? WHERE id=1",
        templateId.trim());
  }

  @Transactional(readOnly = true)
  public JsonNode freezeGroup(String id) {
    if (id == null || id.isBlank()) return json.nullNode();
    // 群停用不阻断业务启动；冻结原身份，投递前再检查可用性并记录无法通知。
    var group = targets.findById(id).orElseThrow(() -> new IllegalArgumentException("通知群不存在。"));
    if (!"GROUP".equals(group.targetType)) throw new IllegalArgumentException("阻断通知只能选择群聊。");
    return snapshot(group);
  }

  private ObjectNode target(String id, String type) {
    var target = targets.findById(id).orElseThrow(() -> new IllegalArgumentException("通知对象不存在。"));
    if (!type.equals(target.targetType) || target.deleted || !target.enabled || !target.available)
      throw new IllegalArgumentException("通知人员或群聊未启用或已不可用。");
    return snapshot(target);
  }

  private ObjectNode snapshot(DingTalkTargetEntity target) {
    return json.createObjectNode()
        .put("id", target.id)
        .put("clientId", target.clientId)
        .put("externalId", target.externalId)
        .put("displayName", target.displayName);
  }

  @Transactional(readOnly = true)
  public JsonNode resolvePerson(String clientId, JsonNode developer) {
    var ids =
        db.queryForList(
            "SELECT target_id FROM codex_jira_developer_mappings WHERE identity_hash=?",
            String.class,
            identity(
                clientId,
                developer.path("accountType").asText(),
                developer.path("accountId").asText()));
    if (ids.isEmpty()) throw new IllegalArgumentException("Jira 开发人尚未配置钉钉映射。");
    return target(ids.get(0), "PERSON");
  }

  @Transactional(readOnly = true)
  public boolean available(JsonNode frozen, String type, String clientId) {
    try {
      var current = target(frozen.path("id").asText(), type);
      return clientId.equals(current.path("clientId").asText())
          && clientId.equals(frozen.path("clientId").asText())
          && current.path("externalId").equals(frozen.path("externalId"));
    } catch (IllegalArgumentException error) {
      return false;
    }
  }

  @Transactional
  public void reserve(String workflowId, JsonNode group) {
    if (!group.isObject()) return;
    insert(workflowId, workflowId, group, null, null, "watching");
  }

  @Transactional
  public void reserveTest(
      String id, String clientId, String groupId, String personId, JsonNode payload) {
    lockConfiguration();
    if (read(id) != null) return; // 客户端超时重试沿用相同编号与原收件人。
    var group = target(groupId, "GROUP");
    var person = target(personId, "PERSON");
    if (!clientId.equals(group.path("clientId").asText())
        || !clientId.equals(person.path("clientId").asText()))
      throw new IllegalArgumentException("请选择当前机器人的群和人员。");
    insert(id, null, group, person, payload, "pending");
  }

  private void insert(
      String id,
      String workflowId,
      JsonNode group,
      JsonNode person,
      JsonNode payload,
      String state) {
    db.update(
        "INSERT INTO codex_blocked_notifications(id,workflow_id,group_json,person_json,payload_json,state,reason,next_at,updated_at) VALUES (?,?,?,?,?,?,?, ?,?)",
        id,
        workflowId,
        json.writeValueAsString(group),
        person == null ? null : json.writeValueAsString(person),
        payload == null ? null : json.writeValueAsString(payload),
        state,
        "",
        now(),
        now());
  }

  @Transactional(readOnly = true)
  public Notice read(String id) {
    var rows =
        db.query(
            "SELECT * FROM codex_blocked_notifications WHERE id=?",
            (r, n) ->
                new Notice(
                    r.getString("id"),
                    r.getString("workflow_id"),
                    json.readTree(r.getString("group_json")),
                    r.getString("person_json") == null
                        ? json.nullNode()
                        : json.readTree(r.getString("person_json")),
                    r.getString("payload_json") == null
                        ? json.nullNode()
                        : json.readTree(r.getString("payload_json")),
                    r.getString("state"),
                    r.getString("reason"),
                    r.getInt("attempts")),
            id);
    return rows.isEmpty() ? null : rows.get(0);
  }

  @Transactional(readOnly = true)
  public Map<String, Object> view(String id) {
    var notice = read(id);
    if (notice == null) return Map.of("state", "not_enabled", "reason", "未启用阻断通知");
    return Map.of(
        "state", notice.state(), "reason", notice.reason(), "attempts", notice.attempts());
  }

  @Transactional
  public List<String> due(String state) {
    // 领取超时不重发；外部可能已经成功。
    db.update(
        "UPDATE codex_blocked_notifications SET state='unknown',reason='发送结果未确认，未自动重发。',updated_at=? WHERE state='sending' AND updated_at<?",
        now(),
        Timestamp.from(Instant.now().minusSeconds(120)));
    return db.queryForList(
        "SELECT id FROM codex_blocked_notifications WHERE state=? AND next_at<=? ORDER BY next_at,id LIMIT 50",
        String.class,
        state,
        now());
  }

  @Transactional
  public boolean poll(String id) {
    return db.update(
            "UPDATE codex_blocked_notifications SET next_at=? WHERE id=? AND state='watching' AND next_at<=?",
            Timestamp.from(Instant.now().plusSeconds(30)),
            id,
            now())
        == 1;
  }

  @Transactional
  public void prepare(String id, JsonNode person, JsonNode payload) {
    db.update(
        "UPDATE codex_blocked_notifications SET person_json=?,payload_json=?,state='pending',next_at=?,updated_at=? WHERE id=? AND state='watching'",
        json.writeValueAsString(person),
        json.writeValueAsString(payload),
        now(),
        now(),
        id);
  }

  @Transactional
  public Notice claim(String id) {
    int changed =
        db.update(
            "UPDATE codex_blocked_notifications SET state='sending',attempts=attempts+1,updated_at=? WHERE id=? AND state='pending' AND next_at<=?",
            now(),
            id,
            now());
    return changed == 1 ? read(id) : null;
  }

  @Transactional
  public void finish(String id, String expected, String state, String reason) {
    db.update(
        "UPDATE codex_blocked_notifications SET state=?,reason=?,updated_at=? WHERE id=? AND state=?",
        state,
        reason,
        now(),
        id,
        expected);
  }

  @Transactional
  public void retry(Notice notice) {
    int[] delays = {30, 120, 600};
    if (notice.attempts() > delays.length) {
      finish(notice.id(), "sending", "failed", "发送失败，已达到重试上限。");
    } else
      db.update(
          "UPDATE codex_blocked_notifications SET state='pending',reason='暂未发送，等待重试。',next_at=?,updated_at=? WHERE id=? AND state='sending'",
          Timestamp.from(Instant.now().plusSeconds(delays[notice.attempts() - 1])),
          now(),
          notice.id());
  }

  private void lockConfiguration() {
    // 配置及测试登记的短事务串行化，让并发重复请求也返回稳定结果。
    db.queryForObject(
        "SELECT id FROM codex_blocked_notification_settings WHERE id=1 FOR UPDATE", Integer.class);
  }

  private static Timestamp now() {
    return Timestamp.from(Instant.now());
  }
}
