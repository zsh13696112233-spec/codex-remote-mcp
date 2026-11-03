package com.codexflow.configcenter.application;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.*;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

class JiraPoolServiceTest {
  private final JiraRequestStore requests = mock(JiraRequestStore.class);
  private final GatewayClient gateway = mock(GatewayClient.class);
  private final WorkflowRunService workflows = mock(WorkflowRunService.class);
  private final WorkflowRunStore runs = mock(WorkflowRunStore.class);
  private final JiraPoolService service = new JiraPoolService(requests, gateway, workflows, runs);

  private ObjectNode node() {
    return JsonNodeFactory.instance.objectNode();
  }

  private ObjectNode setup(int pending) {
    var request = node().put("source", "web");
    request.set("spec", node());
    when(requests.status("r")).thenReturn(request);
    when(gateway.post(eq("/jira-collections"), any()))
        .thenReturn(node().put("state", "ready").put("pending", pending));
    return request;
  }

  @Test
  void boardAddsMonitorLinksForEveryHistoricalExecutionEvenWhenQueuedAgain() {
    var result = node();
    var item = result.putArray("items").addObject();
    item.putNull("workflow_id");
    var executions = item.putArray("executions");
    executions.addObject().put("workflowId", "first").put("reason", "首次阻断");
    executions.addObject().put("workflowId", "retry").put("reworkNumber", 1);
    when(gateway.get("/jira-pool?groupId=&project=&task=&state=")).thenReturn(result);
    when(runs.monitorUrl("first")).thenReturn("/monitor?workflowId=first");
    when(runs.monitorUrl("retry")).thenReturn("/monitor?workflowId=retry");
    when(requests.scheduleOverview("")).thenReturn(JsonNodeFactory.instance.arrayNode());

    var board = service.board("", null, "", "", "");

    assertEquals(
        "/monitor?workflowId=first",
        board.path("items").get(0).path("executions").get(0).path("monitorUrl").asText());
    assertEquals(
        "/monitor?workflowId=retry",
        board.path("items").get(0).path("executions").get(1).path("monitorUrl").asText());
    assertEquals(
        "首次阻断", board.path("items").get(0).path("executions").get(0).path("reason").asText());
  }

  @Test
  void noIssuesDoesNotCreateRunOrExecuteCheck() {
    setup(0);
    service.advance("r");
    verify(requests).state("r", "empty", "没有待分配工单。");
    verify(requests, never()).prepare(any(), any());
    verify(requests, never()).checkPayload(any());
    verifyNoInteractions(workflows);
  }

  @Test
  void waitsForCapacityBeforeCheckOrReservation() {
    setup(2);
    when(gateway.post(eq("/jira-capacity"), any())).thenReturn(node().put("capacity", 0));
    service.advance("r");
    verify(requests).state(eq("r"), eq("waiting"), anyString());
    verify(requests, never()).checkPayload(any());
    verify(requests, never()).reservation(any());
  }

  @Test
  void busyCheckMachineKeepsCollectionPendingWithoutStartingRun() {
    setup(2).put("source", "schedule");
    when(gateway.post(eq("/jira-capacity"), any())).thenReturn(node().put("capacity", 1));
    when(requests.checkPayload("r")).thenReturn(node());
    when(gateway.checkSchedule(any()))
        .thenReturn(
            node().put("status", "skipped").put("retryable", true).put("reason", "检查执行机忙。"));
    service.advance("r");
    verify(requests).checkFinished("r", false, true, "检查执行机忙。");
    verify(gateway, never()).delete(any());
    verify(requests, never()).reservation(any());
    verifyNoInteractions(workflows);
  }

  @Test
  void falseCheckResultStopsBatchAndPreservesReason() {
    setup(2).put("source", "schedule");
    when(gateway.post(eq("/jira-capacity"), any())).thenReturn(node().put("capacity", 1));
    when(requests.checkPayload("r")).thenReturn(node());
    when(gateway.checkSchedule(any()))
        .thenReturn(node().put("status", "skipped").put("reason", "条件不满足，已跳过本次。"));
    service.advance("r");
    verify(requests).checkFinished("r", false, false, "条件不满足，已跳过本次。");
    verify(gateway).delete("/jira-collections/r");
    verify(requests, never()).reservation(any());
  }

  @Test
  void unknownVerificationDoesNotLoseLastReservedIssue() {
    setup(0).put("reservationId", "reservation");
    when(requests.reservation("r")).thenReturn("reservation");
    when(gateway.post(eq("/jira-reservations"), any()))
        .thenReturn(node().putNull("reservation").put("state", "waiting_verification"));
    when(gateway.get("/jira-collections/r")).thenReturn(node().put("pending", 0));
    service.advance("r");
    verify(requests).state(eq("r"), eq("waiting"), anyString());
    verify(requests, never()).prepare(any(), any());
  }

  @Test
  void existingRunWaitingForSubmissionKeepsReservation() {
    var request = setup(0).put("reservationId", "reservation").put("workflowId", "run");
    when(requests.reservation("r")).thenReturn("reservation");
    when(gateway.post(eq("/jira-reservations"), any()))
        .thenReturn(node().set("reservation", node()));
    var prepared =
        new TaskLaunchStore.LaunchReservation("task", new PreparedRun("run", node()), false);
    when(requests.prepare(eq("r"), any())).thenReturn(prepared);
    when(workflows.submitPrepared(any())).thenThrow(new ConflictFailure("等待历史升级登记"));
    when(runs.runStatus("run")).thenReturn("submitting");
    service.advance("r");
    verify(gateway, never()).delete(any());
    verify(requests).state(eq("r"), eq("dispatching"), anyString());
  }
}
