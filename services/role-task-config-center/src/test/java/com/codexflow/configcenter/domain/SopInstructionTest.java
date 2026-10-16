package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import com.codexflow.configcenter.GroupedFixtureSupport;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import jakarta.persistence.EntityManager;
import jakarta.validation.Validator;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest
@Transactional
class SopInstructionTest extends GroupedFixtureSupport {
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired JdbcTemplate jdbc;
  @Autowired EntityManager entities;
  @Autowired ObjectMapper json;
  @Autowired Validator validator;

  @Test
  void inheritsOverridesRestoresAndKeepsRetrySnapshot() {
    String role =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    jdbc.update("update codex_sop_roles set duty = ? where id = ?", "角色原始职责", role);
    var body =
        json.createObjectNode()
            .put("name", "提示词继承测试")
            .put("groupId", GROUP)
            .put("supervisorAgentId", "local")
            .put("enabled", true);
    var step =
        body.putArray("steps")
            .addObject()
            .put("displayName", "执行")
            .put("roleId", role)
            .put("agentId", "local");
    var request = json.treeToValue(body, SopSaveRequest.class);
    assertThat(validator.validate(request)).isEmpty();
    String sop = config.createSop(request).path("id").asText();
    entities.flush();
    entities.clear();
    assertThat(config.getSop(sop).path("steps").get(0).path("instruction").asText()).isEmpty();
    String task =
        config
            .createTask(
                grouped(new TaskDefinitionSaveRequest("提示词任务", "任务目标标记", sop, "补充标记", true)))
            .path("id")
            .asText();
    var first = runs.prepareLatest(task);
    String original = first.payload().path("nodes").get(0).path("prompt").asText();
    assertThat(original).containsOnlyOnce("角色原始职责").contains("任务目标标记", "补充标记", "期望输出");
    entities.flush();
    jdbc.update("update codex_sop_roles set duty = ? where id = ?", "角色新版职责", role);
    entities.clear();
    assertThat(prompt(task)).containsOnlyOnce("角色新版职责").doesNotContain("角色原始职责");
    step.put("instruction", "步骤专用要求");
    config.updateSop(sop, json.treeToValue(body, SopSaveRequest.class));
    entities.flush();
    entities.clear();
    assertThat(prompt(task)).containsOnlyOnce("步骤专用要求").doesNotContain("角色新版职责");
    for (String value : new String[] {"  角色新版职责  ", "   "}) {
      step.put("instruction", value);
      config.updateSop(sop, json.treeToValue(body, SopSaveRequest.class));
      entities.flush();
      entities.clear();
      assertThat(config.getSop(sop).path("steps").get(0).path("instruction").asText()).isEmpty();
      assertThat(prompt(task)).containsOnlyOnce("角色新版职责");
    }
    assertThat(
            runs.prepareRetry(first.workflowId())
                .payload()
                .path("nodes")
                .get(0)
                .path("prompt")
                .asText())
        .isEqualTo(original);
  }

  private String prompt(String task) {
    return runs.prepareLatest(task).payload().path("nodes").get(0).path("prompt").asText();
  }
}
