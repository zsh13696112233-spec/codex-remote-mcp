package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.GroupedFixtureSupport;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import tools.jackson.databind.node.JsonNodeFactory;

@SpringBootTest
class JiraAccountServiceTest extends GroupedFixtureSupport {
  @Autowired JiraAccountService accounts;
  @Autowired GroupService groups;
  @Autowired JdbcTemplate jdbc;

  @Test
  void validatesGroupAndUsabilityForSopSelection() {
    String id = UUID.randomUUID().toString();
    var row =
        JsonNodeFactory.instance
            .objectNode()
            .put("groupId", GROUP)
            .put("enabled", true)
            .put("testStatus", "passed")
            .put("executorAlias", "bot");
    when(groupGateway.get("/jira-accounts/" + id)).thenReturn(row);
    assertThat(groups.requireJira(id, GROUP)).isSameAs(row);
    row.put("executorAlias", "");
    assertThatThrownBy(() -> groups.requireJira(id, GROUP)).hasMessageContaining("自动映射");
    row.put("executorAlias", "bot");
    row.put("enabled", false);
    assertThatThrownBy(() -> groups.requireJira(id, GROUP)).isInstanceOf(ConflictFailure.class);
    row.put("enabled", true).put("groupId", UUID.randomUUID().toString());
    assertThatThrownBy(() -> groups.requireJira(id, GROUP)).hasMessageContaining("同一分组");
    assertThatThrownBy(() -> groups.requireJira(null, GROUP)).hasMessageContaining("未绑定");
  }

  @Test
  void validSopReferencePreventsGatewayDeletion() {
    String id = UUID.randomUUID().toString();
    String sop =
        jdbc.queryForObject(
            "SELECT id FROM codex_sop_sops ORDER BY created_at LIMIT 1", String.class);
    jdbc.update("UPDATE codex_sop_sops SET jira_connection_id=? WHERE id=?", id, sop);
    try {
      assertThatThrownBy(() -> accounts.delete(id, 1)).hasMessageContaining("SOP");
      verify(groupGateway, never()).delete("/jira-accounts/" + id + "?version=1");
    } finally {
      jdbc.update("UPDATE codex_sop_sops SET jira_connection_id=NULL WHERE id=?", sop);
    }
  }

  @Test
  void pendingRequestReferencePreventsDeletion() {
    String id = UUID.randomUUID().toString(), request = UUID.randomUUID().toString();
    jdbc.update(
        "INSERT INTO codex_jira_requests(id,task_definition_id,source,spec_json,state) VALUES(?,?,'web',?,'waiting')",
        request,
        UUID.randomUUID().toString(),
        "{\"jira\":{\"connectionId\":\"" + id + "\"}}");
    try {
      assertThatThrownBy(() -> accounts.delete(id, 1)).hasMessageContaining("待派发");
    } finally {
      jdbc.update("DELETE FROM codex_jira_requests WHERE id=?", request);
    }
  }

  @Test
  void forwardsOnlyVersionedDeletionAndNeverStoresTokenInMysql() {
    String id = UUID.randomUUID().toString();
    var response = JsonNodeFactory.instance.objectNode().put("deleted", true);
    when(groupGateway.delete("/jira-accounts/" + id + "?version=7")).thenReturn(response);
    assertThat(accounts.delete(id, 7)).isSameAs(response);
    var body =
        JsonNodeFactory.instance.objectNode().put("groupId", GROUP).put("token", "fixture-token");
    when(groupGateway.post(eq("/jira-accounts"), any()))
        .thenReturn(JsonNodeFactory.instance.objectNode().put("id", id));
    assertThat(accounts.save(null, body).has("token")).isFalse();
    verify(groupGateway).post("/jira-accounts", body);
  }
}
