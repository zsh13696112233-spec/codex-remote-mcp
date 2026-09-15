package db.migration;

import java.sql.Connection;
import java.util.ArrayList;
import org.flywaydb.core.api.migration.BaseJavaMigration;
import org.flywaydb.core.api.migration.Context;
import tools.jackson.databind.ObjectMapper;

/** 分页读取冻结名称，仅使用明确的通知绑定来源回填，兼容 MySQL 与 H2。 */
public class V26__backfill_run_catalog extends BaseJavaMigration {
  @Override
  public void migrate(Context context) throws Exception {
    backfill(context.getConnection());
  }

  public static void backfill(Connection connection) throws Exception {
    ObjectMapper mapper = new ObjectMapper();
    String after = "";
    while (true) {
      var batch = new ArrayList<String[]>();
      try (var query =
          connection.prepareStatement(
              "SELECT r.workflow_id, r.snapshot_json, b.trigger_source FROM codex_sop_task_runs r "
                  + "LEFT JOIN codex_sop_dingtalk_workflow_bindings b ON b.workflow_id = r.workflow_id "
                  + "WHERE r.workflow_id > ? ORDER BY r.workflow_id LIMIT 100")) {
        query.setString(1, after);
        try (var rows = query.executeQuery()) {
          while (rows.next()) {
            String source = rows.getString(3);
            if (source == null || !java.util.Set.of("dingtalk", "schedule", "web").contains(source))
              source = "unknown";
            batch.add(
                new String[] {
                  rows.getString(1),
                  mapper.readTree(rows.getString(2)).path("name").asText(""),
                  source
                });
          }
        }
      }
      if (batch.isEmpty()) return;
      try (var update =
          connection.prepareStatement(
              "UPDATE codex_sop_task_runs SET run_name = ?, trigger_source = ? WHERE workflow_id = ?")) {
        for (String[] row : batch) {
          update.setString(1, row[1]);
          update.setString(2, row[2]);
          update.setString(3, row[0]);
          update.addBatch();
        }
        update.executeBatch();
      }
      after = batch.get(batch.size() - 1)[0];
    }
  }
}
