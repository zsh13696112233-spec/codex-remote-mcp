package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.init.ScriptUtils;

class SopDingTalkDetailsMigrationTest {
  @Test
  void existingSopsKeepDetailsWhileExplicitNewValuesArePreserved() throws Exception {
    try (var connection =
            DriverManager.getConnection("jdbc:h2:mem:sop-details-migration;MODE=MySQL");
        var sql = connection.createStatement()) {
      sql.execute("CREATE TABLE codex_sop_sops (id VARCHAR(36) PRIMARY KEY)");
      sql.execute("INSERT INTO codex_sop_sops VALUES ('old')");
      ScriptUtils.executeSqlScript(
          connection,
          new ClassPathResource("db/migration/V33__add_sop_dingtalk_execution_details.sql"));
      sql.execute("INSERT INTO codex_sop_sops VALUES ('new',FALSE)");
      try (var rows =
          sql.executeQuery(
              "SELECT id,dingtalk_show_execution_details FROM codex_sop_sops ORDER BY id")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(1)).isEqualTo("new");
        assertThat(rows.getBoolean(2)).isFalse();
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(1)).isEqualTo("old");
        assertThat(rows.getBoolean(2)).isTrue();
      }
    }
  }
}
