package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import java.sql.DriverManager;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.init.ScriptUtils;

class SharedGroupsMigrationTest {
  @Test
  void upgradingPreservesOldSnapshotsAndLeavesAllOldRowsUnassigned() throws Exception {
    try (var db = DriverManager.getConnection("jdbc:h2:mem:group-migration;MODE=MySQL")) {
      try (var sql = db.createStatement()) {
        for (String suffix : java.util.List.of("roles", "sops", "task_definitions")) {
          sql.execute("CREATE TABLE codex_sop_" + suffix + " (id VARCHAR(36) PRIMARY KEY)");
          sql.execute("INSERT INTO codex_sop_" + suffix + " VALUES ('old')");
        }
        sql.execute(
            "CREATE TABLE codex_sop_task_runs (workflow_id VARCHAR(128) PRIMARY KEY,snapshot_json TEXT,submitted_json TEXT)");
        sql.execute(
            "INSERT INTO codex_sop_task_runs VALUES ('old-run','{\"name\":\"旧快照\"}','{\"workflowId\":\"old-run\"}')");
      }
      ScriptUtils.executeSqlScript(
          db, new ClassPathResource("db/migration/V28__shared_groups.sql"));
      try (var sql = db.createStatement()) {
        for (String suffix : java.util.List.of("roles", "sops", "task_definitions", "task_runs")) {
          try (var row = sql.executeQuery("SELECT group_id FROM codex_sop_" + suffix)) {
            assertThat(row.next()).isTrue();
            assertThat(row.getString(1)).isNull();
          }
        }
        try (var row =
            sql.executeQuery(
                "SELECT snapshot_json,submitted_json,group_name FROM codex_sop_task_runs")) {
          assertThat(row.next()).isTrue();
          assertThat(row.getString(1)).isEqualTo("{\"name\":\"旧快照\"}");
          assertThat(row.getString(2)).isEqualTo("{\"workflowId\":\"old-run\"}");
          assertThat(row.getString(3)).isNull();
        }
      }
    }
  }
}
