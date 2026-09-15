package db.migration;

import java.sql.Connection;
import org.flywaydb.core.api.migration.BaseJavaMigration;
import org.flywaydb.core.api.migration.Context;

/** 保留历史名称，仅对未删除角色建立唯一约束，兼容 H2 自动生成的旧约束名。 */
public class V29__release_deleted_role_names extends BaseJavaMigration {
  @Override
  public void migrate(Context context) throws Exception {
    migrate(context.getConnection());
  }

  public static void migrate(Connection connection) throws Exception {
    boolean h2 = "H2".equals(connection.getMetaData().getDatabaseProductName());
    try (var sql = connection.createStatement()) {
      sql.execute(
          "ALTER TABLE codex_sop_roles ADD COLUMN active_name VARCHAR(100) GENERATED ALWAYS AS (CASE WHEN deleted=FALSE THEN name ELSE NULL END)");
      sql.execute("CREATE UNIQUE INDEX uq_roles_active_name ON codex_sop_roles(active_name)");
      if (h2) {
        String constraint;
        try (var rows =
            sql.executeQuery(
                "SELECT CONSTRAINT_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS WHERE UPPER(TABLE_NAME)='CODEX_SOP_ROLES' AND TABLE_SCHEMA=CURRENT_SCHEMA AND CONSTRAINT_TYPE='UNIQUE'")) {
          if (!rows.next()) throw new IllegalStateException("缺少原角色名称唯一约束");
          constraint = rows.getString(1);
          if (rows.next()) throw new IllegalStateException("角色名称唯一约束不明确");
        }
        sql.execute(
            "ALTER TABLE codex_sop_roles DROP CONSTRAINT \""
                + constraint.replace("\"", "\"\"")
                + "\"");
      } else {
        sql.execute("ALTER TABLE codex_sop_roles DROP INDEX name");
      }
    }
  }
}
