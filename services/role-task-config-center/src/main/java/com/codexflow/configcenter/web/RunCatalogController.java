package com.codexflow.configcenter.web;

import com.codexflow.configcenter.application.RunCatalogService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

@RestController
public class RunCatalogController {
  private final RunCatalogService service;

  public RunCatalogController(RunCatalogService service) {
    this.service = service;
  }

  @GetMapping("/api/task-runs")
  public ObjectNode list(
      @RequestParam(defaultValue = "") String name,
      @RequestParam(defaultValue = "") String type,
      @RequestParam(defaultValue = "") String startDate,
      @RequestParam(defaultValue = "") String endDate,
      @RequestParam(defaultValue = "0") int page,
      @RequestParam(defaultValue = "20") int size,
      @RequestParam(defaultValue = "") String groupId) {
    return service.list(name, type, startDate, endDate, page, size, groupId);
  }
}
