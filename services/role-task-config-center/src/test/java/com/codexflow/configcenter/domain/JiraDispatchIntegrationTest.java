package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.when;

import com.codexflow.configcenter.GroupedFixtureSupport;
import com.codexflow.configcenter.dto.JiraDispatchRequest;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import com.codexflow.configcenter.dto.TaskScheduleRequest;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import tools.jackson.databind.node.ObjectNode;

@SpringBootTest
class JiraDispatchIntegrationTest extends GroupedFixtureSupport {
  @Autowired JiraRequestStore requests;
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired TaskLaunchStore launches;
  @Autowired TaskScheduleStore schedules;
  @Autowired JdbcTemplate jdbc;
  @Autowired com.codexflow.configcenter.application.WorkflowRunService service;

  private ObjectNode task() {
    return task("project = TEST AND status = 待办 AND assignee = currentUser()");
  }

  private ObjectNode task(String query) {
    String role =
        jdbc.queryForObject(
            "SELECT id FROM codex_sop_roles ORDER BY created_at LIMIT 1", String.class);
    var step =
        new SopStepRequest(
            "步骤", role, "处理工单", null, null, null, null, null, null, null, null, Set.of(), Set.of());
    var sop =
        config.createSop(
            new SopSaveRequest(
                "Jira " + UUID.randomUUID(),
                null,
                null,
                null,
                null,
                true,
                "automatic",
                null,
                List.of(step),
                GROUP,
                false,
                null,
                "group",
                new JiraDispatchRequest(query, "bot")));
    assertThat(sop.path("jira").path("account").asText()).isEqualTo("bot");
    return config.createTask(
        grouped(
            new TaskDefinitionSaveRequest(
                "Jira task " + UUID.randomUUID(),
                "处理一项",
                sop.path("id").asText(),
                null,
                true,
                null,
                false,
                null,
                false)));
  }

  @Test
  void busyCheckReturnsRequestToWaitingAndAllowsNextCheck() {
    String id =
        requests.enqueue(task().path("id").asText(), "web", null).path("requestId").asText();
    requests.checkFinished(id, false, true, "检查执行机忙。");
    assertThat(requests.status(id).path("state").asText()).isEqualTo("waiting");
    assertThat(requests.status(id).path("reason").asText()).isEqualTo("检查执行机忙。");
    assertThat(
            jdbc.queryForObject(
                "SELECT check_state FROM codex_jira_requests WHERE id=?", String.class, id))
        .isEqualTo("pending");
    requests.checkFinished(id, false, false, "脚本首条非空输出必须为 TRUE 或 FALSE。");
    assertThat(requests.status(id).path("state").asText()).isEqualTo("failed");
    assertThat(requests.status(id).path("reason").asText()).contains("TRUE 或 FALSE");
  }

  @Test
  void jiraRunsFreezeConfigurationAndDoNotUseSingleTaskSlot() {
    var task = task();
    String id = task.path("id").asText();
    var first = launches.reserveLatest(id).prepared();
    var second = launches.reserveLatest(id).prepared();
    assertThat(first.workflowId()).isNotEqualTo(second.workflowId());
    assertThat(launches.activeWorkflowId(id)).isEmpty();
    assertThat(launches.activeLaunches())
        .extracting(TaskLaunchStore.ActiveLaunch::workflowId)
        .contains(first.workflowId(), second.workflowId());
    jdbc.update(
        "UPDATE codex_sop_sops SET jira_jql='project = CHANGED',jira_account='other' WHERE id=?",
        task.path("sopId").asText());
    var retry = runs.prepareRetry(first.workflowId());
    assertThat(retry.payload().path("jira").path("jql").asText())
        .isEqualTo(
            "project = TEST AND status = 10205 AND assignee = currentUser() AND \"处理角色\" = \"Planner\"");
    assertThat(retry.payload().path("jira").path("account").asText()).isEqualTo("bot");
    assertThat(retry.payload().path("jiraRetryOf").asText()).isEqualTo(first.workflowId());
    assertThat(first.payload().has("supervisorAgentId")).isFalse();
    assertThat(first.payload().path("nodes").get(0).has("cwd")).isFalse();
  }

  @Test
  void jiraQueryCannotBypassSingleStatusAndCurrentAssignee() {
    for (String query :
        List.of(
            "project = TEST",
            "project = TEST AND status = 10205",
            "project = TEST AND status in (待办, Closed) AND assignee = currentUser()",
            "project = TEST AND status = 待办 AND assignee = currentUser() OR project = OTHER",
            "project = TEST AND status = 待办 AND assignee = other",
            "project = TEST AND status = 待办 AND assignee = \"currentUser()\"",
            "project = TEST AND status = 待办 AND assignee = currentUser() AND priority = High")) {
      assertThatThrownBy(() -> task(query))
          .isInstanceOf(IllegalArgumentException.class)
          .hasMessageContaining("只能指定项目和单个状态");
    }
    assertThatThrownBy(() -> task("project = \"   \" AND status = 待办 AND assignee = currentUser()"))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("项目名称");
  }

