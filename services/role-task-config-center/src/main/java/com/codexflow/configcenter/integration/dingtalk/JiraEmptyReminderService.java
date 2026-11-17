package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.*;
import java.sql.Timestamp;
import java.time.*;
import java.util.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 账号级缺任务提醒：持久化领取后发送，结果不明的同一条通知不重发。 */
@Service
public class JiraEmptyReminderService {
  private final JdbcTemplate db;
  private final TransactionTemplate tx;
  private final ObjectMapper json;
  private final GatewayClient gateway;
  private final BlockedNotificationStore targets;
  private final DingTalkSettingsStore settings;
  private final DingTalkTransport transport;
  private final Clock clock;
  private static final org.slf4j.Logger LOG =
      org.slf4j.LoggerFactory.getLogger(JiraEmptyReminderService.class);

  @org.springframework.beans.factory.annotation.Autowired
  public JiraEmptyReminderService(
      JdbcTemplate db,
      PlatformTransactionManager manager,
      ObjectMapper json,
      GatewayClient gateway,
      BlockedNotificationStore targets,
      DingTalkSettingsStore settings,
      DingTalkTransport transport) {
    this(db, manager, json, gateway, targets, settings, transport, Clock.systemUTC());
  }

  JiraEmptyReminderService(
      JdbcTemplate db,
      PlatformTransactionManager manager,
      ObjectMapper json,
      GatewayClient gateway,
      BlockedNotificationStore targets,
      DingTalkSettingsStore settings,
      DingTalkTransport transport,
      Clock clock) {
    this.db = db;
    this.tx = new TransactionTemplate(manager);
    this.json = json;
    this.gateway = gateway;
    this.targets = targets;
    this.settings = settings;
    this.transport = transport;
    this.clock = clock;
  }

  record Row(String id, long version, boolean enabled, ObjectNode config, ObjectNode state) {}

  record Ticket(String id, String delivery, long version, int interval) {}

  private Row read(String id, boolean lock) {
    var rows =
        db.query(
            "SELECT * FROM codex_jira_empty_reminders WHERE account_id=?"
                + (lock ? " FOR UPDATE" : ""),
            (r, n) ->
                new Row(
                    r.getString("account_id"),
                    r.getLong("version"),
                    r.getBoolean("enabled"),
                    (ObjectNode) json.readTree(r.getString("config_json")),
                    (ObjectNode) json.readTree(r.getString("state_json"))),
            id);
    return rows.isEmpty() ? null : rows.get(0);
  }

  private ObjectNode defaults() {
    return json.createObjectNode()
        .put("enabled", false)
        .put("groupId", "")
        .put("personId", "")
        .set("days", json.createArrayNode().add(1).add(2).add(3).add(4).add(5));
  }

  public JsonNode get(String id) {
    GroupService.validateId(id);
    gateway.get("/jira-accounts/" + id);
    Row row = read(id, false);
    ObjectNode result = row == null ? defaults() : row.config().deepCopy();
    result.remove("clientId");
    if (row != null) result.put("enabled", row.enabled());
    if (row == null)
      result.putArray("windows").addObject().put("start", "09:00").put("end", "18:00");
    result.put("version", row == null ? 0 : row.version()).put("timezone", "Asia/Shanghai");
    ObjectNode display = json.createObjectNode().put("phase", "disabled");
    if (row != null) {
      for (String field : List.of("phase", "interval", "lastSentAt", "nextAt", "lastResult"))
        if (row.state().has(field)) display.set(field, row.state().get(field));
    }
    result.set("status", display);
    return result;
  }

