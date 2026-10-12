package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import com.codexflow.configcenter.GroupedFixtureSupport;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@SpringBootTest
@Transactional
class SopWaitPolicyTest extends GroupedFixtureSupport {
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired JdbcTemplate jdbc;
  @Autowired ObjectMapper json;

  private ObjectNode body() {
    var body =
        json.createObjectNode()
            .put("name", "等待规则测试")
            .put("groupId", GROUP)
            .put("supervisorAgentId", "local")
            .put("enabled", true)
            .put("advanceMode", "semi_automatic");
    body.putArray("steps")
        .addObject()
        .put("displayName", "执行")
        .put(
            "roleId",
            jdbc.queryForObject(
                "select id from codex_sop_roles order by created_at limit 1", String.class))
        .put("instruction", "执行要求")
        .put("agentId", "local");
    return body;
  }

  @Test
  void freezesDefaultsAndRetainsOriginalRetryPolicy() {
    var body = body();
    String sop = config.createSop(json.treeToValue(body, SopSaveRequest.class)).path("id").asText();
    assertThat(config.getSop(sop).path("confirmationMinutes").asInt()).isEqualTo(2);
    assertThat(config.getSop(sop).path("blockedMinutes").asInt()).isEqualTo(30);
    String task =
        config
            .createTask(grouped(new TaskDefinitionSaveRequest("等待任务", "目标", sop, null, true)))
            .path("id")
            .asText();
    var first = runs.prepareLatest(task);
    assertThat(first.payload().path("waitPolicy").path("confirmationMinutes").asInt()).isEqualTo(2);
    body.put("confirmationMinutes", 5).put("blockedMinutes", 60);
    config.updateSop(sop, json.treeToValue(body, SopSaveRequest.class));
    assertThat(
            runs.prepareRetry(first.workflowId())
                .payload()
                .path("waitPolicy")
                .path("blockedMinutes")
                .asInt())
        .isEqualTo(30);
    assertThat(runs.prepareLatest(task).payload().path("waitPolicy").path("blockedMinutes").asInt())
        .isEqualTo(60);
    body.put("advanceMode", "automatic");
    config.updateSop(sop, json.treeToValue(body, SopSaveRequest.class));
    assertThat(runs.prepareLatest(task).payload().has("waitPolicy")).isFalse();
  }

  @Test
  void rejectsOutOfRangeDurations() {
    for (int value : new int[] {0, 721}) {
      var body = body().put("confirmationMinutes", value);
      assertThatThrownBy(() -> config.createSop(json.treeToValue(body, SopSaveRequest.class)))
          .isInstanceOf(IllegalArgumentException.class);
    }
  }
}
