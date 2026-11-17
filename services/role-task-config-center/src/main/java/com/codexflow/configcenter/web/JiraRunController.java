package com.codexflow.configcenter.web;

import com.codexflow.configcenter.application.JiraPagination;
import com.codexflow.configcenter.application.WorkflowRunService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

/** 各组 Jira 领取、分配和交付台账，只经配置中心访问网关。 */
@RestController
public class JiraRunController {
  @org.springframework.beans.factory.annotation.Autowired
  private com.codexflow.configcenter.application.JiraPoolService pool;

  private final WorkflowRunService service;

  public JiraRunController(WorkflowRunService service) {
    this.service = service;
  }

  @GetMapping("/api/jira-requests/{id}")
  public JsonNode request(@org.springframework.web.bind.annotation.PathVariable String id) {
    java.util.UUID.fromString(id);
    return pool.status(id);
  }

  @GetMapping({"/api/jira-statistics", "/api/jira-statistics/items"})
  public JsonNode statistics(
      @RequestParam(defaultValue = "") String groupId,
      @RequestParam(defaultValue = "") String project,
      @RequestParam(defaultValue = "") String sopId,
      @RequestParam(defaultValue = "") String category,
      @RequestParam(defaultValue = "") String developer,
      @RequestParam(required = false) Long before,
      @RequestParam(required = false) Integer page,
      @RequestParam(defaultValue = "10") int pageSize,
      jakarta.servlet.http.HttpServletRequest request) {
    if (!groupId.isEmpty()) java.util.UUID.fromString(groupId);
    if (before != null && before < 1) throw new IllegalArgumentException("分页参数无效。");
    return pool.statistics(
        groupId,
        project,
        category,
        developer,
        before,
        request.getRequestURI().endsWith("/items"),
        sopId,
        new JiraPagination(page, pageSize));
  }

  @GetMapping("/api/jira-pool")
  public JsonNode pool(
      @RequestParam(defaultValue = "") String groupId,
      @RequestParam(required = false) Long before,
      @RequestParam(defaultValue = "") String project,
      @RequestParam(defaultValue = "") String sopId,
      @RequestParam(defaultValue = "") String task,
      @RequestParam(defaultValue = "") String state,
      @RequestParam(required = false) Integer page,
      @RequestParam(defaultValue = "10") int pageSize,
      @RequestParam(required = false) Integer collectionPage,
      @RequestParam(defaultValue = "10") int collectionPageSize) {
    if (!groupId.isEmpty()) java.util.UUID.fromString(groupId);
    if (before != null && before < 1) throw new IllegalArgumentException("分页参数无效。");
    return pool.board(
        groupId,
        before,
        project,
        task,
        state,
        sopId,
        new JiraPagination(page, pageSize),
        new JiraPagination(collectionPage, collectionPageSize));
  }

  @GetMapping("/api/jira-runs/{workflowId}")
  public JsonNode detail(@org.springframework.web.bind.annotation.PathVariable String workflowId) {
    return service.jiraRunDetail(workflowId);
  }

  @GetMapping("/api/jira-runs")
  public JsonNode list(
      @RequestParam(defaultValue = "") String groupId,
      @RequestParam(required = false) Long before,
      @RequestParam(defaultValue = "") String sopId,
      @RequestParam(required = false) Integer page,
      @RequestParam(defaultValue = "10") int pageSize) {
    return service.jiraRuns(groupId, before, sopId, new JiraPagination(page, pageSize));
  }
}
