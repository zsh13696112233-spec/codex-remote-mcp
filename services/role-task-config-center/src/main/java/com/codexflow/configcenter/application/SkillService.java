package com.codexflow.configcenter.application;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.GroupService;
import java.nio.charset.StandardCharsets;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;

/** Skill 元数据和下发状态由中央网关保存，配置中心只提供管理代理。 */
@Service
public class SkillService {
  private final GatewayClient gateway;

  public SkillService(GatewayClient gateway) {
    this.gateway = gateway;
  }

  private String scoped(String path, String groupId) {
    if (groupId == null || groupId.isEmpty()) return path;
    GroupService.validateId(groupId);
    return path + "?groupId=" + groupId;
  }

  public ResponseEntity<JsonNode> list(boolean machines, String groupId) {
    return gateway.skillExchange(
        "GET",
        scoped(machines ? "/skills/machines" : "/skills", groupId),
        null,
        "application/json");
  }

  public ResponseEntity<JsonNode> upload(byte[] zip, String groupId) {
    GroupService.validateId(groupId);
    return gateway.skillExchange("POST", scoped("/skills", groupId), zip, "application/zip");
  }

  public ResponseEntity<JsonNode> inventory(String groupId) {
    return gateway.skillExchange(
        "GET", scoped("/skills/inventory", groupId), null, "application/json");
  }

  public ResponseEntity<JsonNode> assign(JsonNode body) {
    return send("/skills/groups/assign", body);
  }

  public ResponseEntity<JsonNode> checkDirectory(String id) {
    if (!id.matches("machine-[a-f0-9]{32}")) throw new IllegalArgumentException("执行机编号不正确。");
    return gateway.skillExchange(
        "POST",
        "/skills/machines/" + id + "/check",
        "{}".getBytes(StandardCharsets.UTF_8),
        "application/json");
  }

  public ResponseEntity<JsonNode> detail(String id) {
    if (!id.matches("[a-f0-9]{64}")) throw new IllegalArgumentException("Skill 包编号不正确。");
    return gateway.skillExchange("GET", "/skills/" + id, null, "application/json");
  }

  public ResponseEntity<JsonNode> deployments(String id, String groupId) {
    return gateway.skillExchange(
        "GET",
        scoped("/skill-deployments" + (id == null ? "" : "/" + id), groupId),
        null,
        "application/json");
  }

  public ResponseEntity<JsonNode> create(JsonNode body) {
    return send("/skill-deployments", body);
  }

  public ResponseEntity<JsonNode> action(String id, String action, JsonNode body) {
    if (!action.equals("retry") && !action.equals("check")) {
      throw new IllegalArgumentException("不支持的安装操作。");
    }
    return send("/skill-deployment-tasks/" + id + "/" + action, body);
  }

  private ResponseEntity<JsonNode> send(String path, JsonNode body) {
    return gateway.skillExchange(
        "POST", path, body.toString().getBytes(StandardCharsets.UTF_8), "application/json");
  }
}
