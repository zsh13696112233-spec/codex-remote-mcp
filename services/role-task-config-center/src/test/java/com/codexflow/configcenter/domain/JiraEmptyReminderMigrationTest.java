package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import java.util.UUID;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;

class JiraEmptyReminderMigrationTest {
  @Test
  void upgradePreservesExistingSettingsAndStoresChinese() {
    var data =
        new DriverManagerDataSource(
            "jdbc:h2:mem:empty-upgrade-"
                + UUID.randomUUID()
                + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1",
            "sa",
            "");
    Flyway.configure().dataSource(data).target("51").load().migrate();
    var db = new JdbcTemplate(data);
    db.update(
        "UPDATE codex_blocked_notification_settings SET template_id='existing.schema' WHERE id=1");
    var flyway = Flyway.configure().dataSource(data).load();
    flyway.migrate();
    assertThat(
            db.queryForObject(
                "SELECT template_id FROM codex_blocked_notification_settings WHERE id=1",
                String.class))
        .isEqualTo("existing.schema");
    db.update(
        "INSERT INTO codex_jira_empty_reminders VALUES(?,1,TRUE,'{}',?)",
        UUID.randomUUID().toString(),
        "{\"lastResult\":\"已投递📣\"}");
    assertThat(db.queryForObject("SELECT state_json FROM codex_jira_empty_reminders", String.class))
        .contains("已投递📣");
    assertThat(flyway.migrate().migrationsExecuted).isZero();
    flyway.validate();
  }
}
