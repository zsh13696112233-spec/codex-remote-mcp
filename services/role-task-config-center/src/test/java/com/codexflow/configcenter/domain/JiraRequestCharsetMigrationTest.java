package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;

class JiraRequestCharsetMigrationTest {
  @Test
  void upgradePreservesRequestsAndAcceptsUnicodeSnapshotsAndMessages() throws Exception {
    String url =
        "jdbc:h2:mem:jira-charset-"
            + UUID.randomUUID()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    Flyway.configure().dataSource(url, "sa", "").target("50").load().migrate();
    try (var connection = DriverManager.getConnection(url, "sa", "");
        var sql = connection.createStatement()) {
      sql.executeUpdate(
          "INSERT INTO codex_jira_requests(id,task_definition_id,source,spec_json,state) "
              + "VALUES ('old','task','web','{}','waiting')");
      sql.executeUpdate(
          "INSERT INTO codex_jira_dingtalk_requests(client_id,message_id,request_id,message_json) "
              + "VALUES ('client','old-message','old','{}')");
      var flyway = Flyway.configure().dataSource(url, "sa", "").load();
      assertThat(flyway.migrate().migrationsExecuted).isEqualTo(1);
      flyway.validate();
      assertThat(flyway.migrate().migrationsExecuted).isZero();
      try (var rows = sql.executeQuery("SELECT state FROM codex_jira_requests WHERE id='old'")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(1)).isEqualTo("waiting");
      }
      String snapshot = "{\"name\":\"验收😀\",\"prompt\":\"转交报告人，添加 ai已验证\"}";
      try (var insert =
          connection.prepareStatement(
              "INSERT INTO codex_jira_requests(id,task_definition_id,source,spec_json,schedule_json,state,reason) "
                  + "VALUES ('new','task','web',?,?,'cancelled',?)")) {
        insert.setString(1, snapshot);
        insert.setString(2, "{\"name\":\"定时验收😀\"}");
        insert.setString(3, "仅验证中文和表情😀");
        insert.executeUpdate();
        assertThatThrownBy(insert::executeUpdate).isInstanceOf(SQLException.class);
      }
      try (var insert =
          connection.prepareStatement(
              "INSERT INTO codex_jira_dingtalk_requests(client_id,message_id,request_id,message_json) "
                  + "VALUES ('client','new-message','new',?)")) {
        insert.setString(1, snapshot);
        insert.executeUpdate();
        assertThatThrownBy(insert::executeUpdate).isInstanceOf(SQLException.class);
      }
      try (var rows =
          sql.executeQuery(
              "SELECT spec_json,schedule_json,reason FROM codex_jira_requests WHERE id='new'")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(1)).isEqualTo(snapshot);
        assertThat(rows.getString(2)).isEqualTo("{\"name\":\"定时验收😀\"}");
        assertThat(rows.getString(3)).isEqualTo("仅验证中文和表情😀");
      }
      try (var rows =
          sql.executeQuery(
              "SELECT message_id,message_json FROM codex_jira_dingtalk_requests ORDER BY message_id")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(1)).isEqualTo("new-message");
        assertThat(rows.getString(2)).isEqualTo(snapshot);
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(2)).isEqualTo("{}");
      }
    }
  }
}