  public JsonNode save(String id, JsonNode input) {
    GroupService.validateId(id);
    if (!input.isObject()
        || !input.path("enabled").isBoolean()
        || !input.path("version").isIntegralNumber())
      throw new IllegalArgumentException("提醒配置格式无效。");
    JiraReminderWindow.validate(input);
    ObjectNode config =
        json.createObjectNode()
            .put("enabled", input.path("enabled").asBoolean())
            .put("groupId", input.path("groupId").asText())
            .put("personId", input.path("personId").asText());
    config.set("days", input.path("days"));
    config.set("windows", input.path("windows"));
    if (config.path("enabled").asBoolean()) {
      var identity = validateTargets(config);
      config.put("clientId", identity[0].path("clientId").asText());
    }
    tx.executeWithoutResult(
        status -> {
          // 共用配置写锁，处理首次插入和账号删除/停用的并发。
          db.queryForObject(
              "SELECT id FROM codex_group_write_lock WHERE id=1 FOR UPDATE", Integer.class);
          gateway.get("/jira-accounts/" + id);
          Row old = read(id, true);
          long version = old == null ? 0 : old.version();
          if (input.path("version").asLong() != version)
            throw new ConflictFailure("提醒配置已变化，请刷新后重试。");
          if (old != null) cancelDelivery(old.state());
          ObjectNode state =
              json.createObjectNode()
                  .put("phase", config.path("enabled").asBoolean() ? "unknown" : "disabled")
                  .put("interval", 60)
                  .put("resetAt", clock.instant().toString());
          if (old == null)
            db.update(
                "INSERT INTO codex_jira_empty_reminders(account_id,version,enabled,config_json,state_json) VALUES(?,?,?,?,?)",
                id,
                1,
                config.path("enabled").asBoolean(),
                json.writeValueAsString(config),
                json.writeValueAsString(state));
          else
            db.update(
                "UPDATE codex_jira_empty_reminders SET version=?,enabled=?,config_json=?,state_json=? WHERE account_id=?",
                version + 1,
                config.path("enabled").asBoolean(),
                json.writeValueAsString(config),
                json.writeValueAsString(state),
                id);
        });
    return get(id);
  }

  private JsonNode[] validateTargets(JsonNode config) {
    var current = settings.current();
    if (!current.enabled() || targets.template().isBlank())
      throw new IllegalArgumentException("请先启用钉钉机器人并配置开发人通知卡片模板。");
    JsonNode group = targets.target(config.path("groupId").asText(), "GROUP");
    JsonNode person = targets.target(config.path("personId").asText(), "PERSON");
    if (!current.clientId().equals(group.path("clientId").asText())
        || !current.clientId().equals(person.path("clientId").asText())
        || (config.has("clientId") && !current.clientId().equals(config.path("clientId").asText())))
      throw new IllegalArgumentException("请选择当前机器人的通知群和人员。");
    return new JsonNode[] {group, person};
  }

  public void reconcile() {
    for (String id :
        db.queryForList(
            "SELECT account_id FROM codex_jira_empty_reminders WHERE enabled=TRUE", String.class)) {
      try {
        tick(id);
      } catch (RuntimeException error) {
        // 单账号异常不能中断其他账号；不记录上游原始响应。
        LOG.warn("Jira 缺任务提醒检查失败，账号={}，异常类型={}。", id, error.getClass().getSimpleName());
      }
    }
  }

  private JsonNode observation(String id) {
    try {
      return gateway.get("/jira-accounts/" + id + "/collection-status");
    } catch (RuntimeException error) {
      LOG.warn("Jira 采集状态暂不可用，账号={}，异常类型={}。", id, error.getClass().getSimpleName());
      return json.createObjectNode().put("state", "unknown");
    }
  }