  @Test
  void customJiraStatusIsPreservedInRunAndRetrySnapshots() {
    for (String status : List.of("Open", "10206", "\"In Progress\"", "\"进行中\"")) {
      String query = "project = TEST AND status = " + status + " AND assignee = currentUser()";
      var task = task(query);
      var run = launches.reserveLatest(task.path("id").asText()).prepared();
      assertThat(run.payload().path("jira").path("jql").asText())
          .isEqualTo(query + " AND \"处理角色\" = \"Planner\"");
      assertThat(runs.prepareRetry(run.workflowId()).payload().path("jira").path("jql").asText())
          .isEqualTo(query + " AND \"处理角色\" = \"Planner\"");
    }
    assertThatThrownBy(() -> task("project = TEST AND status = '   ' AND assignee = currentUser()"))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("Jira 状态");
  }

  @Test
  void jiraProjectEscapesArePreservedAndStatusIsCanonicalPending() {
    String project = "\"研发 \\\"A\\\"\\\\B\"";
    var task = task("project = " + project + " AND status = '待办' AND assignee = currentUser()");
    var run = launches.reserveLatest(task.path("id").asText()).prepared();
    assertThat(run.payload().path("jira").path("jql").asText())
        .isEqualTo(
            "project = "
                + project
                + " AND status = 10205 AND assignee = currentUser() AND \"处理角色\" = \"Planner\"");
  }

  @Test
  void jiraPendingStatusIdIsAcceptedAndCanonicalized() {
    for (String status : List.of("10205", "\"10205\"", "'10205'")) {
      var task = task("project = TEST AND status = " + status + " AND assignee = currentUser()");
      var run = launches.reserveLatest(task.path("id").asText()).prepared();
      assertThat(run.payload().path("jira").path("jql").asText())
          .isEqualTo(
              "project = TEST AND status = 10205 AND assignee = currentUser() AND \"处理角色\" = \"Planner\"");
    }
  }

  @Test
  void plannerConditionIsPreservedAndCannotSelectOtherRoles() {
    String query =
        "project = TEST AND status = 10205 AND assignee = currentUser() AND \"处理角色\" = \"Planner\"";
    var task = task(query);
    var run = launches.reserveLatest(task.path("id").asText()).prepared();
    assertThat(run.payload().path("jira").path("jql").asText()).isEqualTo(query);
    for (String suffix :
        List.of(
            "\"Reviewer\"", "\"Planner\" OR project = OTHER", "\"Planner\" AND priority = High")) {
      assertThatThrownBy(
              () ->
                  task(
                      "project = TEST AND status = 10205 AND assignee = currentUser() AND \"处理角色\" = "
                          + suffix))
          .isInstanceOf(IllegalArgumentException.class);
    }
  }

