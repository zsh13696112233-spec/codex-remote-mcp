package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;

class BlockedNotificationMigrationTest {
  @Test
  void notificationTimesHaveDefaultsAndPollingDoesNotChangeUpdatedAt() throws Exception {
    String url =
        "jdbc:h2:mem:blocked-migration-"
            + UUID.randomUUID()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    Flyway.configure().dataSource(url, "sa", "").load().migrate();
    try (var connection = DriverManager.getConnection(url, "sa", "");
        var statement = connection.createStatement()) {
      statement.executeUpdate(
          "INSERT INTO codex_blocked_notifications(id,group_json,state,reason)"
              + " VALUES ('defaults','{}','watching','')");
      try (var rows =
          statement.executeQuery(
              "SELECT next_at,updated_at FROM codex_blocked_notifications WHERE id='defaults'")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getTimestamp("next_at")).isNotNull();
        assertThat(rows.getTimestamp("updated_at")).isNotNull();
      }
      Timestamp original = Timestamp.from(Instant.parse("2026-01-01T00:00:00Z"));
      try (var update =
          connection.prepareStatement(
              "UPDATE codex_blocked_notifications SET updated_at=? WHERE id='defaults'")) {
        update.setTimestamp(1, original);
        update.executeUpdate();
      }
      statement.executeUpdate(
          "UPDATE codex_blocked_notifications SET next_at=CURRENT_TIMESTAMP WHERE id='defaults'");
      try (var rows =
          statement.executeQuery(
              "SELECT updated_at FROM codex_blocked_notifications WHERE id='defaults'")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getTimestamp(1)).isEqualTo(original);
      }
    }
  }
}
