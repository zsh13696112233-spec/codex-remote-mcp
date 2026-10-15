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
  @Autowired TaskRunRepository runRepository;

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
    assertThat(config.getSop(sop).has("confirmationMinutes")).isFalse();
    assertThat(config.getSop(sop).has("blockedMinutes")).isFalse();
    String task =
        config
            .createTask(grouped(new TaskDefinitionSaveRequest("等待任务", "目标", sop, null, true)))
            .path("id")
            .asText();
    var first = runs.prepareLatest(task);
    assertThat(first.payload().path("waitPolicy"))
        .isEqualTo(json.createObjectNode().put("version", 2));
    // 模拟升级前已保存的运行，重试必须保留其原策略。
    var historical = runRepository.findById(first.workflowId()).orElseThrow();
    var original = first.payload().deepCopy();
    original
        .putObject("waitPolicy")
        .put("version", 1)
        .put("confirmationMinutes", 2)
        .put("blockedMinutes", 30);
    historical.submittedJson = original.toString();
    runRepository.saveAndFlush(historical);
    body.put("confirmationMinutes", 5).put("blockedMinutes", 60);
    config.updateSop(sop, json.treeToValue(body, SopSaveRequest.class));
    assertThat(
            runs.prepareRetry(first.workflowId())
                .payload()
                .path("waitPolicy")
                .path("blockedMinutes")
                .asInt())
        .isEqualTo(30);
    assertThat(runs.prepareLatest(task).payload().path("waitPolicy"))
        .isEqualTo(json.createObjectNode().put("version", 2));
    body.put("handoffMode", "cumulative_files");
    config.updateSop(sop, json.treeToValue(body, SopSaveRequest.class));
    assertThat(runs.prepareLatest(task).payload().path("waitPolicy").path("version").asInt())
        .isEqualTo(2);
    body.put("advanceMode", "automatic");
    config.updateSop(sop, json.treeToValue(body, SopSaveRequest.class));
    assertThat(runs.prepareLatest(task).payload().has("waitPolicy")).isFalse();
  }

  @Test
  void ignoresRetiredDurationFields() {
    for (int value : new int[] {0, 721}) {
      var body = body().put("confirmationMinutes", value);
      assertThat(
              config
                  .createSop(json.treeToValue(body, SopSaveRequest.class))
                  .has("confirmationMinutes"))
          .isFalse();
    }
  }
}
