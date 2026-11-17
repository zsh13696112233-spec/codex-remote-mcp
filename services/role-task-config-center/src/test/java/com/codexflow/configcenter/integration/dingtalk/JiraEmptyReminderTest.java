package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.*;
import tools.jackson.databind.*;
import tools.jackson.databind.node.ObjectNode;

class JiraEmptyReminderTest {
  @Test
  void accountLookupFailureBeforeDeliveryKeepsCurrentInterval() {
    service.tick(id);
    clock.advance(60);
    when(gateway.get("/jira-accounts/" + id))
        .thenThrow(new com.codexflow.configcenter.client.GatewayFailure(502, "unavailable"));
    service.tick(id);
    sends(0);
    var stored =
        json.readTree(
            db.queryForObject(
                "SELECT state_json FROM codex_jira_empty_reminders WHERE account_id=?",
                String.class,
                id));
    assertThat(stored.path("interval").asInt()).isEqualTo(60);
    assertThat(stored.path("lastResult").asText()).contains("尚未发送");
  }

  @Test
  void transportWindowExpiryCancelsWithoutRetryOutsideWindow() {
    doThrow(new DingTalkTransport.DeliveryExpired())
        .when(transport)
        .sendBlockedCard(anyString(), anyString(), anyString(), anyString(), anyMap());
    service.tick(id);
    clock.advance(60);
    service.tick(id);
    assertThat(status().path("phase").asText()).isEqualTo("outside");
    assertThat(status().has("nextAt")).isFalse();
    assertThat(
            db.queryForObject(
                "SELECT status FROM codex_jira_empty_reminder_deliveries", String.class))
        .isEqualTo("cancelled");
  }

  ObjectMapper json = new ObjectMapper();
  JdbcTemplate db;
  DataSourceTransactionManager manager;
  GatewayClient gateway = mock(GatewayClient.class);
  DingTalkTransport transport = mock(DingTalkTransport.class);
  BlockedNotificationStore targets = mock(BlockedNotificationStore.class);
  DingTalkSettingsStore settings = mock(DingTalkSettingsStore.class);
  MutableClock clock = new MutableClock();
  JiraEmptyReminderService service;
  String id = UUID.randomUUID().toString();
  String observed = "empty", scope = "a", nonempty = "";
  ObjectNode config;

  static class MutableClock extends Clock {
    Instant now = Instant.parse("2026-11-12T01:00:00Z");

    public ZoneId getZone() {
      return ZoneOffset.UTC;
    }

    public Clock withZone(ZoneId zone) {
      return this;
    }

    public Instant instant() {
      return now;
    }

    void advance(long seconds) {
      now = now.plusSeconds(seconds);
    }
  }

  @BeforeEach
  void setup() {
    var data =
        new DriverManagerDataSource(
            "jdbc:h2:mem:reminder-"
                + UUID.randomUUID()
                + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1",
            "sa",
            "");
    Flyway.configure().dataSource(data).load().migrate();
    db = new JdbcTemplate(data);
    manager = new DataSourceTransactionManager(data);
    when(settings.current())
        .thenReturn(new DingTalkSettingsStore.Settings(true, "client", "unused", "", 1000, true));
    when(targets.template()).thenReturn("notice.schema");
    when(targets.target(anyString(), anyString()))
        .thenAnswer(
            a ->
                json.createObjectNode()
                    .put("clientId", "client")
                    .put("externalId", a.<String>getArgument(0))
                    .put("displayName", "张三"));
    when(gateway.get("/jira-accounts/" + id))
        .thenReturn(json.createObjectNode().put("enabled", true).put("name", "测试账号"));
    when(gateway.get("/jira-accounts/" + id + "/collection-status"))
        .thenAnswer(
            a ->
                json.createObjectNode()
                    .put("state", observed)
                    .put("scopeVersion", scope)
                    .put("lastNonemptyAt", nonempty)
                    .put("collectedAt", clock.instant().toString()));
    service = newService();
    config =
        (ObjectNode)
            json.readTree(
                "{\"version\":0,\"enabled\":true,\"groupId\":\"group\",\"personId\":\"person\",\"days\":[1,2,3,4,5],\"windows\":[{\"start\":\"09:00\",\"end\":\"18:00\"}]}");
    service.save(id, config);
  }

