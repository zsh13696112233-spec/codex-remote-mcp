package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import com.codexflow.configcenter.GroupedFixtureSupport;
import com.codexflow.configcenter.dto.*;
import jakarta.persistence.EntityManager;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest
@Transactional
class SopGraphIntegrationTest extends GroupedFixtureSupport {
  @Autowired ConfigService service;
  @Autowired WorkflowRunStore runs;
  @Autowired JdbcTemplate jdbc;
  @Autowired EntityManager em;
  @Autowired ObjectMapper mapper;

  @Test
  void graphSurvivesSaveAndReloadAndSubmissionUsesEdgeOrder() {
    String role =
        jdbc.queryForObject("select id from codex_sop_roles order by name limit 1", String.class);
    var a = step("a", role);
    var b = step("b", role);
    var graph =
        new SopEditorGraph(
            1,
            List.of(
                new SopEditorGraph.Node("start", "start", 0d, 0d),
                new SopEditorGraph.Node("a", "step", 800d, 20d),
                new SopEditorGraph.Node("b", "step", -100d, 70d),
                new SopEditorGraph.Node("end", "end", 900d, 0d)),
            List.of(
                new SopEditorGraph.Edge("start", "b"),
                new SopEditorGraph.Edge("b", "a"),
                new SopEditorGraph.Edge("a", "end")));
    var body =
        new SopSaveRequest(
            "画布流程",
            "说明",
            "local",
            7200,
            "gpt-5.6-sol",
            true,
            10,
            "semi_automatic",
            "legacy_text",
            null,
            List.of(a, b),
            GROUP,
            graph);
    var saved = service.createSop(body);
    String id = saved.path("id").asText();
    em.flush();
    em.clear();
    var loaded = service.getSop(id);
    assertThat(loaded.path("editorGraph")).isEqualTo(mapper.valueToTree(graph));
    assertThat(loaded.path("steps").get(0).path("nodeKey").asText()).isEqualTo("b");
    String oldStepId = loaded.path("steps").get(0).path("id").asText();
    var updated = service.updateSop(id, body);
    em.flush();
    assertThat(updated.path("steps").get(0).path("nodeKey").asText()).isEqualTo("b");
    assertThat(updated.path("steps").get(0).path("id").asText()).isNotEqualTo(oldStepId);
    var task =
        service.createTask(grouped(new TaskDefinitionSaveRequest("画布任务", "运行", id, null, true)));
    var run = runs.prepareLatest(task.path("id").asText());
    var payload = run.payload();
    assertThat(payload.has("editorGraph")).isFalse();
    assertThat(payload.path("nodes").get(0).path("displayName").asText()).isEqualTo("b");
    assertThat(payload.path("nodes").get(1).path("dependsOn").get(0).asText())
        .isEqualTo(payload.path("nodes").get(0).path("id").asText());
    assertThat(payload.path("advanceMode").asText()).isEqualTo("semi_automatic");
    em.flush();
    String snapshot =
        jdbc.queryForObject(
            "select snapshot_json from codex_sop_task_runs where workflow_id=?",
            String.class,
            run.workflowId());
    service.updateSop(id, body);
    em.flush();
    em.clear();
    assertThat(runs.getPrepared(run.workflowId()).payload()).isEqualTo(payload);
    assertThat(
            jdbc.queryForObject(
                "select snapshot_json from codex_sop_task_runs where workflow_id=?",
                String.class,
                run.workflowId()))
        .isEqualTo(snapshot);
  }

  @Test
  void legacyStepsReceiveStableKeys() {
    String role =
        jdbc.queryForObject("select id from codex_sop_roles order by name limit 1", String.class);
    var saved =
        service.createSop(
            grouped(
                new SopSaveRequest(
                    "旧流程", null, 7200, "gpt-5.6-sol", true, List.of(step(null, role)))));
    assertThat(saved.has("editorGraph")).isFalse();
    assertThat(saved.path("steps").get(0).path("nodeKey").asText()).isNotBlank();
    em.flush();
    em.clear();
    assertThat(service.getSop(saved.path("id").asText()).path("steps").get(0).path("nodeKey"))
        .isEqualTo(saved.path("steps").get(0).path("nodeKey"));
  }

  @Test
  void graphMismatchIsRejectedBeforePersistence() {
    String role =
        jdbc.queryForObject("select id from codex_sop_roles order by name limit 1", String.class);
    long count = jdbc.queryForObject("select count(*) from codex_sop_sops", Long.class);
    var graph =
        new SopEditorGraph(
            1,
            List.of(
                new SopEditorGraph.Node("start", "start", 0d, 0d),
                new SopEditorGraph.Node("wrong-key", "step", 320d, 0d),
                new SopEditorGraph.Node("end", "end", 640d, 0d)),
            List.of(
                new SopEditorGraph.Edge("start", "wrong-key"),
                new SopEditorGraph.Edge("wrong-key", "end")));
    var request =
        new SopSaveRequest(
            "无效画布",
            null,
            "local",
            7200,
            "gpt-5.6-sol",
            true,
            10,
            "automatic",
            "legacy_text",
            null,
            List.of(step("a", role)),
            GROUP,
            graph);
    assertThatThrownBy(() -> service.createSop(request))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("一一对应");
    assertThat(jdbc.queryForObject("select count(*) from codex_sop_sops", Long.class))
        .isEqualTo(count);
  }

  private SopStepRequest step(String key, String role) {
    return new SopStepRequest(
        key == null ? "旧步骤" : key,
        role,
        "执行",
        "输出",
        "local",
        "local",
        null,
        false,
        "read_only",
        null,
        60,
        Set.of("tag"),
        Set.of(),
        key);
  }
}
