package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.BlockedNotificationStore;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@SpringBootTest
@Transactional
class BlockedNotificationTest {
  @Autowired BlockedNotificationStore store;
  @Autowired JdbcTemplate db;
  @Autowired ObjectMapper json;
  @Autowired jakarta.persistence.EntityManager entities;
  private BlockedNotificationService service;
  private DingTalkTransport transport;
  private GatewayClient gateway;
  private WorkflowRunStore runs;
  private String group;
  private String person;
  private String client;

  @BeforeEach
  void setup() {
    client = "client-" + UUID.randomUUID();
    group = target("GROUP", "group");
    person = target("PERSON", "developer");
    var settings = mock(DingTalkSettingsStore.class);
    when(settings.current())
        .thenReturn(
            new DingTalkSettingsStore.Settings(true, client, "unused-test", "", 1000, true));
    gateway = mock(GatewayClient.class);
    transport = mock(DingTalkTransport.class);
    runs = mock(WorkflowRunStore.class);
    service = new BlockedNotificationService(store, settings, transport, gateway, runs, json);
    store.saveTemplate("blocked.schema");
  }

  private String target(String type, String external) {
    String id = UUID.randomUUID().toString();
    db.update(
        "INSERT INTO codex_sop_dingtalk_targets(id,client_id,target_type,external_id,display_name,source,available,enabled,deleted) VALUES (?,?,?,?,?,'DIRECTORY',true,true,false)",
        id,
        client,
        type,
        external,
        "developer".equals(external) ? "开发人" : external);
    return id;
  }

  private String reserve() {
    String id = UUID.randomUUID().toString();
    store.reserve(id, store.freezeGroup(group));
    when(runs.runStatus(id)).thenReturn("running");
    when(runs.monitorUrl(id)).thenReturn("https://console.example.org/?workflowId=" + id);
    when(gateway.get("/workflows/" + id)).thenReturn(runtime());
    return id;
  }

  private ObjectNode runtime() {
    var result = json.createObjectNode().put("status", "failed");
    result.putArray("nodes").addObject().put("id", "step").put("displayName", "检查代码");
    var termination =
        result
            .putObject("termination")
            .put("outcome", "blocked")
            .put("nodeId", "step")
            .put("reason", "缺少源码");
    termination
        .putObject("jiraDeveloper")
        .put("status", "resolved")
        .put("accountType", "key")
        .put("accountId", "dev-1")
        .put("displayName", "开发人");
    termination
        .putObject("jiraComment")
        .put("issueKey", "TEST-1")
        .put("status", "unknown")
        .put("detail", "角色修改失败，评论未确认");
    return result;
  }

  @Test
  void blockedUsesDeveloperAndFrozenTargetOnce() {
    String id = reserve();
    service.observe(store.read(id));
    assertThat(store.read(id).person().path("externalId").asText()).isEqualTo("developer");
    assertThat(store.read(id).payload().path("body").asText()).contains("第1步", "评论未确认");
    String other = target("PERSON", "other");
    db.update("UPDATE codex_sop_dingtalk_targets SET display_name=? WHERE id=?", "开发人", other);
    db.update("UPDATE codex_sop_dingtalk_targets SET display_name=? WHERE id=?", "新名称", person);
    service.observe(store.read(id));
    service.send(id);
    service.send(id);
    verify(transport, times(1))
        .sendBlockedCard(
            eq("blocked-" + id),
            eq(client),
            eq("group"),
            eq("developer"),
            argThat(data -> "开发人".equals(data.get("_atDisplayName"))));
    assertThat(store.view(id).get("state")).isEqualTo("delivered");
  }

  @Test
  void onlyConfirmedBlockedTerminalTriggers() {
    for (String state : new String[] {"running", "cancelled", "completed"}) {
      String id = reserve();
      var value = runtime().put("status", state);
      when(gateway.get("/workflows/" + id)).thenReturn(value);
      service.observe(store.read(id));
      assertThat(store.read(id).state())
          .isEqualTo(state.equals("running") ? "watching" : "not_applicable");
    }
    for (String outcome : new String[] {"success", "no_task", ""}) {
      String id = reserve();
      var value = runtime();
      ((ObjectNode) value.path("termination")).put("outcome", outcome);
      when(gateway.get("/workflows/" + id)).thenReturn(value);
      service.observe(store.read(id));
      assertThat(store.read(id).state()).isEqualTo("not_applicable");
    }
    verifyNoInteractions(transport);
  }

