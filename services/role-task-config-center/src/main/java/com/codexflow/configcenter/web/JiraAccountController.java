package com.codexflow.configcenter.web;

import com.codexflow.configcenter.domain.JiraAccountService;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;

@RestController
@RequestMapping("/api/jira-accounts")
public class JiraAccountController {
  private final JiraAccountService accounts;

  public JiraAccountController(JiraAccountService accounts) {
    this.accounts = accounts;
  }

  @GetMapping
  public JsonNode list(@RequestParam(required = false) String groupId) {
    return accounts.list(groupId);
  }

  @PostMapping
  public JsonNode create(@RequestBody JsonNode body) {
    return accounts.save(null, body);
  }

  @PutMapping("/{id}")
  public JsonNode update(@PathVariable String id, @RequestBody JsonNode body) {
    return accounts.save(id, body);
  }

  @PostMapping("/{id}/test")
  public JsonNode test(@PathVariable String id, @RequestBody JsonNode body) {
    return accounts.test(id, body);
  }

  @DeleteMapping("/{id}")
  public JsonNode delete(@PathVariable String id, @RequestParam long version) {
    return accounts.delete(id, version);
  }
}
