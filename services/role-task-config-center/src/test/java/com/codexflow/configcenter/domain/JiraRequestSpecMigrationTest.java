package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;

class JiraRequestSpecMigrationTest {
  @Test
  void upgradePreservesInlineHistoryAndProtectsSharedReferences() {
    var data =
        new DriverManagerDataSource(
            "jdbc:h2:mem:jira-spec-"
                + UUID.randomUUID()
                + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1",
            "sa",
            "");
    Flyway.configure().dataSource(data).target("52").load().migrate();
    var db = new JdbcTemplate(data);
    String snapshot = "{\"name\":\"中文快照😀\"}";
    db.update(
        "INSERT INTO codex_jira_requests(id,task_definition_id,source,spec_json,state) VALUES('old','task','schedule',?,'empty')",
        snapshot);
    var flyway = Flyway.configure().dataSource(data).load();
    assertThat(flyway.migrate().migrationsExecuted).isEqualTo(1);
    assertThat(
            db.queryForObject(
                "SELECT spec_json FROM codex_jira_requests WHERE id='old'", String.class))
        .isEqualTo(snapshot);
    assertThat(
            db.queryForObject(
                "SELECT spec_snapshot_id FROM codex_jira_requests WHERE id='old'", String.class))
        .isNull();
    db.update("INSERT INTO codex_jira_request_specs(id,spec_json) VALUES('shared',?)", snapshot);
    for (String id : java.util.List.of("first", "second")) {
      db.update(
          "INSERT INTO codex_jira_requests(id,task_definition_id,source,spec_json,spec_snapshot_id,state) VALUES(?,'task','schedule','{}','shared','pending')",
          id);
    }
    assertThat(
            db.queryForObject(
                "SELECT spec_json FROM codex_jira_request_specs WHERE id='shared'", String.class))
        .isEqualTo(snapshot);
    assertThatThrownBy(() -> db.update("DELETE FROM codex_jira_request_specs WHERE id='shared'"))
        .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
    assertThatThrownBy(
            () ->
                db.update(
                    "UPDATE codex_jira_requests SET spec_snapshot_id='missing' WHERE id='first'"))
        .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
    flyway.validate();
    assertThat(flyway.migrate().migrationsExecuted).isZero();
  }
}
