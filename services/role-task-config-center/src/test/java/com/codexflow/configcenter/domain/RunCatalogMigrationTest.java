package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import db.migration.V26__backfill_run_catalog;
import java.sql.DriverManager;
import org.junit.jupiter.api.Test;

class RunCatalogMigrationTest {
  @Test
  void backfillUsesOnlySnapshotNameAndExplicitSourcesAcrossBatches() throws Exception {
    try (var connection = DriverManager.getConnection("jdbc:h2:mem:catalog-migration;MODE=MySQL")) {
      try (var sql = connection.createStatement()) {
        sql.execute(
            "CREATE TABLE codex_sop_task_runs (workflow_id VARCHAR(128) PRIMARY KEY, snapshot_json TEXT, run_name VARCHAR(255), trigger_source VARCHAR(16))");
        sql.execute(
            "CREATE TABLE codex_sop_dingtalk_workflow_bindings (workflow_id VARCHAR(128) PRIMARY KEY, trigger_source VARCHAR(16))");
      }
      try (var insert =
          connection.prepareStatement(
              "INSERT INTO codex_sop_task_runs VALUES (?, ?, '', 'unknown')")) {
        for (int i = 0; i < 205; i++) {
          insert.setString(1, String.format("%03d", i));
          insert.setString(2, "{\"name\":\"历史名称\",\"scheduleEnabled\":true}");
          insert.addBatch();
        }
        insert.executeBatch();
      }
      try (var sql = connection.createStatement()) {
        sql.execute(
            "INSERT INTO codex_sop_dingtalk_workflow_bindings VALUES ('000', 'schedule'), ('001', 'dingtalk'), ('002', 'web'), ('003', 'chat')");
      }
      V26__backfill_run_catalog.backfill(connection);
      try (var sql = connection.createStatement();
          var rows =
              sql.executeQuery(
                  "SELECT workflow_id, run_name, trigger_source FROM codex_sop_task_runs ORDER BY workflow_id")) {
        int count = 0;
        while (rows.next()) {
          assertThat(rows.getString(2)).isEqualTo("历史名称");
          assertThat(rows.getString(3))
              .isEqualTo(
                  switch (count) {
                    case 0 -> "schedule";
                    case 1 -> "dingtalk";
                    case 2 -> "web";
                    default -> "unknown";
                  });
          count++;
        }
        assertThat(count).isEqualTo(205);
      }
    }
  }
}