  JiraEmptyReminderService newService() {
    return new JiraEmptyReminderService(
        db, manager, json, gateway, targets, settings, transport, clock);
  }

  JsonNode status() {
    return service.get(id).path("status");
  }

  void sends(int times) {
    verify(transport, times(times))
        .sendBlockedCard(anyString(), anyString(), anyString(), anyString(), anyMap());
  }

  @Test
  void exponentialIntervalsCapAndRestartPersists() {
    service.tick(id);
    clock.advance(59);
    service.tick(id);
    sends(0);
    clock.advance(1);
    service.tick(id);
    sends(1);
    service = newService();
    int count = 1;
    for (int delay : new int[] {120, 240, 480, 960, 1920, 3600, 3600}) {
      clock.advance(delay - 1);
      service.tick(id);
      sends(count);
      clock.advance(1);
      service.tick(id);
      sends(++count);
    }
    assertThat(status().path("interval").asInt()).isEqualTo(3600);
  }

  @Test
  void workAndBriefNonemptyHistoryReset() {
    service.tick(id);
    clock.advance(60);
    service.tick(id);
    sends(1);
    nonempty = clock.instant().toString(); // 两次检查之间出现又消失的工单。
    clock.advance(30);
    service.tick(id);
    assertThat(status().path("interval").asInt()).isEqualTo(60);
    clock.advance(59);
    service.tick(id);
    sends(1);
    clock.advance(1);
    service.tick(id);
    sends(2);
    observed = "available";
    clock.advance(30);
    service.tick(id);
    assertThat(status().has("nextAt")).isFalse();
  }

  @Test
  void outsideWindowNeverSendsAndNextWindowStartsFresh() {
    clock.now = Instant.parse("2026-11-12T09:59:30Z");
    service.tick(id);
    clock.advance(30);
    service.tick(id);
    sends(0);
    assertThat(status().path("phase").asText()).isEqualTo("outside");
    clock.now = Instant.parse("2026-11-13T01:00:00Z");
    service.tick(id);
    clock.advance(60);
    service.tick(id);
    sends(1);
  }

  @Test
  void unknownInterruptsTimerWithoutIncreasingInterval() {
    service.tick(id);
    clock.advance(60);
    service.tick(id);
    observed = "unknown";
    clock.advance(120);
    service.tick(id);
    sends(1);
    observed = "empty";
    service.tick(id);
    clock.advance(119);
    service.tick(id);
    sends(1);
    clock.advance(1);
    service.tick(id);
    sends(2);
  }

  @Test
  void recheckCancelsWhenWorkArrivesAtSend() {
    service.tick(id);
    clock.advance(60);
    when(gateway.get("/jira-accounts/" + id + "/collection-status"))
        .thenReturn(
            json.createObjectNode()
                .put("state", "empty")
                .put("scopeVersion", "a")
                .put("collectedAt", clock.instant().toString()))
        .thenReturn(json.createObjectNode().put("state", "available").put("scopeVersion", "a"));
    service.tick(id);
    sends(0);
    assertThat(
            db.queryForObject(
                "SELECT status FROM codex_jira_empty_reminder_deliveries", String.class))
        .isEqualTo("cancelled");
  }

  @Test
  void temporaryFailureRetriesAtCurrentIntervalAndUnknownDoesNotRepeat() {
    doThrow(new BlockedNotificationService.NotSentFailure(true))
        .doThrow(new IllegalStateException("unknown"))
        .doNothing()
        .when(transport)
        .sendBlockedCard(anyString(), anyString(), anyString(), anyString(), anyMap());
    service.tick(id);
    clock.advance(60);
    service.tick(id);
    sends(1);
    assertThat(status().path("interval").asInt()).isEqualTo(60);
    clock.advance(60);
    service.tick(id);
    sends(2);
    assertThat(status().path("interval").asInt()).isEqualTo(120);
    service.tick(id);
    sends(2);
    clock.advance(120);
    service.tick(id);
    sends(3);
    assertThat(
            db.queryForList(
                "SELECT status FROM codex_jira_empty_reminder_deliveries", String.class))
        .containsExactlyInAnyOrder("retryable", "unknown", "delivered");
  }

