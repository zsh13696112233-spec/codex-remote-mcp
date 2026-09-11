package com.codexflow.configcenter.application;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.dto.SopSaveRequest;
import java.nio.charset.StandardCharsets;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.web.util.UriUtils;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.JsonNodeFactory;

/** 机器登记由 Python 持久化，配置中心只代理管理和校验。 */
@Service
public class MachineService {
  private final GatewayClient gateway;

  @Value("${CODEX_AGENT_SOURCE:file}")
  private String source;

  public MachineService(GatewayClient gateway) {
    this.gateway = gateway;
  }

  public JsonNode groups() {
    return gateway.get("/agent-groups");
  }

  public JsonNode machines() {
    return gateway.get("/agents");
  }

  public JsonNode createGroup(JsonNode body) {
    return gateway.post("/agent-groups", body);
  }

  public JsonNode updateGroup(String id, JsonNode body) {
    return gateway.put("/agent-groups/" + segment(id), body);
  }

  public JsonNode deleteGroup(String id) {
    return gateway.delete("/agent-groups/" + segment(id));
  }

  public JsonNode createMachine(JsonNode body) {
    return gateway.post("/agents", body);
  }

  public JsonNode updateMachine(String id, JsonNode body) {
    return gateway.put("/agents/" + segment(id), body);
  }

  public JsonNode testMachine(String id) {
    return gateway.post("/agents/" + segment(id) + "/test", JsonNodeFactory.instance.objectNode());
  }

  public JsonNode importMachines() {
    return gateway.post("/agents/import", JsonNodeFactory.instance.objectNode());
  }

  public void validateSop(SopSaveRequest body) {
    if (!"registry".equals(source)) return;
    var agents = machines();
    if (!"registry".equals(agents.path("source").asText())) {
      throw new IllegalArgumentException("配置中心与网关的机器登记模式不一致，请检查部署配置。");
    }
    var request =
        JsonNodeFactory.instance.objectNode().put("supervisorId", body.supervisorAgentId());
    var executors = request.putArray("executorIds");
    body.steps().forEach(step -> executors.add(step.agentId()));
    gateway.post("/agents/validate", request);
  }

  private static String segment(String id) {
    return UriUtils.encodePathSegment(id, StandardCharsets.UTF_8);
  }
}
