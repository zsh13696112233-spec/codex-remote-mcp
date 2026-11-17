package com.codexflow.configcenter.web;

import com.codexflow.configcenter.integration.dingtalk.JiraEmptyReminderService;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;

@RestController
@RequestMapping("/api/jira-accounts/{id}/empty-reminder")
public class JiraEmptyReminderController {
  private final JiraEmptyReminderService service;

  public JiraEmptyReminderController(JiraEmptyReminderService service) {
    this.service = service;
  }

  @GetMapping
  public JsonNode get(@PathVariable String id) {
    return service.get(id);
  }

  @PutMapping
  public JsonNode save(@PathVariable String id, @RequestBody JsonNode body) {
    return service.save(id, body);
  }
}
