package com.codexflow.configcenter.application;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.RunCatalogStore;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

@Service
public class RunCatalogService {
  private static final Logger LOGGER = LoggerFactory.getLogger(RunCatalogService.class);
  private static final Set<String> LIVE =
      Set.of("queued", "running", "cancelling", "completed", "failed", "cancelled");
  private final RunCatalogStore catalog;
  private final WorkflowRunStore runs;
  private final GatewayClient gateway;

  public RunCatalogService(RunCatalogStore catalog, WorkflowRunStore runs, GatewayClient gateway) {
    this.catalog = catalog;
    this.runs = runs;
    this.gateway = gateway;
  }

  public ObjectNode list(
      String name, String type, String startDate, String endDate, int page, int size) {
    return list(name, type, startDate, endDate, page, size, "");
  }

  public ObjectNode list(
      String name,
      String type,
      String startDate,
      String endDate,
      int page,
      int size,
      String groupId) {
    ObjectNode result = catalog.list(name, type, startDate, endDate, page, size, groupId);
    result.put("statusFresh", true);
    if (result.path("items").isEmpty()) return result;
    ObjectNode request = result.objectNode();
    var ids = request.putArray("workflowIds");
    result.path("items").forEach(row -> ids.add(row.path("workflowId").asText()));
    try {
      JsonNode statuses = gateway.post("/workflow-statuses", request).path("statuses");
      if (!statuses.isObject()) throw new IllegalStateException("状态响应格式无效");
      ObjectNode accepted = result.objectNode();
      for (JsonNode row : result.path("items")) {
        String id = row.path("workflowId").asText();
        JsonNode status = statuses.path(id);
        if (status.isTextual() && LIVE.contains(status.asText())) {
          accepted.put(id, status.asText());
        } else if (!"submit_failed".equals(row.path("status").asText())) {
          result.put("statusFresh", false);
        }
      }
      runs.recordSummaryStatuses(accepted);
      result
          .path("items")
          .forEach(
              row -> {
                JsonNode status = accepted.get(row.path("workflowId").asText());
                if (status != null) ((ObjectNode) row).put("status", status.asText());
              });
    } catch (RuntimeException error) {
      result.put("statusFresh", false);
      LOGGER.warn("运行列表状态刷新失败，保留最近已知状态，异常类型：{}", error.getClass().getSimpleName());
    }
    return result;
  }
}
