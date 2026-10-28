package com.codexflow.configcenter.web;

import com.codexflow.configcenter.application.WorkflowRunService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

/** 各组 Jira 领取、分配和交付台账，只经配置中心访问网关。 */
@RestController
public class JiraRunController {
  private final WorkflowRunService service;

  public JiraRunController(WorkflowRunService service) {
    this.service = service;
  }

  @GetMapping("/api/jira-runs")
  public JsonNode list(
      @RequestParam(defaultValue = "") String groupId,
      @RequestParam(required = false) Long before) {
    return service.jiraRuns(groupId, before);
  }
}
