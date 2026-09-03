package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;

/** 验证 V1 固定任务和 SOP 目标会完整迁移到任务定义绑定。 */
class DingTalkV14MigrationTest {

  @Test
  void migratesLegacyTargetAndActiveWorkflowToTaskDefinition() throws Exception {
    String url =
        "jdbc:h2:mem:dingtalk-upgrade-"
            + UUID.randomUUID()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    Flyway.configure()
        .dataSource(url, "sa", "")
        .locations("classpath:db/migration")
        .target("13")
        .load()
        .migrate();

    String sopId = "10000000-0000-4000-8000-000000000001";
    String taskId = "10000000-0000-4000-8000-000000000002";
    String targetId = "10000000-0000-4000-8000-000000000003";
    String workflowId = "10000000-0000-4000-8000-000000000004";
    try (Connection connection = DriverManager.getConnection(url, "sa", "");
        Statement statement = connection.createStatement()) {
      statement.executeUpdate(
          "INSERT INTO codex_sop_dingtalk_targets(id,client_id,target_type,external_id,display_name,source,available,enabled,deleted) VALUES ('"
              + targetId
              + "','client-v1','GROUP','chat-v1','历史群','OBSERVED',TRUE,TRUE,FALSE)");
      statement.executeUpdate(
          "INSERT INTO codex_sop_sops(id,name,supervisor_agent_id,failure_policy,supervisor_timeout_sec,max_retry_count,advance_mode,handoff_mode,default_step_model,enabled,deleted) VALUES ('"
              + sopId
              + "','历史SOP','local','stop',7200,10,'automatic','legacy_text','gpt-5.6-sol',TRUE,FALSE)");
      statement.executeUpdate(
          "UPDATE codex_sop_sops SET dingtalk_target_id='"
              + targetId
              + "' WHERE id='"
              + sopId
              + "'");
      statement.executeUpdate(
          "INSERT INTO codex_sop_task_definitions(id,name,objective,sop_id,enabled,deleted) VALUES ('"
              + taskId
              + "','历史任务','验证升级','"
              + sopId
              + "',TRUE,FALSE)");
      statement.executeUpdate(
          "INSERT INTO codex_sop_dingtalk_bot_settings(id,enabled,client_id,client_secret,task_definition_id,card_template_id,event_poll_interval_ms,version) VALUES (1,TRUE,'client-v1','secret','"
              + taskId
              + "','',1000,0)");
      statement.executeUpdate(
          "INSERT INTO codex_sop_dingtalk_bot_state(client_id,active_workflow_id,version) VALUES ('client-v1','"
              + workflowId
              + "',0)");
    }

    Flyway.configure()
        .dataSource(url, "sa", "")
        .locations("classpath:db/migration")
        .load()
        .migrate();

    try (Connection connection = DriverManager.getConnection(url, "sa", "");
        Statement statement = connection.createStatement()) {
      try (ResultSet result =
          statement.executeQuery(
              "SELECT dingtalk_target_id,dingtalk_active_workflow_id FROM codex_sop_task_definitions WHERE id='"
                  + taskId
                  + "'")) {
        assertThat(result.next()).isTrue();
        assertThat(result.getString("dingtalk_target_id")).isEqualTo(targetId);
        assertThat(result.getString("dingtalk_active_workflow_id")).isEqualTo(workflowId);
      }
      assertThat(singleValue(statement, "SELECT dingtalk_target_id FROM codex_sop_sops")).isNull();
      assertThat(
              singleValue(
                  statement, "SELECT task_definition_id FROM codex_sop_dingtalk_bot_settings"))
          .isNull();
    }
  }

  private static String singleValue(Statement statement, String sql) throws Exception {
    try (ResultSet result = statement.executeQuery(sql)) {
      result.next();
      return result.getString(1);
    }
  }
}
