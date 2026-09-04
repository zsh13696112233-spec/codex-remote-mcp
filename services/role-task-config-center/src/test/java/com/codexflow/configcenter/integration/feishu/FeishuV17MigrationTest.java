package com.codexflow.configcenter.integration.feishu;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;

/** 验证升级后把飞书活动运行迁入统一任务槽，并保留历史任务的每日定时默认值。 */
class FeishuV17MigrationTest {

  @Test
  void migratesActiveFeishuWorkflowToTaskDefinitionSlot() throws Exception {
    String url =
        "jdbc:h2:mem:feishu-v17-upgrade-"
            + UUID.randomUUID()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    Flyway.configure()
        .dataSource(url, "sa", "")
        .locations("classpath:db/migration")
        .target("16")
        .load()
        .migrate();

    String sopId = "20000000-0000-4000-8000-000000000001";
    String taskId = "20000000-0000-4000-8000-000000000002";
    String workflowId = "20000000-0000-4000-8000-000000000003";
    try (Connection connection = DriverManager.getConnection(url, "sa", "");
        Statement statement = connection.createStatement()) {
      statement.executeUpdate(
          "INSERT INTO"
              + " codex_sop_sops(id,name,supervisor_agent_id,failure_policy,supervisor_timeout_sec,max_retry_count,advance_mode,handoff_mode,default_step_model,enabled,deleted)"
              + " VALUES ('"
              + sopId
              + "','飞书迁移SOP','local','stop',7200,10,'automatic','legacy_text','gpt-5.6-sol',TRUE,FALSE)");
      statement.executeUpdate(
          "INSERT INTO codex_sop_task_definitions(id,name,objective,sop_id,enabled,deleted) VALUES"
              + " ('"
              + taskId
              + "','飞书迁移任务','验证统一任务槽','"
              + sopId
              + "',TRUE,FALSE)");
      statement.executeUpdate(
          "INSERT INTO"
              + " codex_sop_task_runs(workflow_id,task_definition_id,status,snapshot_json,submitted_json)"
              + " VALUES ('"
              + workflowId
              + "','"
              + taskId
              + "','running','{}','{}')");
      statement.executeUpdate(
          "INSERT INTO codex_sop_feishu_bot_state(app_id,active_workflow_id,version) VALUES"
              + " ('app-v17','"
              + workflowId
              + "',0)");
      statement.executeUpdate(
          "INSERT INTO"
              + " codex_sop_feishu_workflow_bindings(workflow_id,app_id,task_definition_id,trigger_message_id,chat_id,root_message_id,initiator_open_id,status)"
              + " VALUES ('"
              + workflowId
              + "','app-v17','"
              + taskId
              + "','message-v17','chat-v17','message-v17','user-v17','active')");
    }

    Flyway.configure()
        .dataSource(url, "sa", "")
        .locations("classpath:db/migration")
        .load()
        .migrate();

    try (Connection connection = DriverManager.getConnection(url, "sa", "");
        Statement statement = connection.createStatement();
        ResultSet result =
            statement.executeQuery(
                "SELECT active_workflow_id,schedule_mode FROM codex_sop_task_definitions WHERE id='"
                    + taskId
                    + "'")) {
      assertThat(result.next()).isTrue();
      assertThat(result.getString("active_workflow_id")).isEqualTo(workflowId);
      assertThat(result.getString("schedule_mode")).isEqualTo("daily");
    }
  }
}
