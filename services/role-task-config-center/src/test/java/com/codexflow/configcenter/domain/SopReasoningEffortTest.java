package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.GroupedFixtureSupport;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import jakarta.persistence.EntityManager;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest
@Transactional
class SopReasoningEffortTest extends GroupedFixtureSupport {
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired JdbcTemplate jdbc;
  @Autowired EntityManager entities;
  @Autowired ObjectMapper json;

  private SopSaveRequest request(String defaultEffort, String override) {
    String role =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    var body =
        json.createObjectNode()
            .put("name", "思考强度测试")
            .put("groupId", GROUP)
            .put("supervisorAgentId", "local")
            .put("enabled", true)
            .put("defaultStepModel", "gpt-6.1-sol")
            .put("defaultStepReasoningEffort", defaultEffort);
    body.putArray("steps")
        .addObject()
        .put("displayName", "执行")
        .put("roleId", role)
        .put("instruction", "执行要求")
        .put("agentId", "local")
        .put("reasoningEffortOverride", override);
    return json.treeToValue(body, SopSaveRequest.class);
  }

  private void catalog() {
    var response = json.createObjectNode();
    response
        .putArray("models")
        .addObject()
        .put("model", "gpt-6.1-sol")
        .putArray("supportedReasoningEfforts")
        .add("high")
        .add("medium");
    when(groupGateway.get("/agents/local/models")).thenReturn(response);
  }

  @Test
  void defaultsToSolAndAcceptsOnlyCurrentModels() {
    tools.jackson.databind.node.ObjectNode body = json.valueToTree(request(null, null));
    body.remove("defaultStepModel");
    String id = config.createSop(json.treeToValue(body, SopSaveRequest.class)).path("id").asText();
    assertThat(config.getSop(id).path("defaultStepModel").asText()).isEqualTo("gpt-6.1-sol");
    body.put("defaultStepModel", "gpt-6-astra");
    ((tools.jackson.databind.node.ObjectNode) body.path("steps").get(0))
        .put("modelOverride", "gpt-6.1-sol");
    config.updateSop(id, json.treeToValue(body, SopSaveRequest.class));
    assertThat(config.getSop(id).path("defaultStepModel").asText()).isEqualTo("gpt-6-astra");
    for (String model :
        java.util.List.of("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-sol")) {
      body.put("defaultStepModel", model);
      assertThatThrownBy(() -> config.updateSop(id, json.treeToValue(body, SopSaveRequest.class)))
          .hasMessageContaining("不支持的模型");
      body.put("defaultStepModel", "gpt-6.1-sol");
      ((tools.jackson.databind.node.ObjectNode) body.path("steps").get(0))
          .put("modelOverride", model);
      assertThatThrownBy(() -> config.updateSop(id, json.treeToValue(body, SopSaveRequest.class)))
          .hasMessageContaining("不支持的模型");
    }
  }

  @Test
  void persistsInheritanceOverrideAndImmutableRetry() {
    catalog();
    String sop = config.createSop(request("high", null)).path("id").asText();
    entities.flush();
    entities.clear();
    assertThat(config.getSop(sop).path("defaultStepReasoningEffort").asText()).isEqualTo("high");
    String task =
        config
            .createTask(grouped(new TaskDefinitionSaveRequest("思考任务", "目标", sop, null, true)))
            .path("id")
            .asText();
    var first = runs.prepareLatest(task);
    assertThat(first.payload().path("nodes").get(0).path("reasoningEffort").asText())
        .isEqualTo("high");
    config.updateSop(sop, request("high", "medium"));
    entities.flush();
    entities.clear();
    assertThat(config.getSop(sop).path("steps").get(0).path("reasoningEffortOverride").asText())
        .isEqualTo("medium");
    assertThat(
            runs.prepareRetry(first.workflowId())
                .payload()
                .path("nodes")
                .get(0)
                .path("reasoningEffort")
                .asText())
        .isEqualTo("high");
    assertThat(
            runs.prepareLatest(task)
                .payload()
                .path("nodes")
                .get(0)
                .path("reasoningEffort")
                .asText())
        .isEqualTo("medium");
    config.updateSop(sop, request(null, null));
    assertThat(runs.prepareLatest(task).payload().path("nodes").get(0).has("reasoningEffort"))
        .isFalse();
  }

  @Test
  void oldConfigurationDoesNotQueryCapabilitiesAndOldRetryOmitsEffort() {
    String sop = config.createSop(request(null, null)).path("id").asText();
    verify(groupGateway, never()).get("/agents/local/models");
    String task =
        config
            .createTask(grouped(new TaskDefinitionSaveRequest("默认任务", "目标", sop, null, true)))
            .path("id")
            .asText();
    var first = runs.prepareLatest(task);
    catalog();
    config.updateSop(sop, request("high", null));
    assertThat(
            runs.prepareRetry(first.workflowId())
                .payload()
                .path("nodes")
                .get(0)
                .has("reasoningEffort"))
        .isFalse();
  }

  @Test
  void hidesInternalConnectionDetailsWhenCapabilitiesFail() {
    when(groupGateway.get("/agents/local/models"))
        .thenThrow(
            new com.codexflow.configcenter.client.GatewayFailure(
                502, "http://private-host:8080 secret"));
    assertThatThrownBy(() -> config.createSop(request("high", null)))
        .hasMessageContaining("无法读取执行机支持的思考强度")
        .hasMessageNotContaining("private-host")
        .hasMessageNotContaining("secret");
  }

  @Test
  void rejectsUnknownOrUnsupportedEffort() {
    catalog();
    assertThatThrownBy(() -> config.createSop(request("bogus", null)))
        .hasMessageContaining("思考强度无效");
    assertThatThrownBy(() -> config.createSop(request(null, "ultra"))).hasMessageContaining("不支持");
  }
}
