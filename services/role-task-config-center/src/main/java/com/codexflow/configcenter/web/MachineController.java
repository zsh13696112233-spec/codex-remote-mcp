package com.codexflow.configcenter.web;

import com.codexflow.configcenter.application.MachineService;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;

/** 内网机器分组管理入口。 */
@RestController
@RequestMapping("/api")
public class MachineController {
  private final MachineService service;

  public MachineController(MachineService service) {
    this.service = service;
  }

  @GetMapping("/agent-groups")
  public JsonNode groups() {
    return service.groups();
  }

  @PostMapping("/agent-groups")
  public JsonNode createGroup(@RequestBody JsonNode body) {
    return service.createGroup(body);
  }

  @PutMapping("/agent-groups/{id}")
  public JsonNode updateGroup(@PathVariable String id, @RequestBody JsonNode body) {
    return service.updateGroup(id, body);
  }

  @DeleteMapping("/agent-groups/{id}")
  public JsonNode deleteGroup(@PathVariable String id) {
    return service.deleteGroup(id);
  }

  @PostMapping("/agents")
  public JsonNode createMachine(@RequestBody JsonNode body) {
    return service.createMachine(body);
  }

  @PutMapping("/agents/{id}")
  public JsonNode updateMachine(@PathVariable String id, @RequestBody JsonNode body) {
    return service.updateMachine(id, body);
  }

  @PostMapping("/agents/{id}/test")
  public JsonNode testMachine(@PathVariable String id) {
    return service.testMachine(id);
  }
}
