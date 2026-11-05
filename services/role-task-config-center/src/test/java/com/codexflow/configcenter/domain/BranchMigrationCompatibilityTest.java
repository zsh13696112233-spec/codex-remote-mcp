package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;

class BranchMigrationCompatibilityTest {
  @Test
  void validatesAlreadyAppliedCanvasMigrationsOnRestart() throws Exception {
    String url =
        "jdbc:h2:mem:branch-"
            + UUID.randomUUID()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    Flyway.configure().dataSource(url, "sa", "").target("29").load().migrate();
    var flyway = Flyway.configure().dataSource(url, "sa", "").load();
    assertThat(flyway.migrate().migrationsExecuted).isEqualTo(19);
    try (var connection = DriverManager.getConnection(url, "sa", "");
        var statement = connection.createStatement()) {
      statement.executeUpdate(
          "INSERT INTO codex_sop_sops"
              + " (id,name,supervisor_agent_id,default_step_model,editor_graph_json)"
              + " VALUES ('compat-sop','兼容测试','local','test','{}')");
    }
    var restarted = Flyway.configure().dataSource(url, "sa", "").load();
    restarted.validate();
    assertThat(restarted.migrate().migrationsExecuted).isZero();
    try (var connection = DriverManager.getConnection(url, "sa", "");
        var statement = connection.createStatement();
        var result =
            statement.executeQuery(
                "SELECT editor_graph_json,dispatch_mode FROM codex_sop_sops WHERE id='compat-sop'")) {
      assertThat(result.next()).isTrue();
      assertThat(result.getString(1)).isEqualTo("{}");
      assertThat(result.getString(2)).isEqualTo("fixed");
    }
  }

  @Test
  void plannerMigrationUpdatesEveryStandardSopIncludingDisabledOnes() throws Exception {
    String url =
        "jdbc:h2:mem:planner-"
            + UUID.randomUUID()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    Flyway.configure().dataSource(url, "sa", "").target("45").load().migrate();
    String base = "project = TEST AND status = Open AND assignee = currentUser()";
    String planner = base + " AND \"处理角色\" = \"Planner\"";
    try (var c = DriverManager.getConnection(url, "sa", "");
        var q =
            c.prepareStatement(
                "INSERT INTO codex_sop_sops(id,name,supervisor_agent_id,default_step_model,jira_jql,enabled) VALUES (?,?, 'local','test',?,?)")) {
      for (int i = 0; i < 4; i++) {
        q.setString(1, "planner-" + i);
        q.setString(2, "SOP " + i);
        q.setString(3, i == 2 ? planner : i == 3 ? null : base);
        q.setBoolean(4, i != 1);
        q.executeUpdate();
      }
    }
    var flyway = Flyway.configure().dataSource(url, "sa", "").load();
    assertThat(flyway.migrate().migrationsExecuted).isEqualTo(3);
    try (var c = DriverManager.getConnection(url, "sa", "");
        var q = c.createStatement();
        var r =
            q.executeQuery(
                "SELECT id,jira_jql,jira_connection_id FROM codex_sop_sops ORDER BY id")) {
      int count = 0;
      while (r.next()) {
        count++;
        assertThat(r.getString(3)).isNull();
        assertThat(r.getString(2)).isEqualTo(r.getString(1).equals("planner-3") ? null : planner);
      }
      assertThat(count).isEqualTo(4);
    }
    assertThat(flyway.migrate().migrationsExecuted).isZero();
  }

  @Test
  void jiraPoolTimestampDefaultsAreExplicitAndApplicationValuesRemainAuthoritative()
      throws Exception {
    String url =
        "jdbc:h2:mem:jira-defaults-"
            + UUID.randomUUID()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    Flyway.configure().dataSource(url, "sa", "").load().migrate();
    try (var c = DriverManager.getConnection(url, "sa", "");
        var q = c.createStatement()) {
      q.executeUpdate(
          "INSERT INTO codex_jira_requests(id,task_definition_id,source,spec_json,state) VALUES('defaults','task','web','{}','pending')");
      try (var r =
          q.executeQuery(
              "SELECT created_at,updated_at FROM codex_jira_requests WHERE id='defaults'")) {
        assertThat(r.next()).isTrue();
        assertThat(r.getTimestamp(1)).isNotNull();
        assertThat(r.getTimestamp(2)).isNotNull();
      }
      q.executeUpdate(
          "INSERT INTO codex_jira_requests(id,task_definition_id,source,spec_json,state,created_at,updated_at) VALUES('explicit','task','web','{}','pending','2026-01-02 03:04:05.123456','2026-01-02 04:05:06.234567')");
      q.executeUpdate("UPDATE codex_jira_requests SET state='waiting' WHERE id='explicit'");
      try (var r =
          q.executeQuery(
              "SELECT created_at,updated_at FROM codex_jira_requests WHERE id='explicit'")) {
        assertThat(r.next()).isTrue();
        assertThat(r.getTimestamp(1))
            .isEqualTo(java.sql.Timestamp.valueOf("2026-01-02 03:04:05.123456"));
        assertThat(r.getTimestamp(2))
            .isEqualTo(java.sql.Timestamp.valueOf("2026-01-02 04:05:06.234567"));
      }
    }
  }
}
