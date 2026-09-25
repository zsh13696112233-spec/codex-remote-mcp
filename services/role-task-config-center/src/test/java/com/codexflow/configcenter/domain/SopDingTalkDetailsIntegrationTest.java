package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import com.codexflow.configcenter.GroupedFixtureSupport;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import jakarta.persistence.EntityManager;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@SpringBootTest
@Transactional
class SopDingTalkDetailsIntegrationTest extends GroupedFixtureSupport {
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired JdbcTemplate jdbc;
  @Autowired EntityManager entities;
  @Autowired ObjectMapper json;

  private SopSaveRequest request(Boolean show) {
    String role =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    var step =
        new SopStepRequest(
            "执行步骤", role, "执行", null, "local", "local", null, false, null, null, 1800, Set.of(),
            Set.of());
    return new SopSaveRequest(
        "展示开关测试",
        null,
        "local",
        7200,
        null,
        true,
        10,
        "automatic",
        "legacy_text",
        null,
        List.of(step),
        GROUP,
        show);
  }

  @Test
  void persistsExplicitValuesAndKeepsExistingValueForLegacyUpdates() {
    String id = config.createSop(request(null)).path("id").asText();
    assertThat(config.getSop(id).path("dingtalkShowExecutionDetails").asBoolean()).isFalse();
    config.updateSop(
        id, json.readValue(json.writeValueAsString(request(true)), SopSaveRequest.class));
    config.updateSop(id, request(null));
    entities.flush();
    entities.clear();
    assertThat(config.getSop(id).path("dingtalkShowExecutionDetails").asBoolean()).isTrue();
    config.updateSop(id, request(false));
    config.updateSop(id, request(null));
    entities.flush();
    entities.clear();
    assertThat(config.getSop(id).path("dingtalkShowExecutionDetails").asBoolean()).isFalse();
    assertThat(config.listSops(""))
        .anySatisfy(
            sop -> {
              assertThat(sop.path("id").asText()).isEqualTo(id);
              assertThat(sop.path("dingtalkShowExecutionDetails").asBoolean()).isFalse();
            });
  }

  @Test
  void freezesPreferenceAcrossLaunchSourcesRetryAndReloadWithoutChangingGatewayPayload() {
    String sop = config.createSop(request(false)).path("id").asText();
    String task =
        config
            .createTask(grouped(new TaskDefinitionSaveRequest("通知测试", "目标", sop, null, true)))
            .path("id")
            .asText();
    for (String source : List.of("web", "schedule", "dingtalk")) {
      var prepared = runs.prepareLatest(task, source);
      assertThat(runs.dingtalkShowExecutionDetails(prepared.workflowId())).isFalse();
      assertThat(prepared.payload().has("dingtalkShowExecutionDetails")).isFalse();
    }
    var first = runs.prepareLatest(task);
    config.updateSop(sop, request(true));
    assertThat(runs.dingtalkShowExecutionDetails(runs.prepareLatest(task).workflowId())).isTrue();
    assertThat(
            runs.dingtalkShowExecutionDetails(runs.prepareRetry(first.workflowId()).workflowId()))
        .isFalse();
    entities.flush();
    entities.clear();
    assertThat(runs.dingtalkShowExecutionDetails(first.workflowId())).isFalse();
    // Upgrade compatibility: do not rewrite old snapshots, including when retried.
    ObjectNode snapshot =
        (ObjectNode)
            json.readTree(
                jdbc.queryForObject(
                    "select snapshot_json from codex_sop_task_runs where workflow_id=?",
                    String.class,
                    first.workflowId()));
    ((ObjectNode) snapshot.path("sop")).remove("dingtalkShowExecutionDetails");
    jdbc.update(
        "update codex_sop_task_runs set snapshot_json=? where workflow_id=?",
        json.writeValueAsString(snapshot),
        first.workflowId());
    entities.clear();
    assertThat(runs.dingtalkShowExecutionDetails(first.workflowId())).isTrue();
    assertThat(
            runs.dingtalkShowExecutionDetails(runs.prepareRetry(first.workflowId()).workflowId()))
        .isTrue();
  }
}
