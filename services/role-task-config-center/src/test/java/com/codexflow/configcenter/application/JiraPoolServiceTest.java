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
  void numberedPagesAreForwardedWithoutChangingFilters() {
    when(gateway.get(anyString()))
        .thenReturn(node().put("total", 61).put("page", 2).put("pageSize", 20));
    when(requests.scheduleOverview("")).thenReturn(JsonNodeFactory.instance.arrayNode());
    var result =
        service.board(
            "",
            null,
            "TEST",
            "任务",
            "pending",
            "",
            new JiraPagination(2, 20),
            new JiraPagination(3, 50));
    assertEquals(61, result.path("total").asInt());
    verify(gateway)
        .get(
            "/jira-pool?groupId=&project=TEST&task=%E4%BB%BB%E5%8A%A1&state=pending&page=2&pageSize=20&collectionPage=3&collectionPageSize=50");
    service.statistics("", "TEST", "passed", "A&B", null, true, "", new JiraPagination(3, 100));
    verify(gateway)
        .get(
            "/jira-statistics/items?groupId=&project=TEST&category=passed&developer=A%26B&page=3&pageSize=100");
  }

  @Test
  void invalidPaginationIsRejected() {
    org.junit.jupiter.api.Assertions.assertThrows(
        IllegalArgumentException.class, () -> new JiraPagination(0, 20));
    org.junit.jupiter.api.Assertions.assertThrows(
        IllegalArgumentException.class, () -> new JiraPagination(1, 101));
    org.junit.jupiter.api.Assertions.assertThrows(
        IllegalArgumentException.class, () -> new JiraPagination(1, 20).query(5L));
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
  void continuousFailuresWarnOncePerRequestAndWarnAgainAfterRecovery() {
    var logger =
        (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(JiraPoolService.class);
    var appender =
        new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
    appender.start();
    logger.addAppender(appender);
    try {
      var request = setup(0);
      var error = new IllegalStateException("private details");
      when(requests.status("r"))
          .thenThrow(error)
          .thenThrow(error)
          .thenReturn(request)
          .thenThrow(error);
      when(requests.status("other")).thenThrow(error);
      service.advance("r");
      service.advance("r");
      assertEquals(1, appender.list.size());
      service.advance("other");
      assertEquals(2, appender.list.size());
      service.advance("r");
      service.advance("r");
      assertEquals(3, appender.list.size());
      verify(requests, times(4)).status("r");
      verify(requests).state("r", "empty", "没有待分配工单。");
      for (var event : appender.list) {
        org.junit.jupiter.api.Assertions.assertFalse(
            event.getFormattedMessage().contains("private details"));
      }
    } finally {
      logger.detachAppender(appender);
      appender.stop();
    }
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
