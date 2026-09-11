package com.codexflow.configcenter.application;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import tools.jackson.databind.node.JsonNodeFactory;

class MachineServiceTest {
  private final GatewayClient gateway = mock(GatewayClient.class);
  private final MachineService service = new MachineService(gateway);

  @Test
  void fileModeKeepsOfflineSopSave() {
    ReflectionTestUtils.setField(service, "source", "file");
    service.validateSop(null);
    verifyNoInteractions(gateway);
  }

  @Test
  void registrySopSaveValidatesBothSides() {
    ReflectionTestUtils.setField(service, "source", "registry");
    when(gateway.get("/agents"))
        .thenReturn(JsonNodeFactory.instance.objectNode().put("source", "registry"));
    var body = mock(SopSaveRequest.class);
    var step = mock(SopStepRequest.class);
    when(body.supervisorAgentId()).thenReturn("supervisor");
    when(step.agentId()).thenReturn("worker");
    when(body.steps()).thenReturn(List.of(step));
    service.validateSop(body);
    verify(gateway)
        .post(
            eq("/agents/validate"),
            argThat(
                json ->
                    "supervisor".equals(json.path("supervisorId").asText())
                        && "worker".equals(json.path("executorIds").get(0).asText())));
  }

  @Test
  void managementUsesGatewayAndEncodesIdentifiers() {
    var body = JsonNodeFactory.instance.objectNode().put("name", "第一组");
    service.createGroup(body);
    service.updateGroup("group/a", body);
    service.deleteGroup("empty");
    service.createMachine(body);
    service.updateMachine("worker", body);
    service.testMachine("worker");
    service.importMachines();
    verify(gateway).post("/agent-groups", body);
    verify(gateway).put("/agent-groups/group%2Fa", body);
    verify(gateway).delete("/agent-groups/empty");
    verify(gateway).post("/agents", body);
    verify(gateway).put("/agents/worker", body);
    verify(gateway).post(eq("/agents/worker/test"), any());
    verify(gateway).post(eq("/agents/import"), any());
  }
}
