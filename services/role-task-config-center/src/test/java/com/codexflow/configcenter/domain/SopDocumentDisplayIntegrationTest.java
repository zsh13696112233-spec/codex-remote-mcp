package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import com.codexflow.configcenter.GroupedFixtureSupport;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest
@Transactional
class SopDocumentDisplayIntegrationTest extends GroupedFixtureSupport {
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired JdbcTemplate jdbc;
  @Autowired ObjectMapper json;

  @Test
  void documentSwitchIsAbsentFromConfigurationAndRuntimeSnapshot() {
    String role =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    var body =
        json.createObjectNode()
            .put("name", "文档展示测试")
            .put("groupId", GROUP)
            .put("supervisorAgentId", "local")
            .put("supervisorTimeoutSec", 7200)
            .put("enabled", true)
            .put("advanceMode", "semi_automatic");
    var steps = body.putArray("steps");
    for (String name : java.util.List.of("方案", "实施")) {
      steps
          .addObject()
          .put("displayName", name)
          .put("roleId", role)
          .put("instruction", "完成步骤")
          .put("agentId", "local");
    }
    String sop = config.createSop(json.treeToValue(body, SopSaveRequest.class)).path("id").asText();
    assertThat(config.getSop(sop).path("steps").get(0).has("displayDocuments")).isFalse();
    String task =
        config
            .createTask(grouped(new TaskDefinitionSaveRequest("文档任务", "目标", sop, null, true)))
            .path("id")
            .asText();
    var snapshot = runs.prepareLatest(task).payload();
    assertThat(snapshot.path("advanceMode").asText()).isEqualTo("semi_automatic");
    for (var node : snapshot.path("nodes")) assertThat(node.has("displayDocuments")).isFalse();
    assertThat(
            jdbc.queryForObject(
                "select count(*) from information_schema.columns where lower(table_name)='codex_sop_steps' and lower(column_name)='display_documents'",
                Integer.class))
        .isEqualTo(1);
  }
}
