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
  // 测试显式触发服务；禁止真实定时任务跨用例访问已重置的网关模拟对象。
  @org.springframework.test.context.bean.override.mockito.MockitoBean(name = "taskScheduler")
  org.springframework.scheduling.TaskScheduler taskScheduler;

  @Autowired JiraRequestStore requests;
  @Autowired JiraCollectionService collections;
  @Autowired ConfigService config;
  @Autowired WorkflowRunStore runs;
  @Autowired TaskLaunchStore launches;
  @Autowired TaskScheduleStore schedules;
  @Autowired JdbcTemplate jdbc;
  @Autowired com.codexflow.configcenter.application.WorkflowRunService service;

  @Test
  void independentCollectionIncludesEnabledTasksWithoutScheduleAndCreatesNoLaunch() {
    var task = task();
    var taskId = task.path("id").asText();
    var before = jdbc.queryForObject("SELECT COUNT(*) FROM codex_jira_requests", Integer.class);
    var captured =
        new java.util.concurrent.atomic.AtomicReference<tools.jackson.databind.JsonNode>();
    when(groupGateway.post(eq("/jira-polling"), any()))
        .thenAnswer(
            call -> {
              captured.set(call.getArgument(1));
              return task.objectNode();
            });
    collections.synchronize();
    var collected =
        captured
            .get()
            .path("tasks")
            .valueStream()
            .filter(row -> row.path("taskDefinitionId").asText().equals(taskId))
            .findFirst()
            .orElseThrow();
    assertThat(collected.path("sopId").asText()).isEqualTo(task.path("sopId").asText());
    assertThat(collected.path("sopName").asText()).isEqualTo(task.path("sopName").asText());
    assertThat(
            captured
                .get()
                .path("tasks")
                .valueStream()
                .anyMatch(row -> row.path("taskDefinitionId").asText().equals(taskId)))
        .isTrue();
    assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM codex_jira_requests", Integer.class))
        .isEqualTo(before);
    jdbc.update("UPDATE codex_sop_task_definitions SET enabled=FALSE WHERE id=?", taskId);
    collections.synchronize();
    assertThat(
            captured
                .get()
                .path("tasks")
                .valueStream()
                .anyMatch(row -> row.path("taskDefinitionId").asText().equals(taskId)))
        .isFalse();
  }

  private ObjectNode task() {
    return task("project = TEST AND status = 待办 AND assignee = currentUser()");
  }

  private ObjectNode task(String query) {
    org.mockito.Mockito.doReturn(
            tools.jackson.databind.node.JsonNodeFactory.instance
                .objectNode()
                .put("id", GROUP)
                .put("groupId", GROUP)
                .put("enabled", true)
                .put("testStatus", "passed")
                .put("executorAlias", "bot"))
        .when(groupGateway)
        .get("/jira-accounts/" + GROUP);
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
                new JiraDispatchRequest(query, "bot", GROUP)));
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
    jdbc.update("UPDATE codex_sop_sops SET name='改名后的流程' WHERE id=?", task.path("sopId").asText());
    var frozen = runs.frozenSopContexts(List.of(first.workflowId())).path(first.workflowId());
    assertThat(frozen.path("sopName").asText()).isEqualTo(task.path("sopName").asText());
    assertThat(frozen.path("sopId").asText()).isEqualTo(task.path("sopId").asText());
    var retry = runs.prepareRetry(first.workflowId());
    assertThat(first.payload().path("jiraDeliveryVersion").asInt()).isEqualTo(3);
    assertThat(retry.payload().path("jiraDeliveryVersion").asInt()).isEqualTo(3);
    assertThat(retry.payload().path("jira").path("jql").asText())
        .isEqualTo(
            "project = TEST AND status = 10205 AND assignee = currentUser() AND \"处理角色\" = \"Planner\"");
    assertThat(retry.payload().path("jira").path("connectionId").asText()).isEqualTo(GROUP);
    assertThat(retry.payload().path("jiraRetryOf").asText()).isEqualTo(first.workflowId());
    assertThat(first.payload().has("supervisorAgentId")).isFalse();
    assertThat(first.payload().path("nodes").get(0).has("cwd")).isFalse();
  }

  @Test
  void metadataSyncBeforeSubmissionMustNotMarkSopAsRegistered() {
    var task = task();
    var prepared = launches.reserveLatest(task.path("id").asText()).prepared();
    var legacyPayload = prepared.payload().deepCopy();
    legacyPayload.remove("sopId");
    legacyPayload.remove("sopName");
    jdbc.update(
        "UPDATE codex_sop_task_runs SET submitted_json=? WHERE workflow_id=?",
        legacyPayload.toString(),
        prepared.workflowId());
    var frozen = runs.getPrepared(prepared.workflowId()).payload().deepCopy();
    var snapshot =
        jdbc.queryForObject(
            "SELECT snapshot_json FROM codex_sop_task_runs WHERE workflow_id=?",
            String.class,
            prepared.workflowId());
    when(groupGateway.post(eq("/workflow-task-bindings"), any()))
        .thenAnswer(
            call -> {
              // 网关跳过尚不存在的运行后，提交恰好完成，再写入登记标记。
              runs.recordGatewayStatus(
                  prepared.workflowId(), task.objectNode().put("status", "queued"), "queued");
              var response = task.objectNode().put("registered", true);
              response.putArray("sopRegisteredWorkflowIds");
              return response;
            });
    service.synchronizeRuntimeScopes();
    assertThat(
            jdbc.queryForObject(
                "SELECT runtime_sop_registered FROM codex_sop_task_runs WHERE workflow_id=?",
                Boolean.class,
                prepared.workflowId()))
        .isFalse();
    assertThat(runs.pendingRuntimeScopes(task.path("id").asText())).contains(prepared.workflowId());
    var confirmed = task.objectNode().put("registered", true);
    confirmed.putArray("sopRegisteredWorkflowIds").add(prepared.workflowId());
    when(groupGateway.post(eq("/workflow-task-bindings"), any())).thenReturn(confirmed);
    service.synchronizeRuntimeScopes();
    assertThat(
            jdbc.queryForObject(
                "SELECT runtime_sop_registered FROM codex_sop_task_runs WHERE workflow_id=?",
                Boolean.class,
                prepared.workflowId()))
        .isTrue();
    assertThat(runs.pendingRuntimeScopes(task.path("id").asText()))
        .doesNotContain(prepared.workflowId());
    assertThat(runs.getPrepared(prepared.workflowId()).payload()).isEqualTo(frozen);
    assertThat(
            jdbc.queryForObject(
                "SELECT snapshot_json FROM codex_sop_task_runs WHERE workflow_id=?",
                String.class,
                prepared.workflowId()))
        .isEqualTo(snapshot);
  }

  @Test
  void jiraQueryCannotBypassStatusAndCurrentAssignee() {
    for (String query :
        List.of(
            "project = TEST",
            "project = TEST AND status = 10205",
            "project = TEST AND status IN () AND assignee = currentUser()",
            "project = TEST AND status = 待办 AND assignee = currentUser() OR project = OTHER",
            "project = TEST AND status = 待办 AND assignee = other",
            "project = TEST AND status = 待办 AND assignee = \"currentUser()\"",
            "project = TEST AND status = 待办 AND assignee = currentUser() AND priority = High")) {
      assertThatThrownBy(() -> task(query))
          .isInstanceOf(IllegalArgumentException.class)
          .hasMessageContaining("只能指定项目和状态");
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
  void multipleJiraStatusesAndRolesArePreservedInFrozenRetries() {
    for (String statuses :
        List.of("IN (\"Open\", \"重新打开\")", "IN (1, 4)", "IN (\"In Progress\", \"Closed\")")) {
      String query =
          "project = TEST AND status "
              + statuses
              + " AND assignee = currentUser() AND \"处理角色\" IN (\"Planner\", \"Reviewer\") AND issuetype = \"sub-bug\"";
      var definition = task(query);
      var run = launches.reserveLatest(definition.path("id").asText()).prepared();
      assertThat(run.payload().path("jira").path("jql").asText()).isEqualTo(query);
      assertThat(runs.prepareRetry(run.workflowId()).payload().path("jira").path("jql").asText())
          .isEqualTo(query);
    }
    for (String statuses :
        List.of("IN ()", "IN (1,)", "IN (1, \" \")", "IN (1,4) OR project = OTHER")) {
      assertThatThrownBy(
              () -> task("project = TEST AND status " + statuses + " AND assignee = currentUser()"))
          .isInstanceOf(IllegalArgumentException.class);
    }
    var definition =
        task("project = TEST AND status IN (待办, 10205, Closed) AND assignee = currentUser()");
    assertThat(
            launches
                .reserveLatest(definition.path("id").asText())
                .prepared()
                .payload()
                .path("jira")
                .path("jql")
                .asText())
        .contains("status IN (10205, Closed)");
  }

  @Test
  void jiraRolesAndOptionalIssueTypeAreFrozenInRuns() {
    String base = "project = TEST AND status = Open AND assignee = currentUser()";
    for (String role : List.of("= \"Reviewer\"", "IN (\"Planner\", \"Reviewer\")")) {
      for (String type :
          List.of(
              "",
              " AND issuetype = \"sub-bug\"",
              " AND issuetype = \"Acceptance-bug\"",
              " AND issuetype IN (\"6\", \"7\")")) {
        String query = base + " AND \"处理角色\" " + role + type;
        var definition = task(query);
        var run = launches.reserveLatest(definition.path("id").asText()).prepared();
        assertThat(run.payload().path("jira").path("jql").asText()).isEqualTo(query);
        assertThat(runs.prepareRetry(run.workflowId()).payload().path("jira").path("jql").asText())
            .isEqualTo(query);
      }
    }
    for (String suffix :
        List.of(
            " AND \"处理角色\" IN ()",
            " AND \"处理角色\" = \" \"",
            " AND \"处理角色\" IN (\"Planner\", \" \")",
            " AND issuetype = \" \"",
            " AND issuetype IN ()",
            " AND issuetype IN (\"6\", \" \")",
            " AND \"处理角色\" = \"Reviewer\" OR project = OTHER",
            " AND issuetype = \"sub-bug\" OR project = OTHER")) {
      assertThatThrownBy(() -> task(base + suffix)).isInstanceOf(IllegalArgumentException.class);
    }
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
  void plannerConditionIsPreservedAndCannotInjectOtherClauses() {
    String query =
        "project = TEST AND status = 10205 AND assignee = currentUser() AND \"处理角色\" = \"Planner\"";
    var task = task(query);
    var run = launches.reserveLatest(task.path("id").asText()).prepared();
    assertThat(run.payload().path("jira").path("jql").asText()).isEqualTo(query);
    for (String suffix :
        List.of("\"Planner\" OR project = OTHER", "\"Planner\" AND priority = High")) {
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
  void legacyScheduledRequestWaitingForCheckCanPrepareWithoutChangingSnapshot() {
    var task = task();
    String taskId = task.path("id").asText();
    String rule =
        (String)
            schedules
                .save(
                    null,
                    new TaskScheduleRequest(
                        "历史检查",
                        task.path("sopId").asText(),
                        taskId,
                        "interval",
                        null,
                        5,
                        true,
                        GROUP))
                .get("id");
    try {
      jdbc.update(
          "UPDATE codex_task_schedules SET check_enabled=TRUE,check_agent_id='busy-machine',next_at=? WHERE id=?",
          Instant.now().minusSeconds(1),
          rule);
      var dispatch =
          schedules.claimDispatches(ZonedDateTime.now(ZoneId.of("Asia/Shanghai")), 100).stream()
              .filter(d -> d.taskId().equals(taskId))
              .findFirst()
              .orElseThrow();
      String id = requests.enqueue(taskId, "schedule", dispatch.token()).path("requestId").asText();
      jdbc.update(
          "UPDATE codex_jira_requests SET state='waiting',check_state='pending',reason='检查执行机忙。' WHERE id=?",
          id);
      String snapshot =
          jdbc.queryForObject(
              "SELECT schedule_json FROM codex_jira_requests WHERE id=?", String.class, id);
      requests.validate(id);
      String reservation = requests.reservation(id);
      var binding =
          tools.jackson.databind.node.JsonNodeFactory.instance
              .objectNode()
              .put("reservationId", reservation)
              .put("issueKey", "TEST-1")
              .put("server", "http://jira.test")
              .put("accountId", "bot");
      var prepared = requests.prepare(id, binding);
      assertThat(prepared.prepared().payload().path("jiraReservation").path("issueKey").asText())
          .isEqualTo("TEST-1");
      assertThat(
              jdbc.queryForObject(
                  "SELECT schedule_json FROM codex_jira_requests WHERE id=?", String.class, id))
          .isEqualTo(snapshot);
      assertThat(runs.listRuns(taskId)).hasSize(1);
    } finally {
      schedules.delete(rule);
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
    assertThat(runs.prepareRetry(first.prepared().workflowId()).payload().path("jiraReservation"))
        .isEqualTo(first.prepared().payload().path("jiraReservation"));
  }
}
