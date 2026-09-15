package com.codexflow.configcenter.web;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.domain.GroupService;
import java.util.List;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;

@RestController
@RequestMapping("/api/groups")
public class GroupController {
  private final GroupService groups;
  private final ConfigService configs;

  public GroupController(GroupService groups, ConfigService configs) {
    this.groups = groups;
    this.configs = configs;
  }

  @GetMapping
  public JsonNode list() {
    return groups.catalog();
  }

  @PostMapping
  public JsonNode create(@RequestBody JsonNode body) {
    return groups.save(null, body);
  }

  @PutMapping("/{id}")
  public JsonNode rename(@PathVariable String id, @RequestBody JsonNode body) {
    return groups.save(id, body);
  }

  @DeleteMapping("/{id}")
  public JsonNode delete(@PathVariable String id) {
    return groups.delete(id);
  }

  public record Assignment(String kind, List<String> ids, String groupId) {}

  @PostMapping("/assign")
  public void assign(@RequestBody Assignment body) {
    configs.assignGroups(body.kind(), body.ids(), body.groupId());
  }
}