  @Test
  void permanentFailureRequiresConfigurationSave() {
    doThrow(new BlockedNotificationService.NotSentFailure(false))
        .when(transport)
        .sendBlockedCard(anyString(), anyString(), anyString(), anyString(), anyMap());
    service.tick(id);
    clock.advance(60);
    service.tick(id);
    clock.advance(3600);
    service.tick(id);
    sends(1);
    assertThat(status().path("phase").asText()).isEqualTo("failed");
    config.put("version", 1);
    service.save(id, config);
    service.tick(id);
    assertThat(status().path("phase").asText()).isEqualTo("waiting");
  }

  @Test
  void concurrentInstancesOnlyDeliverOnce() throws Exception {
    service.tick(id);
    clock.advance(60);
    var executor = Executors.newFixedThreadPool(2);
    try {
      var first = executor.submit(() -> service.tick(id));
      var second = executor.submit(() -> newService().tick(id));
      first.get(10, TimeUnit.SECONDS);
      second.get(10, TimeUnit.SECONDS);
      sends(1);
    } finally {
      executor.shutdownNow();
    }
  }

  @Test
  void staleConfigurationRejectedAndDisableStopsTimer() {
    assertThatThrownBy(() -> service.save(id, config)).isInstanceOf(ConflictFailure.class);
    config.put("version", 1).put("enabled", false);
    service.save(id, config);
    service.tick(id);
    clock.advance(600);
    service.tick(id);
    sends(0);
    assertThat(status().path("phase").asText()).isEqualTo("disabled");
  }

  @Test
  void crashedSendingIsNotResent() {
    service.tick(id);
    var s =
        (ObjectNode)
            json.readTree(
                db.queryForObject(
                    "SELECT state_json FROM codex_jira_empty_reminders WHERE account_id=?",
                    String.class,
                    id));
    s.put("phase", "sending")
        .put("claimedAt", clock.instant().toString())
        .put("deliveryId", "interrupted")
        .put("interval", 120)
        .put("nextAt", clock.instant().plusSeconds(300).toString());
    db.update(
        "UPDATE codex_jira_empty_reminders SET state_json=? WHERE account_id=?",
        json.writeValueAsString(s),
        id);
    db.update(
        "INSERT INTO codex_jira_empty_reminder_deliveries VALUES('interrupted',?,CURRENT_TIMESTAMP,'sending')",
        id);
    clock.advance(120);
    service = newService();
    service.tick(id);
    sends(0);
    assertThat(
            db.queryForObject(
                "SELECT status FROM codex_jira_empty_reminder_deliveries", String.class))
        .isEqualTo("unknown");
  }

  @Test
  void failedPreSendObservationDoesNotDoubleTheInterval() {
    service.tick(id);
    clock.advance(60);
    when(gateway.get("/jira-accounts/" + id + "/collection-status"))
        .thenReturn(
            json.createObjectNode()
                .put("state", "empty")
                .put("scopeVersion", "a")
                .put("collectedAt", clock.instant().toString()))
        .thenReturn(json.createObjectNode().put("state", "unknown"));
    service.tick(id);
    sends(0);
    assertThat(status().path("interval").asInt()).isEqualTo(60);
    assertThat(status().has("nextAt")).isFalse();
  }

  @Test
  void accountDeletionStopsFuturePolling() {
    service.tick(id);
    observed = "deleted";
    clock.advance(30);
    service.tick(id);
    assertThat(
            db.queryForObject(
                "SELECT enabled FROM codex_jira_empty_reminders WHERE account_id=?",
                Boolean.class,
                id))
        .isFalse();
    assertThat(status().path("phase").asText()).isEqualTo("disabled");
    clearInvocations(gateway);
    service.reconcile();
    verifyNoInteractions(gateway);
  }

  @Test
  void changedScopeRestartsAtOneMinute() {
    service.tick(id);
    clock.advance(60);
    service.tick(id);
    sends(1);
    scope = "b";
    clock.advance(30);
    service.tick(id);
    assertThat(status().path("interval").asInt()).isEqualTo(60);
    clock.advance(60);
    service.tick(id);
    sends(2);
  }
}
