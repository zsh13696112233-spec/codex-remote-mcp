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
  void statisticsEncodesFiltersAndLinksSelectedResultOnly() {
    var result = node();
    result.putArray("items").addObject().put("workflowId", "final");
    when(gateway.get(
            "/jira-statistics/items?groupId=&project=TEST&category=passed&developer=A%26B&before=7"))
        .thenReturn(result);
    when(runs.monitorUrl("final")).thenReturn("/monitor?workflowId=final");
    var actual = service.statistics("", "TEST", "passed", "A&B", 7L, true);
    assertEquals(
        "/monitor?workflowId=final", actual.path("items").get(0).path("monitorUrl").asText());
    verifyNoInteractions(requests, workflows);
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
  void noIssuesDoesNotCreateRun() {
    setup(0);
    service.advance("r");
    verify(requests).state("r", "empty", "没有待分配工单。");
    verify(requests, never()).prepare(any(), any());
    verifyNoInteractions(workflows);
  }

  @Test
  void waitsForCapacityBeforeReservation() {
    setup(2);
    when(gateway.post(eq("/jira-capacity"), any())).thenReturn(node().put("capacity", 0));
    service.advance("r");
    verify(requests).state(eq("r"), eq("waiting"), anyString());
    verify(requests, never()).reservation(any());
  }

  @Test
  void scheduledIssueWithCapacityIsReservedAndSubmittedDirectly() {
    setup(1).put("source", "schedule");
    when(gateway.post(eq("/jira-capacity"), any())).thenReturn(node().put("capacity", 1));
    when(requests.reservation("r")).thenReturn("reservation");
    when(gateway.post(eq("/jira-reservations"), any()))
        .thenReturn(node().set("reservation", node().put("issueKey", "TEST-1")));
    when(gateway.get("/jira-collections/r")).thenReturn(node().put("pending", 0));
    var prepared =
        new TaskLaunchStore.LaunchReservation("task", new PreparedRun("run", node()), false);
    when(requests.prepare(eq("r"), any())).thenReturn(prepared);
    service.advance("r");
    verify(workflows).submitReservation(eq(prepared), eq("schedule"), anyString());
    verify(requests).dispatched("r", false);
    verify(gateway).post(eq("/jira-collections"), any());
    verify(gateway).post(eq("/jira-capacity"), any());
    verify(gateway).post(eq("/jira-reservations"), any());
    verify(gateway).get("/jira-collections/r");
    verifyNoMoreInteractions(gateway);
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
