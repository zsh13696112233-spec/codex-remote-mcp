package com.codexflow.configcenter.web;

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

  public TaskScheduleController(TaskScheduleStore schedules) {
    this.schedules = schedules;
  }

  @GetMapping
  public List<Map<String, Object>> list(
      @RequestParam(defaultValue = "") String q, @RequestParam(defaultValue = "") String groupId) {
    return schedules.list(q, groupId);
  }

  @PostMapping
  @ResponseStatus(HttpStatus.CREATED)
  public Map<String, Object> create(@Valid @RequestBody TaskScheduleRequest body) {
    return schedules.save(null, body);
  }

  @PutMapping("/{id}")
  public Map<String, Object> update(
      @PathVariable String id, @Valid @RequestBody TaskScheduleRequest body) {
    return schedules.save(id, body);
  }

  @DeleteMapping("/{id}")
  @ResponseStatus(HttpStatus.NO_CONTENT)
  public void delete(@PathVariable String id) {
    schedules.delete(id);
  }
}