  @Test
  void missingMultipleAndOldProtocolDoNotGuessRecipients() {
    for (String status : new String[] {"empty", "multiple", "unavailable", ""}) {
      String id = reserve();
      var value = runtime();
      ((ObjectNode) value.path("termination").path("jiraDeveloper")).put("status", status);
      when(gateway.get("/workflows/" + id)).thenReturn(value);
      service.observe(store.read(id));
      assertThat(store.read(id).state()).isEqualTo("unavailable");
    }
    verifyNoInteractions(transport);
  }

  @Test
  void namesMatchExactlyAndDuplicatesChooseStableId() {
    db.update("UPDATE codex_sop_dingtalk_targets SET display_name=? WHERE id=?", "  开发人  ", person);
    entities.clear();
    assertThat(store.resolvePerson(client, " 开发人 ").path("id").asText()).isEqualTo(person);
    String other = target("PERSON", "other");
    db.update("UPDATE codex_sop_dingtalk_targets SET display_name=? WHERE id=?", "开发人", other);
    entities.clear();
    String expected = person.compareTo(other) < 0 ? person : other;
    assertThat(store.resolvePerson(client, "开发人").path("id").asText()).isEqualTo(expected);
    assertThatThrownBy(() -> store.resolvePerson(client, "开发"))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> store.resolvePerson("another-bot", "开发人"))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> store.resolvePerson(client, " "))
        .isInstanceOf(IllegalArgumentException.class);
    target("PERSON", "Alice");
    assertThatThrownBy(() -> store.resolvePerson(client, "alice"))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void unavailableDeletedAndGroupNamesAreNotRecipients() {
    for (String column : new String[] {"available", "deleted"}) {
      boolean value = "deleted".equals(column);
      db.update("UPDATE codex_sop_dingtalk_targets SET " + column + "=? WHERE id=?", value, person);
      entities.clear();
      assertThatThrownBy(() -> store.resolvePerson(client, "开发人"))
          .isInstanceOf(IllegalArgumentException.class);
      db.update(
          "UPDATE codex_sop_dingtalk_targets SET " + column + "=? WHERE id=?", !value, person);
    }
    assertThatThrownBy(() -> store.resolvePerson(client, "group"))
        .isInstanceOf(IllegalArgumentException.class);
  }

  private ObjectNode completedRuntime() {
    var value =
        json.createObjectNode()
            .put("status", "completed")
            .put("advanceMode", "automatic")
            .put("response", "已完成代码审核。 token=secret");
    value
        .putObject("jiraContext")
        .put("issueKey", "TEST-2")
        .put("developerStatus", "resolved")
        .put("developerName", "开发人");
    return value;
  }

  @Test
  void automaticCompletionNotifiesByDisplayNameWithoutAccountMapping() {
    String id = reserve();
    when(gateway.get("/workflows/" + id)).thenReturn(completedRuntime());
    service.observe(store.read(id));
    var notice = store.read(id);
    assertThat(notice.state()).isEqualTo("pending");
    assertThat(notice.payload().path("title").asText()).isEqualTo("Jira 自动流程已完成");
    assertThat(notice.payload().path("body").asText())
        .contains("TEST-2", "开发人", "已完成代码审核")
        .doesNotContain("secret");
    assertThat(notice.payload().path("detailLink").asText()).contains(id);
    // 重启后只依赖持久化的 pending 记录投递；无需重新观察或读取 Jira。
    var settings = mock(DingTalkSettingsStore.class);
    when(settings.current())
        .thenReturn(new DingTalkSettingsStore.Settings(true, client, "test", "", 1000, true));
    var restarted = new BlockedNotificationService(store, settings, transport, gateway, runs, json);
    restarted.send(id);
    restarted.observe(store.read(id));
    restarted.send(id);
    verify(transport, times(1))
        .sendBlockedCard(eq("blocked-" + id), eq(client), eq("group"), eq("developer"), anyMap());
    assertThat(store.read(id).state()).isEqualTo("delivered");
  }

  @Test
  void completedNotificationExcludesOtherModesAndEarlyTermination() {
    for (String kind :
        new String[] {"semi", "no_task", "blocked", "missing_jira", "cancelled", "failed"}) {
      String id = reserve();
      var value = completedRuntime();
      switch (kind) {
        case "semi" -> value.put("advanceMode", "semi_automatic");
        case "no_task", "blocked" -> value.putObject("termination").put("outcome", kind);
        case "missing_jira" -> ((ObjectNode) value.path("jiraContext")).put("issueKey", "");
        default -> value.put("status", kind);
      }
      when(gateway.get("/workflows/" + id)).thenReturn(value);
      service.observe(store.read(id));
      assertThat(store.read(id).state()).as(kind).isEqualTo("not_applicable");
    }
    verifyNoInteractions(transport);
  }

  @Test
  void completedNotificationReportsMissingDeveloperWithoutGuessing() {
    for (String status :
        new String[] {"empty", "multiple", "unavailable", "not_applicable", "resolved"}) {
      String id = reserve();
      var value = completedRuntime();
      ((ObjectNode) value.path("jiraContext"))
          .put("developerStatus", status)
          .put("developerName", "");
      when(gateway.get("/workflows/" + id)).thenReturn(value);
      service.observe(store.read(id));
      assertThat(store.read(id).state()).isEqualTo("unavailable");
    }
    verifyNoInteractions(transport);
  }

  @Test
  void unavailableNamesTemplateAndTargetsNeverSend() {
    String id = reserve();
    store.saveTemplate("");
    service.observe(store.read(id));
    assertThat(store.read(id).state()).isEqualTo("unavailable");
    store.saveTemplate("blocked.schema");
    id = reserve();
    db.update("UPDATE codex_sop_dingtalk_targets SET display_name=? WHERE id=?", "其他姓名", person);
    entities.clear();
    service.observe(store.read(id));
    assertThat(store.read(id).state()).isEqualTo("unavailable");
    db.update("UPDATE codex_sop_dingtalk_targets SET display_name=? WHERE id=?", "开发人", person);
    entities.clear();
    id = reserve();
    service.observe(store.read(id));
    db.update("UPDATE codex_sop_dingtalk_targets SET available=false WHERE id=?", person);
    entities.clear();
    service.send(id);
    assertThat(store.read(id).state()).isEqualTo("unavailable");
    verifyNoInteractions(transport);
  }

  @Test
  void disabledPeopleReceiveCompletedAndBlockedNotifications() {
    for (boolean completed : new boolean[] {true, false}) {
      db.update("UPDATE codex_sop_dingtalk_targets SET enabled=false WHERE id=?", person);
      entities.clear();
      String id = reserve();
      when(gateway.get("/workflows/" + id)).thenReturn(completed ? completedRuntime() : runtime());
      service.observe(store.read(id));
      assertThat(store.read(id).state()).isEqualTo("pending");
      service.send(id);
      assertThat(store.read(id).state()).isEqualTo("delivered");
      verify(transport)
          .sendBlockedCard(eq("blocked-" + id), eq(client), eq("group"), eq("developer"), anyMap());
    }
  }

  @Test
  void disablingPersonAfterPreparationDoesNotBlockDelivery() {
    String id = reserve();
    service.observe(store.read(id));
    db.update("UPDATE codex_sop_dingtalk_targets SET enabled=false WHERE id=?", person);
    entities.clear();
    service.send(id);
    assertThat(store.read(id).state()).isEqualTo("delivered");
  }

  @Test
  void notificationTestAlsoAcceptsAvailableDisabledPerson() {
    db.update("UPDATE codex_sop_dingtalk_targets SET enabled=false WHERE id=?", person);
    entities.clear();
    String request = UUID.randomUUID().toString();
    service.test(request, group, person, "测试");
    service.send("test-" + request);
    assertThat(store.read("test-" + request).state()).isEqualTo("delivered");
  }

  @Test
  void timeoutIsUnknownAndNeverResent() {
    String id = reserve();
    service.observe(store.read(id));
    doThrow(new IllegalStateException("timeout secret=do-not-display"))
        .when(transport)
        .sendBlockedCard(anyString(), anyString(), anyString(), anyString(), anyMap());
    service.send(id);
    service.send(id);
    assertThat(store.read(id).state()).isEqualTo("unknown");
    assertThat(store.read(id).reason()).doesNotContain("secret");
    verify(transport, times(1))
        .sendBlockedCard(anyString(), anyString(), anyString(), anyString(), anyMap());
  }

  @Test
  void interruptedDeliveryRecoversToUnknown() {
    String id = reserve();
    service.observe(store.read(id));
    assertThat(store.claim(id)).isNotNull();
    assertThat(store.claim(id)).isNull();
    db.update(
        "UPDATE codex_blocked_notifications SET updated_at=? WHERE id=?",
        Timestamp.from(Instant.now().minusSeconds(180)),
        id);
    store.due("pending");
    assertThat(store.read(id).state()).isEqualTo("unknown");
    service.send(id);
    verifyNoInteractions(transport);
  }

  @Test
  void explicitNotSentFailureRetriesThreeTimesWithSameCard() {
    String id = reserve();
    service.observe(store.read(id));
    doThrow(new BlockedNotificationService.NotSentFailure(true))
        .when(transport)
        .sendBlockedCard(anyString(), anyString(), anyString(), anyString(), anyMap());
    for (int attempt = 0; attempt < 4; attempt++) {
      service.send(id);
      if (attempt < 3) {
        assertThat(store.read(id).state()).isEqualTo("pending");
        db.update(
            "UPDATE codex_blocked_notifications SET next_at=? WHERE id=?",
            Timestamp.from(Instant.now().minusSeconds(1)),
            id);
      }
    }
    assertThat(store.read(id).state()).isEqualTo("failed");
    verify(transport, times(4))
        .sendBlockedCard(eq("blocked-" + id), eq(client), eq("group"), eq("developer"), anyMap());
  }

  @Test
  void testRequestIsIdempotentAndDoesNotReadJiraOrLaunchWorkflow() {
    String request = UUID.randomUUID().toString();
    service.test(request, group, person, "TEST-1 已阻断");
    service.test(request, group, person, "不同内容");
    service.send("test-" + request);
    service.send("test-" + request);
    assertThat(store.read("test-" + request).payload().path("body").asText())
        .contains("TEST-1")
        .doesNotContain("不同内容");
    verifyNoInteractions(gateway, runs);
    verify(transport, times(1))
        .sendBlockedCard(anyString(), eq(client), eq("group"), eq("developer"), anyMap());
  }

  @Test
  void disabledGroupDoesNotPreventSnapshotButCannotReceiveNotification() {
    db.update("UPDATE codex_sop_dingtalk_targets SET enabled=false WHERE id=?", group);
    entities.clear();
    String id = reserve();
    service.observe(store.read(id));
    service.send(id);
    assertThat(store.read(id).state()).isEqualTo("unavailable");
    verifyNoInteractions(transport);
  }

  @Test
  void safeCardDoesNotExposeInternalData() {
    assertThat(
            BlockedNotificationService.safe(
                "token=abc 10.0.0.1:8080 threadId=secret https://internal.example/path"))
        .doesNotContain("abc", "10.0.0.1", "secret", "https://internal");
  }

  @Test
  void cardBodyUsesExplicitMentionAndDedicatedTemplate() {
    var properties = new DingTalkProperties();
    properties.setClientId(client);
    var official = new OfficialDingTalkTransport(properties, json);
    var body =
        official.waitingCardBody(
            "blocked-id",
            "GROUP",
            "group",
            "developer",
            Map.of("_templateId", "blocked.schema", "title", "阻断"));
    assertThat(body.path("cardTemplateId").asText()).isEqualTo("blocked.schema");
    assertThat(body.path("imGroupOpenDeliverModel").path("atUserIds").has("developer")).isTrue();
  }
}
