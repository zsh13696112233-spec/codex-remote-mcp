package com.codexflow.configcenter.application;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import java.util.List;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.node.JsonNodeFactory;

class MachineServiceTest {
  private final GatewayClient gateway = mock(GatewayClient.class);
  private final com.codexflow.configcenter.domain.GroupService groups =
      mock(com.codexflow.configcenter.domain.GroupService.class);
  private final MachineService service = new MachineService(gateway, groups);

  @Test
  void registrySopSaveValidatesBothSides() {
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
  void gatewayFailureCannotBypassSopValidation() {
    var body = mock(SopSaveRequest.class);
    when(body.supervisorAgentId()).thenReturn("supervisor");
    when(body.steps()).thenReturn(List.of());
    when(gateway.post(eq("/agents/validate"), any())).thenThrow(new IllegalStateException("网关不可用"));
    org.assertj.core.api.Assertions.assertThatThrownBy(() -> service.validateSop(body))
        .isInstanceOf(IllegalStateException.class);
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
    verify(groups).save(null, body);
    verify(groups).save("group/a", body);
    verify(groups).delete("empty");
    verify(gateway).post("/agents", body);
    verify(gateway).put("/agents/worker", body);
    verify(gateway).post(eq("/agents/worker/test"), any());
  }
}