  @Test
  void resourceWaitCoalescesAndBatchConsumesTriggerOnlyOnce() {
    var task = task();
    String taskId = task.path("id").asText();
    var request =
        new TaskScheduleRequest(
            "批量 Jira", task.path("sopId").asText(), taskId, "interval", null, 5, true, GROUP);
    String id = (String) schedules.save(null, request).get("id");
    try {
      jdbc.update(
          "UPDATE codex_task_schedules SET next_at=? WHERE id=?",
          Instant.now().minusSeconds(1),
          id);
      var first =
          schedules.claimDispatches(ZonedDateTime.now(ZoneId.of("Asia/Shanghai")), 100).stream()
              .filter(d -> d.taskId().equals(taskId))
              .findFirst()
              .orElseThrow();
      var capacity =
          tools.jackson.databind.node.JsonNodeFactory.instance.objectNode().put("capacity", 0);
      when(groupGateway.post(eq("/jira-capacity"), any())).thenReturn(capacity);
      assertThat(service.deferScheduledForResources(taskId, first.token())).isTrue();
      assertThat(
              schedules.list("", GROUP).stream()
                  .filter(row -> id.equals(row.get("id")))
                  .findFirst()
                  .orElseThrow()
                  .get("waitingResources"))
          .isEqualTo(true);
      var second =
          schedules.claimDispatches(ZonedDateTime.now(ZoneId.of("Asia/Shanghai")), 100).stream()
              .filter(d -> d.taskId().equals(taskId))
              .findFirst()
              .orElseThrow();
      assertThat(schedules.claimDispatches(ZonedDateTime.now(ZoneId.of("Asia/Shanghai")), 100))
          .noneMatch(d -> d.taskId().equals(taskId));
      assertThatThrownBy(() -> launches.reserveJiraBatch(taskId, first.token(), 3))
          .isInstanceOf(ConflictFailure.class);
      capacity.put("capacity", 3);
      when(groupGateway.post(eq("/workflows"), any()))
          .thenAnswer(
              call -> {
                ObjectNode payload = call.getArgument(1);
                return tools.jackson.databind.node.JsonNodeFactory.instance
                    .objectNode()
                    .put("workflowId", payload.path("workflowId").asText())
                    .put("status", "queued");
              });
      var queuedRequest = service.runScheduled(taskId, second.token());
      assertThat(queuedRequest.path("requestId").asText()).isNotBlank();
      assertThat(runs.listRuns(taskId)).isEmpty();
      requests.validate(queuedRequest.path("requestId").asText());
      jdbc.update(
          "UPDATE codex_task_schedules SET next_at=? WHERE id=?",
          Instant.now().minusSeconds(1),
          id);
      var later =
          schedules.claimDispatches(ZonedDateTime.now(ZoneId.of("Asia/Shanghai")), 100).stream()
              .filter(d -> d.taskId().equals(taskId))
              .findFirst()
              .orElseThrow();
      assertThat(service.runScheduled(taskId, later.token()).path("requestId").asText())
          .isNotEqualTo(queuedRequest.path("requestId").asText());

      assertThatThrownBy(() -> service.runScheduled(taskId, second.token()))
          .isInstanceOf(ConflictFailure.class);
    } finally {
      schedules.delete(id);
    }
  }

  @Test
  void editingScheduleInvalidatesPendingResourceRequest() {
    var task = task();
    String taskId = task.path("id").asText();
    String sopId = task.path("sopId").asText();
    String id =
        (String)
            schedules
                .save(
                    null,
                    new TaskScheduleRequest(
                        "Jira", sopId, taskId, "interval", null, 5, true, GROUP))
                .get("id");
    try {
      jdbc.update(
          "UPDATE codex_task_schedules SET next_at=? WHERE id=?",
          Instant.now().minusSeconds(1),
          id);
      var dispatch =
          schedules.claimDispatches(ZonedDateTime.now(ZoneId.of("Asia/Shanghai")), 100).stream()
              .filter(d -> d.taskId().equals(taskId))
              .findFirst()
              .orElseThrow();
      schedules.deferResources(taskId, dispatch.token());
      schedules.save(
          id, new TaskScheduleRequest("Jira", sopId, taskId, "interval", null, 5, false, GROUP));
      assertThat(schedules.claimDispatches(ZonedDateTime.now(ZoneId.of("Asia/Shanghai")), 100))
          .noneMatch(d -> d.taskId().equals(taskId));
    } finally {
      schedules.delete(id);
    }
  }

  @Test
  void poolFreezesBindingOnlyAfterCollectionAndReusesPreparedRun() {
    String taskId = task().path("id").asText();
    var request = requests.enqueue(taskId, "web", null);
    String id = request.path("requestId").asText();
    assertThat(runs.listRuns(taskId)).isEmpty();
    assertThat(requests.enqueue(taskId, "web", null).path("requestId").asText()).isEqualTo(id);
    String reservation = requests.reservation(id);
    var binding =
        tools.jackson.databind.node.JsonNodeFactory.instance
            .objectNode()
            .put("reservationId", reservation)
            .put("issueKey", "TEST-1")
            .put("title", "修复订单列表筛选异常")
            .put("server", "http://jira.test")
            .put("accountId", "bot");
    var first = requests.prepare(id, binding);
    assertThat(requests.prepare(id, binding).prepared().workflowId())
        .isEqualTo(first.prepared().workflowId());
    assertThat(first.prepared().payload().path("jiraReservation")).isEqualTo(binding);
    var issue = requests.status(id).path("jiraIssue");
    assertThat(issue.path("issueKey").asText()).isEqualTo("TEST-1");
    assertThat(issue.path("title").asText()).isEqualTo("修复订单列表筛选异常");
    assertThat(issue.size()).isEqualTo(2);
    binding.put("title", "后续修改不影响本轮");
    assertThat(
            requests
                .prepare(id, binding)
                .prepared()
                .payload()
                .path("jiraReservation")
                .path("title")
                .asText())
        .isEqualTo("修复订单列表筛选异常");
    assertThat(runs.listRuns(taskId)).hasSize(1);
    assertThat(runs.prepareRetry(first.prepared().workflowId()).payload().has("jiraReservation"))
        .isFalse();
  }
}
