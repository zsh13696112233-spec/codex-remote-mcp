package com.codexflow.configcenter.application;

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.ConflictFailure;
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

  @Test
  void oldSnapshotsReceiveRuntimeScopeWithoutChangingStoredPayload() {
    when(store.taskDefinitionId(WORKFLOW_ID)).thenReturn("task-1");
    ObjectNode accepted = objectMapper.createObjectNode().put("status", "queued");
    when(gateway.post(
            org.mockito.ArgumentMatchers.eq("/workflows"), org.mockito.ArgumentMatchers.any()))
        .thenReturn(accepted);
    when(store.recordGatewayStatus(WORKFLOW_ID, accepted, "queued"))
        .thenReturn(objectMapper.createObjectNode());
    service.submitPrepared(prepared);
    verify(gateway, never())
        .post(
            org.mockito.ArgumentMatchers.eq("/workflow-task-bindings"),
            org.mockito.ArgumentMatchers.any());
    verify(store, never()).pendingRuntimeScopes("task-1");
    verify(store).markRuntimeScopes(List.of(WORKFLOW_ID));
    var payload = org.mockito.ArgumentCaptor.forClass(tools.jackson.databind.JsonNode.class);
    verify(gateway).post(org.mockito.ArgumentMatchers.eq("/workflows"), payload.capture());
    org.assertj.core.api.Assertions.assertThat(payload.getValue().path("taskDefinitionId").asText())
        .isEqualTo("task-1");
    org.assertj.core.api.Assertions.assertThat(prepared.payload().has("taskDefinitionId"))
        .isFalse();
  }

  @Test
  void pendingHistoryDefersSubmissionAndReconciliationReusesTheSameRun() {
    when(store.taskDefinitionId(WORKFLOW_ID)).thenReturn("task-1");
    when(store.hasPendingRuntimeHistory("task-1", WORKFLOW_ID)).thenReturn(true);
    when(gateway.get("/workflows/" + WORKFLOW_ID)).thenThrow(new GatewayFailure(404, "missing"));

    assertThatThrownBy(() -> service.submitPrepared(prepared))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("自动继续提交");
    verify(gateway, never())
        .post(org.mockito.ArgumentMatchers.anyString(), org.mockito.ArgumentMatchers.any());
    verify(store, never())
        .recordSubmissionFailure(
            org.mockito.ArgumentMatchers.anyString(), org.mockito.ArgumentMatchers.any());
    verify(launches, never()).release(WORKFLOW_ID);

    when(store.pendingRuntimeScopeTasks()).thenReturn(List.of("task-1"));
    when(store.pendingRuntimeScopes("task-1")).thenReturn(List.of("older"));
    org.mockito.Mockito.doAnswer(
            invocation -> {
              when(store.hasPendingRuntimeHistory("task-1", WORKFLOW_ID)).thenReturn(false);
              return null;
            })
        .when(store)
        .markRuntimeScopes(List.of("older"));
    service.synchronizeRuntimeScopes();

    when(launches.activeLaunches())
        .thenReturn(List.of(new TaskLaunchStore.ActiveLaunch("task-1", WORKFLOW_ID)));
    when(store.runStatus(WORKFLOW_ID)).thenReturn("submitting");
    when(store.getPrepared(WORKFLOW_ID)).thenReturn(prepared);
    ObjectNode payload = prepared.payload().deepCopy().put("taskDefinitionId", "task-1");
    ObjectNode accepted = objectMapper.createObjectNode().put("status", "queued");
    when(gateway.post("/workflows", payload)).thenReturn(accepted);
    when(store.recordGatewayStatus(WORKFLOW_ID, accepted, "queued"))
        .thenReturn(objectMapper.createObjectNode());

    service.reconcileActiveRuns();

    verify(gateway).post("/workflows", payload);
    verify(store).recordGatewayStatus(WORKFLOW_ID, accepted, "queued");
    verify(launches, never()).release(WORKFLOW_ID);
    verify(launches, never()).reserveLatest(org.mockito.ArgumentMatchers.anyString());
  }

  @Test
  void backgroundScopeSyncProcessesOneBatchPerTaskAndRetriesFailuresIndependently() {
    List<String> batch =
        java.util.stream.IntStream.range(0, 200).mapToObj(i -> "old-" + i).toList();
    when(store.pendingRuntimeScopeTasks()).thenReturn(List.of("task-1", "task-2"));
    when(store.pendingRuntimeScopes("task-1")).thenReturn(batch);
    when(store.pendingRuntimeScopes("task-2")).thenReturn(List.of("other"));
    ObjectNode request = objectMapper.createObjectNode().put("taskDefinitionId", "task-1");
    var ids = request.putArray("workflowIds");
    batch.forEach(ids::add);
    when(gateway.post("/workflow-task-bindings", request))
        .thenThrow(new GatewayFailure(502, "temporarily unavailable"))
        .thenReturn(objectMapper.createObjectNode().put("registered", true));

    service.synchronizeRuntimeScopes();

    verify(store).pendingRuntimeScopes("task-1");
    verify(store, never()).markRuntimeScopes(batch);
    verify(store).markRuntimeScopes(List.of("other"));

    service.synchronizeRuntimeScopes();

    verify(store).markRuntimeScopes(batch);
    verify(store, org.mockito.Mockito.times(2)).pendingRuntimeScopes("task-1");
  }

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
