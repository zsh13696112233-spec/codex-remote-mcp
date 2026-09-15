package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import db.migration.V29__release_deleted_role_names;
import java.sql.DriverManager;
import java.sql.SQLException;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class RoleNameMigrationTest {
  @ParameterizedTest
  @ValueSource(booleans = {false, true})
  void preservesOldDeletedNamesAndEnforcesOnlyActiveUniqueness(boolean lowerCase) throws Exception {
    try (var db =
            DriverManager.getConnection(
                "jdbc:h2:mem:role-name-migration;MODE=MySQL;DATABASE_TO_LOWER=" + lowerCase);
        var sql = db.createStatement()) {
      sql.execute(
          "CREATE TABLE codex_sop_roles (id VARCHAR(36) PRIMARY KEY, name VARCHAR(100) NOT NULL UNIQUE, deleted BOOLEAN NOT NULL)");
      sql.execute("INSERT INTO codex_sop_roles VALUES ('old','原角色',TRUE),('active','有效角色',FALSE)");
      V29__release_deleted_role_names.migrate(db);
      sql.execute("INSERT INTO codex_sop_roles(id,name,deleted) VALUES ('new','原角色',FALSE)");
      assertThatThrownBy(
              () ->
                  sql.execute(
                      "INSERT INTO codex_sop_roles(id,name,deleted) VALUES ('duplicate','原角色',FALSE)"))
          .isInstanceOf(SQLException.class);
      sql.execute("UPDATE codex_sop_roles SET deleted=TRUE WHERE id='new'");
      sql.execute("INSERT INTO codex_sop_roles(id,name,deleted) VALUES ('newer','原角色',FALSE)");
      try (var rows = sql.executeQuery("SELECT name,deleted FROM codex_sop_roles WHERE id='old'")) {
        assertThat(rows.next()).isTrue();
        assertThat(rows.getString(1)).isEqualTo("原角色");
        assertThat(rows.getBoolean(2)).isTrue();
      }
    }
  }
}
