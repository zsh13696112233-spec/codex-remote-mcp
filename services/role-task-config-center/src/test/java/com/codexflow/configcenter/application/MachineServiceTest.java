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
  void machineRemarksPassThroughWithoutChangingIdentifiers() {
    var body = JsonNodeFactory.instance.objectNode().put("remark", "开发机");
    var saved = body.deepCopy().put("agentId", "worker");
    var listing = JsonNodeFactory.instance.objectNode();
    listing.putArray("agents").add(saved);
    when(gateway.post("/agents", body)).thenReturn(saved);
    when(gateway.put("/agents/worker", body)).thenReturn(saved);
    when(gateway.get("/agents")).thenReturn(listing);
    org.assertj.core.api.Assertions.assertThat(service.createMachine(body)).isEqualTo(saved);
    org.assertj.core.api.Assertions.assertThat(service.updateMachine("worker", body))
        .isEqualTo(saved);
    org.assertj.core.api.Assertions.assertThat(service.machines()).isEqualTo(listing);
    verify(gateway).put("/agents/worker", body);
  }

  @Test
  void businessDeleteChecksReferencesBeforeCallingGateway() {
    service.deleteMachine("worker");
    var order = inOrder(groups, gateway);
    order.verify(groups).lock();
    order.verify(groups).checkMachineDelete("worker");
    order.verify(gateway).delete("/agents/worker");
    doThrow(new IllegalStateException("机器被引用")).when(groups).checkMachineDelete("used");
    org.assertj.core.api.Assertions.assertThatThrownBy(() -> service.deleteMachine("used"))
        .hasMessageContaining("引用");
    verify(gateway, never()).delete("/agents/used");
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
    service.updateRemark("worker", body);
    verify(groups).save(null, body);
    verify(groups).save("group/a", body);
    verify(groups).delete("empty");
    verify(gateway).post("/agents", body);
    verify(gateway).put("/agents/worker", body);
    verify(gateway).put("/agents/worker/remark", body);
    verify(gateway).post(eq("/agents/worker/test"), any());
  }
}
