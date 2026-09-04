package com.codexflow.configcenter.application;

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.PreparedRun;
import com.codexflow.configcenter.domain.TaskLaunchStore;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import com.codexflow.configcenter.integration.dingtalk.DingTalkProactiveNotificationService;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 验证提交响应不明确时的幂等恢复和运行槽释放边界。 */
class WorkflowRunServiceTest {

  private static final String WORKFLOW_ID = "00000000-0000-4000-8000-000000000901";

  private final ObjectMapper objectMapper = new ObjectMapper();
  private WorkflowRunStore store;
  private TaskLaunchStore launches;
  private GatewayClient gateway;
  private WorkflowRunService service;
  private PreparedRun prepared;

  @BeforeEach
  void setUp() {
    store = mock(WorkflowRunStore.class);
    launches = mock(TaskLaunchStore.class);
    gateway = mock(GatewayClient.class);
    service =
        new WorkflowRunService(
            store, launches, gateway, mock(DingTalkProactiveNotificationService.class));
    prepared =
        new PreparedRun(
            WORKFLOW_ID, objectMapper.createObjectNode().put("workflowId", WORKFLOW_ID));
  }

  @Test
  void ambiguousTransportFailureKeepsSubmittingReservation() {
    GatewayFailure failure = new GatewayFailure(502, "response lost");
    when(gateway.post("/workflows", prepared.payload())).thenThrow(failure);
    when(gateway.get("/workflows/" + WORKFLOW_ID)).thenThrow(new GatewayFailure(404, "missing"));

    assertThatThrownBy(() -> service.submitPrepared(prepared)).isSameAs(failure);

    verify(store, never()).recordSubmissionFailure(WORKFLOW_ID, failure);
    verify(launches, never()).release(WORKFLOW_ID);
  }

  @Test
  void definitiveGatewayRejectionMarksFailureAndReleasesReservation() {
    GatewayFailure failure = new GatewayFailure(400, "invalid payload");
    when(gateway.post("/workflows", prepared.payload())).thenThrow(failure);
    when(gateway.get("/workflows/" + WORKFLOW_ID)).thenThrow(new GatewayFailure(404, "missing"));

    assertThatThrownBy(() -> service.submitPrepared(prepared)).isSameAs(failure);

    verify(store).recordSubmissionFailure(WORKFLOW_ID, failure);
    verify(launches).release(WORKFLOW_ID);
  }

  @Test
  void responseLossUsesExistingGatewayWorkflowAsSuccess() {
    ObjectNode live =
        objectMapper.createObjectNode().put("workflowId", WORKFLOW_ID).put("status", "queued");
    ObjectNode saved = objectMapper.createObjectNode().put("workflowId", WORKFLOW_ID);
    when(gateway.post("/workflows", prepared.payload()))
        .thenThrow(new GatewayFailure(502, "response lost"));
    when(gateway.get("/workflows/" + WORKFLOW_ID)).thenReturn(live);
    when(store.recordGatewayStatus(WORKFLOW_ID, live, "queued")).thenReturn(saved);
    when(store.monitorUrl(WORKFLOW_ID)).thenReturn("http://monitor/?workflowId=" + WORKFLOW_ID);

    service.submitPrepared(prepared);

    verify(store).recordGatewayStatus(WORKFLOW_ID, live, "queued");
    verify(launches, never()).release(WORKFLOW_ID);
  }

  @Test
  void reconciliationResubmitsPersistedWorkflowMissingFromGateway() {
    ObjectNode accepted =
        objectMapper.createObjectNode().put("workflowId", WORKFLOW_ID).put("status", "queued");
    when(launches.activeLaunches())
        .thenReturn(List.of(new TaskLaunchStore.ActiveLaunch("task-1", WORKFLOW_ID)));
    when(gateway.get("/workflows/" + WORKFLOW_ID)).thenThrow(new GatewayFailure(404, "missing"));
    when(store.runStatus(WORKFLOW_ID)).thenReturn("submitting");
    when(store.getPrepared(WORKFLOW_ID)).thenReturn(prepared);
    when(gateway.post("/workflows", prepared.payload())).thenReturn(accepted);
    when(store.recordGatewayStatus(WORKFLOW_ID, accepted, "queued"))
        .thenReturn(objectMapper.createObjectNode().put("workflowId", WORKFLOW_ID));

    service.reconcileActiveRuns();

    verify(gateway).post("/workflows", prepared.payload());
    verify(store).recordGatewayStatus(WORKFLOW_ID, accepted, "queued");
  }
}
