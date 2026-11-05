package com.codexflow.configcenter.domain;

import com.codexflow.configcenter.client.GatewayClient;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 同步独立采集范围，不创建启动请求或占用执行资源。 */
@Service
public class JiraCollectionService {
  private final JdbcTemplate jdbc;
  private final GatewayClient gateway;
  private final DomainJsonMapper json;

  public JiraCollectionService(JdbcTemplate jdbc, GatewayClient gateway, DomainJsonMapper json) {
    this.jdbc = jdbc;
    this.gateway = gateway;
    this.json = json;
  }

  @Transactional
  public void synchronize() {
    // 与配置修改共用锁，防止多个配置中心实例乱序覆盖采集范围。
    jdbc.queryForObject(
        "SELECT id FROM codex_group_write_lock WHERE id=1 FOR UPDATE", Integer.class);
    var body = json.newObject();
    var tasks = body.putArray("tasks");
    for (var row :
        jdbc.queryForList(
            "SELECT t.id,t.name,t.group_id,s.jira_jql,s.jira_connection_id FROM codex_sop_task_definitions t JOIN codex_sop_sops s ON s.id=t.sop_id WHERE t.deleted=FALSE AND t.enabled=TRUE AND s.deleted=FALSE AND s.enabled=TRUE AND s.dispatch_mode='group' AND t.group_id=s.group_id AND s.jira_connection_id IS NOT NULL AND s.jira_jql IS NOT NULL")) {
      var task =
          tasks
              .addObject()
              .put("taskDefinitionId", row.get("id").toString())
              .put("name", row.get("name").toString())
              .put("groupId", row.get("group_id").toString());
      task.putObject("jira")
          .put("connectionId", row.get("jira_connection_id").toString())
          .put("jql", row.get("jira_jql").toString());
    }
    gateway.post("/jira-polling", body);
  }
}
