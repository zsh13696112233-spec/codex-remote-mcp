package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest
@Transactional
class BlockedNotificationSnapshotTest extends com.codexflow.configcenter.GroupedFixtureSupport {
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired BlockedNotificationStore notices;
  @Autowired JdbcTemplate db;

  @Test
  void allSourcesFreezeGroupAndRetryKeepsItWithoutOrdinaryNotifications() {
    String group = UUID.randomUUID().toString();
    db.update(
        "INSERT INTO codex_sop_dingtalk_targets(id,client_id,target_type,external_id,display_name,source,enabled) VALUES (?,'snapshot-client','GROUP','snapshot-group','通知群','OBSERVED',true)",
        group);
    String role =
        db.queryForObject(
            "SELECT id FROM codex_sop_roles ORDER BY created_at LIMIT 1", String.class);
    var step =
        new SopStepRequest(
            "检查", role, "执行", null, null, "local", null, null, null, null, null, Set.of(),
            Set.of());
    String sop =
        config
            .createSop(
                grouped(
                    new SopSaveRequest(
                        "阻断测试-" + UUID.randomUUID(),
                        null,
                        "local",
                        null,
                        null,
                        true,
                        3,
                        "automatic",
                        null,
                        null,
                        List.of(step))))
            .path("id")
            .asText();
    var request =
        new TaskDefinitionSaveRequest(
            "阻断测试任务", "检查任务", sop, null, true, null, null, null, null, null, false, GROUP, group);
    String task = config.createTask(request).path("id").asText();
    String first = null;
    for (String source : List.of("web", "schedule", "dingtalk")) {
      var prepared = runs.prepareLatest(task, source);
      assertThat(prepared.payload().path("resultProtocolVersion").asInt()).isEqualTo(2);
      assertThat(notices.read(prepared.workflowId()).group().path("externalId").asText())
          .isEqualTo("snapshot-group");
      assertThat(notices.read(prepared.workflowId()).state()).isEqualTo("watching");
      if (first == null) first = prepared.workflowId();
    }
    // 旧客户端省略字段不清除；显式空字符串清除，仅影响新运行。
    config.updateTask(
        task, grouped(new TaskDefinitionSaveRequest("阻断测试任务", "检查任务", sop, null, true)));
    assertThat(
            config.listTasks("").stream()
                .filter(t -> task.equals(t.path("id").asText()))
                .findFirst()
                .orElseThrow()
                .path("blockedNotificationGroupId")
                .asText())
        .isEqualTo(group);
    config.updateTask(
        task,
        new TaskDefinitionSaveRequest(
            "阻断测试任务", "检查任务", sop, null, true, null, null, null, null, null, false, GROUP, ""));
    var retry = runs.prepareRetry(first);
    assertThat(notices.read(retry.workflowId()).group().path("externalId").asText())
        .isEqualTo("snapshot-group");
    assertThat(notices.read(runs.prepareLatest(task).workflowId())).isNull();
  }
}
