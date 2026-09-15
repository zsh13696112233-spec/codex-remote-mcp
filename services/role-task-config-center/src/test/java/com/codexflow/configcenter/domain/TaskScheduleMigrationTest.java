package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.init.ScriptUtils;

class TaskScheduleMigrationTest {
  @Test
  void upgradeDisablesLegacyRulesWithoutCopyingOrChangingTaskContents() throws Exception {
    try (var connection =
        DriverManager.getConnection("jdbc:h2:mem:schedule-migration;MODE=MySQL")) {
      try (var sql = connection.createStatement()) {
        sql.execute(
            "CREATE TABLE codex_sop_task_definitions (id VARCHAR(36) PRIMARY KEY,name VARCHAR(160),schedule_enabled BOOLEAN,next_interval_at TIMESTAMP,notify_dingtalk BOOLEAN)");
        sql.execute(
            "INSERT INTO codex_sop_task_definitions VALUES ('task','原任务',TRUE,CURRENT_TIMESTAMP,TRUE)");
      }
      ScriptUtils.executeSqlScript(
          connection, new ClassPathResource("db/migration/V27__independent_task_schedules.sql"));
      try (var sql = connection.createStatement();
          var rows = sql.executeQuery("SELECT * FROM codex_sop_task_definitions")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString("name")).isEqualTo("原任务");
        assertThat(rows.getBoolean("notify_dingtalk")).isTrue();
        assertThat(rows.getBoolean("schedule_enabled")).isFalse();
        assertThat(rows.getTimestamp("next_interval_at")).isNull();
      }
      try (var sql = connection.createStatement();
          var rows = sql.executeQuery("SELECT COUNT(*) FROM codex_task_schedules")) {
        rows.next();
        assertThat(rows.getInt(1)).isZero();
      }
    }
  }
}