  void tick(String id) {
    JsonNode observed = observation(id);
    Ticket ticket =
        tx.execute(
            status -> {
              Row row = read(id, true);
              if (row == null || !row.enabled()) return null;
              Instant now = clock.instant();
              ObjectNode s = row.state();
              if ("sending".equals(s.path("phase").asText())) {
                if (now.isBefore(Instant.parse(s.path("claimedAt").asText()).plusSeconds(120)))
                  return null;
                finishHistory(s, "unknown");
                s.put("phase", "waiting").put("lastResult", "发送结果未确认，该条未自动重发。");
                s.remove("deliveryId");
              }
              boolean ready = observe(row, observed, now);
              Ticket result = null;
              if (ready && !now.isBefore(Instant.parse(s.path("nextAt").asText()))) {
                String delivery = UUID.randomUUID().toString();
                int interval = s.path("interval").asInt(60);
                s.put("deliveryId", delivery)
                    .put("claimedAt", now.toString())
                    .put("phase", "sending");
                // 先提交领取及保守的下一档。进程在外部发送之后退出时不会重复该条。
                s.put("interval", Math.min(3600, interval * 2))
                    .put("nextAt", now.plusSeconds(Math.min(3600, interval * 2)).toString());
                db.update(
                    "INSERT INTO codex_jira_empty_reminder_deliveries(id,account_id,created_at,status) VALUES(?,?,?,'sending')",
                    delivery,
                    id,
                    Timestamp.from(now));
                result = new Ticket(id, delivery, row.version(), interval);
              }
              persist(row);
              return result;
            });
    if (ticket != null) deliver(ticket);
  }

  private void reset(ObjectNode s, String phase, Instant now) {
    cancelDelivery(s);
    s.put("phase", phase).put("interval", 60).put("resetAt", now.toString());
    s.remove(List.of("nextAt", "emptySince", "deliveryId"));
  }

  private boolean observe(Row row, JsonNode observed, Instant now) {
    ObjectNode s = row.state();
    if ("deleted".equals(observed.path("state").asText())) {
      reset(s, "disabled", now);
      db.update("UPDATE codex_jira_empty_reminders SET enabled=FALSE WHERE account_id=?", row.id());
      return false;
    }
    if ("failed".equals(s.path("phase").asText())) return false;
    var window = JiraReminderWindow.current(row.config(), now);
    if (window == null) {
      reset(s, "outside", now);
      return false;
    }
    String scope = observed.path("scopeVersion").asText();
    String nonempty = observed.path("lastNonemptyAt").asText("");
    if (!window.start().toString().equals(s.path("windowStart").asText())
        || (!scope.isEmpty() && !scope.equals(s.path("scopeVersion").asText()))
        || (!nonempty.isEmpty() && !nonempty.equals(s.path("seenNonempty").asText()))) {
      reset(s, "unknown", now);
      s.put("windowStart", window.start().toString());
      if (!scope.isEmpty()) s.put("scopeVersion", scope);
      s.put("seenNonempty", nonempty);
    }
    String state = observed.path("state").asText();
    if (Set.of("available", "disabled", "unconfigured").contains(state)) {
      reset(s, state, now);
      return false;
    }
    Instant collected =
        observed.path("collectedAt").isTextual()
            ? Instant.parse(observed.path("collectedAt").asText())
            : null;
    if (!"empty".equals(state)
        || collected == null
        || collected.isBefore(now.minusSeconds(120))
        || collected.isAfter(now)
        || collected.isBefore(window.start())
        || collected.isBefore(Instant.parse(s.path("resetAt").asText()))) {
      s.put("phase", "unknown");
      s.remove(List.of("nextAt", "emptySince"));
      return false;
    }
    if (!s.hasNonNull("nextAt")) {
      s.put("nextAt", now.plusSeconds(s.path("interval").asInt(60)).toString());
      s.put("emptySince", now.toString());
    }
    s.put("phase", "waiting");
    return true;
  }

