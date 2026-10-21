package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;

class DingTalkActionContextMigrationTest {
  @Test
  void upgradeKeepsExistingActionsAndAcceptsUnicodeNamesWithoutLosingIdempotency()
      throws Exception {
    String url =
        "jdbc:h2:mem:action-charset-"
            + UUID.randomUUID()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    Flyway.configure().dataSource(url, "sa", "").target("41").load().migrate();
    try (var connection = DriverManager.getConnection(url, "sa", "");
        var sql = connection.createStatement()) {
      sql.executeUpdate(
          "INSERT INTO codex_sop_dingtalk_action_contexts VALUES "
              + "('old','workflow','gate','advance_confirm','old-user','Alice',CURRENT_TIMESTAMP)");
      var flyway = Flyway.configure().dataSource(url, "sa", "").load();
      flyway.migrate();
      flyway.validate();
      assertThat(flyway.migrate().migrationsExecuted).isZero();
      try (var rows =
          sql.executeQuery(
              "SELECT operator_display_name FROM codex_sop_dingtalk_action_contexts WHERE id='old'")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(1)).isEqualTo("Alice");
      }
      try (var insert =
          connection.prepareStatement(
              "INSERT INTO codex_sop_dingtalk_action_contexts VALUES "
                  + "(?,'workflow','gate','advance_confirm','new-user',?,CURRENT_TIMESTAMP)")) {
        insert.setString(1, "new");
        insert.setString(2, "测试人员😀");
        insert.executeUpdate();
        insert.setString(1, "duplicate");
        assertThatThrownBy(insert::executeUpdate).isInstanceOf(SQLException.class);
      }
      try (var rows =
          sql.executeQuery(
              "SELECT operator_display_name FROM codex_sop_dingtalk_action_contexts WHERE id='new'")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(1)).isEqualTo("测试人员😀");
      }
    }
  }
}
