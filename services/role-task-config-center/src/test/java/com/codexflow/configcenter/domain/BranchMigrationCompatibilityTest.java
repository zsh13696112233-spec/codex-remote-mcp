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
    assertThat(flyway.migrate().migrationsExecuted).isEqualTo(3);
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
                "SELECT editor_graph_json FROM codex_sop_sops WHERE id='compat-sop'")) {
      assertThat(result.next()).isTrue();
      assertThat(result.getString(1)).isEqualTo("{}");
    }
  }
}