  private void deliver(Ticket ticket) {
    // 在行锁内重新检查并发送，配置保存完成后不会再发送旧配置的通知。
    tx.executeWithoutResult(
        status -> {
          Row row = read(ticket.id(), true);
          if (row == null
              || row.version() != ticket.version()
              || !row.enabled()
              || !ticket.delivery().equals(row.state().path("deliveryId").asText())) return;
          ObjectNode s = row.state();
          Instant now = clock.instant();
          if (!observe(row, observation(ticket.id()), now)
              || !ticket.delivery().equals(s.path("deliveryId").asText())) {
            db.update(
                "UPDATE codex_jira_empty_reminder_deliveries SET status='cancelled' WHERE id=? AND status='sending'",
                ticket.delivery());
            // 领取后复查失败并未发送，保留原档位；新任务或新窗口已由 reset 归零。
            if (ticket.delivery().equals(s.path("deliveryId").asText()))
              s.put("interval", ticket.interval());
            s.remove("deliveryId");
            persist(row);
            return;
          }
          String outcome = "delivered";
          try {
            JsonNode[] identity = validateTargets(row.config());
            JsonNode account = gateway.get("/jira-accounts/" + ticket.id());
            if (!account.path("enabled").asBoolean()) {
              reset(s, "disabled", now);
              persist(row);
              return;
            }
            // 发起请求前再核对时段，网络检查可能跨越边界。
            var sendWindow = JiraReminderWindow.current(row.config(), clock.instant());
            if (sendWindow == null
                || !sendWindow.start().toString().equals(s.path("windowStart").asText())) {
              reset(s, "outside", clock.instant());
              persist(row);
              return;
            }
            Map<String, Object> card = new LinkedHashMap<>();
            card.put("_templateId", targets.template());
            card.put("_emptyReminder", true);
            card.put("_sendBefore", sendWindow.end().toString());
            card.put("_atDisplayName", identity[1].path("displayName").asText());
            card.put("title", "Jira 暂无待处理工单");
            card.put(
                "body",
                BlockedNotificationService.safe(
                    "Jira 账号「"
                        + account.path("name").asText()
                        + "」当前配置的领取范围暂无待处理工单。\n若仍无任务，下次将在"
                        + s.path("interval").asInt() / 60
                        + "分钟后提醒（仅在生效时段内）。"));
            card.put("detailLink", "");
            transport.sendBlockedCard(
                "jira-empty-" + ticket.delivery(),
                identity[0].path("clientId").asText(),
                identity[0].path("externalId").asText(),
                identity[1].path("externalId").asText(),
                card);
            s.put("lastSentAt", clock.instant().toString()).put("lastResult", "已投递");
          } catch (DingTalkTransport.DeliveryExpired error) {
            reset(s, "outside", clock.instant());
            persist(row);
            return;
          } catch (BlockedNotificationService.NotSentFailure error) {
            outcome = error.retryable ? "retryable" : "failed";
            s.put("lastResult", error.retryable ? "暂未发送，将按当前间隔重试。" : "通知被拒绝，请检查群、人员和模板后重新保存配置。");
            if (error.retryable) s.put("interval", ticket.interval());
          } catch (GatewayFailure error) {
            // 账号信息读取失败发生在投递之前，不能当作外部发送结果不明。
            outcome = "retryable";
            s.put("interval", ticket.interval()).put("lastResult", "账号状态暂不可用，尚未发送，将重新检查。");
            LOG.warn("Jira 提醒发送前账号核验失败，账号={}。", ticket.id());
          } catch (IllegalArgumentException error) {
            outcome = "failed";
            s.put("lastResult", "通知配置不可用，请检查群、人员和模板后重新保存配置。");
          } catch (RuntimeException error) {
            outcome = "unknown";
            s.put("lastResult", "发送结果未确认，该条未自动重发。");
            LOG.warn(
                "Jira 缺任务提醒发送结果未确认，通知={}，异常类型={}。",
                ticket.delivery(),
                error.getClass().getSimpleName());
          }
          finishHistory(s, outcome);
          s.remove("deliveryId");
          s.put("phase", outcome.equals("failed") ? "failed" : "waiting");
          if (outcome.equals("failed")) s.remove("nextAt");
          else s.put("nextAt", clock.instant().plusSeconds(s.path("interval").asInt()).toString());
          persist(row);
        });
  }

  private void finishHistory(ObjectNode state, String outcome) {
    if (state.hasNonNull("deliveryId"))
      db.update(
          "UPDATE codex_jira_empty_reminder_deliveries SET status=? WHERE id=? AND status='sending'",
          outcome,
          state.path("deliveryId").asText());
  }

  private void cancelDelivery(ObjectNode state) {
    finishHistory(state, "cancelled");
  }

  private void persist(Row row) {
    db.update(
        "UPDATE codex_jira_empty_reminders SET state_json=? WHERE account_id=?",
        json.writeValueAsString(row.state()),
        row.id());
  }
}
