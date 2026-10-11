package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
class SopTimeoutIntegrationTest extends GroupedFixtureSupport {
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired JdbcTemplate jdbc;
  @Autowired EntityManager entities;
  @Autowired ObjectMapper json;

  private SopSaveRequest request(Integer timeout, int stepTimeout) {
    String role =
        jdbc.queryForObject(
            "select id from codex_sop_roles order by created_at limit 1", String.class);
    var body =
        json.createObjectNode()
            .put("name", "主监督时长测试")
            .put("groupId", GROUP)
            .put("supervisorAgentId", "local")
            .put("enabled", true);
    if (timeout != null) body.put("supervisorTimeoutSec", timeout);
    body.putArray("steps")
        .addObject()
        .put("displayName", "执行")
        .put("roleId", role)
        .put("instruction", "完成步骤")
        .put("agentId", "local")
        .put("timeoutSec", stepTimeout);
    return json.treeToValue(body, SopSaveRequest.class);
  }

  @Test
  void persistsTwelveHoursAndFreezesItForRetry() {
    String sop = config.createSop(request(43200, 7200)).path("id").asText();
    entities.flush();
    entities.clear();
    assertThat(config.getSop(sop).path("supervisorTimeoutSec").asInt()).isEqualTo(43200);
    String task =
        config
            .createTask(grouped(new TaskDefinitionSaveRequest("长任务", "目标", sop, null, true)))
            .path("id")
            .asText();
    var first = runs.prepareLatest(task);
    assertThat(first.payload().path("supervisorTimeoutSec").asInt()).isEqualTo(43200);
    config.updateSop(sop, request(7200, 7200));
    assertThat(runs.prepareRetry(first.workflowId()).payload().path("supervisorTimeoutSec").asInt())
        .isEqualTo(43200);
    assertThat(runs.prepareLatest(task).payload().path("supervisorTimeoutSec").asInt())
        .isEqualTo(7200);
  }

  @Test
  void preservesDefaultAndAcceptsLowerBoundary() {
    String sop = config.createSop(request(null, 10)).path("id").asText();
    assertThat(config.getSop(sop).path("supervisorTimeoutSec").asInt()).isEqualTo(7200);
    config.updateSop(sop, request(10, 10));
    assertThat(config.getSop(sop).path("supervisorTimeoutSec").asInt()).isEqualTo(10);
  }

  @Test
  void rejectsSupervisorAboveTwelveHours() {
    assertThatThrownBy(() -> config.createSop(request(43201, 10)))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("supervisorTimeoutSec");
  }

  @Test
  void rejectsSupervisorBelowTenSeconds() {
    assertThatThrownBy(() -> config.createSop(request(9, 10)))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("supervisorTimeoutSec");
  }

  @Test
  void keepsStepLimitAtTwoHours() {
    assertThatThrownBy(() -> config.createSop(request(43200, 7201)))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("timeoutSec");
  }
}
