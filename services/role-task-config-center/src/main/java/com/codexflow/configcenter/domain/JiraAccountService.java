package com.codexflow.configcenter.domain;

import com.codexflow.configcenter.client.GatewayClient;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 中央账号代理；配置写锁协调 SOP 引用、启动意图和账号删除。 */
@Service
public class JiraAccountService {
  private final GatewayClient gateway;
  private final GroupService groups;
  private final JdbcTemplate jdbc;
  private final ObjectMapper mapper;

  public JiraAccountService(
      GatewayClient gateway, GroupService groups, JdbcTemplate jdbc, ObjectMapper mapper) {
    this.gateway = gateway;
    this.groups = groups;
    this.jdbc = jdbc;
    this.mapper = mapper;
  }

  public JsonNode list(String groupId) {
    if (groupId != null && !groupId.isEmpty()) GroupService.validateId(groupId);
    return gateway.get(
        "/jira-accounts" + (groupId == null || groupId.isEmpty() ? "" : "?groupId=" + groupId));
  }

  @Transactional
  public JsonNode save(String id, JsonNode body) {
    groups.lock();
    groups.require(body.path("groupId").asText());
    if (id == null) return gateway.post("/jira-accounts", body);
    GroupService.validateId(id);
    return gateway.put("/jira-accounts/" + id, body);
  }

  public JsonNode test(String id, JsonNode body) {
    GroupService.validateId(id);
    return gateway.post("/jira-accounts/" + id + "/test", body);
  }

  public JsonNode options(String id, String project, String issueType, boolean refresh) {
    GroupService.validateId(id);
    return gateway.get(
        "/jira-accounts/"
            + id
            + "/options?project="
            + java.net.URLEncoder.encode(project, java.nio.charset.StandardCharsets.UTF_8)
            + "&issueType="
            + java.net.URLEncoder.encode(issueType, java.nio.charset.StandardCharsets.UTF_8)
            + "&refresh="
            + refresh);
  }

  @Transactional
  public JsonNode delete(String id, long version) {
    GroupService.validateId(id);
    groups.lock();
    if (jdbc.queryForObject(
            "SELECT COUNT(*) FROM codex_sop_sops WHERE deleted=FALSE AND jira_connection_id=?",
            Long.class,
            id)
        > 0) throw new ConflictFailure("账号已被有效 SOP 引用，请先调整 SOP。");
    for (String payload :
        jdbc.queryForList(
            "SELECT spec_json FROM codex_jira_requests WHERE state IN ('pending','waiting','dispatching')",
            String.class)) checkReference(id, payload);
    for (String payload :
        jdbc.queryForList(
            "SELECT submitted_json FROM codex_sop_task_runs WHERE status IN ('submitting','queued','running','cancelling')",
            String.class)) checkReference(id, payload);
    return gateway.delete("/jira-accounts/" + id + "?version=" + version);
  }

  private void checkReference(String id, String payload) {
    if (id.equals(mapper.readTree(payload).path("jira").path("connectionId").asText()))
      throw new ConflictFailure("账号仍被待派发来源或未完成运行引用。");
  }
}
