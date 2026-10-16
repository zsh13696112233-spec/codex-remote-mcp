package com.codexflow.configcenter.web;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.TaskScheduleStore;
import com.codexflow.configcenter.dto.TaskScheduleRequest;
import jakarta.validation.Valid;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/task-schedules")
public class TaskScheduleController {
  private final TaskScheduleStore schedules;
  private final GatewayClient gateway;

  public TaskScheduleController(TaskScheduleStore schedules, GatewayClient gateway) {
    this.schedules = schedules;
    this.gateway = gateway;
  }

  @GetMapping
  public List<Map<String, Object>> list(
      @RequestParam(defaultValue = "") String q, @RequestParam(defaultValue = "") String groupId) {
    return schedules.list(q, groupId);
  }

  @PostMapping
  @ResponseStatus(HttpStatus.CREATED)
  public Map<String, Object> create(@Valid @RequestBody TaskScheduleRequest body) {
    validateCheck(body);
    return schedules.save(null, body);
  }

  @PutMapping("/{id}")
  public Map<String, Object> update(
      @PathVariable String id, @Valid @RequestBody TaskScheduleRequest body) {
    validateCheck(body);
    return schedules.save(id, body);
  }

  private void validateCheck(TaskScheduleRequest body) {
    if (!Boolean.TRUE.equals(body.enabled())
        || body.preCheck() == null
        || !body.preCheck().enabled()) return;
    var check = body.preCheck().normalized();
    for (var agent : gateway.get("/agents").path("agents")) {
      if (!check.agentId().equals(agent.path("agentId").asText())
          || !body.groupId().equals(agent.path("groupId").asText())) continue;
      for (var capability : agent.path("capabilities")) {
        if ("executor".equals(capability.asText())) return;
      }
    }
    throw new IllegalArgumentException("请选择同组已登记且具备执行能力的检查执行机。");
  }

  @DeleteMapping("/{id}")
  @ResponseStatus(HttpStatus.NO_CONTENT)
  public void delete(@PathVariable String id) {
    schedules.delete(id);
  }
}
