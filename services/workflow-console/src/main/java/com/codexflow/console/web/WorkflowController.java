package com.codexflow.console.web;

import com.codexflow.console.client.GatewayClient;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

/** 提供工作流状态、事件历史和主监督对话的只读监控 API。 */
@RestController
@RequestMapping("/api")
public class WorkflowController {

  private final GatewayClient gatewayClient;

  /** 注入工作流网关客户端。 */
  public WorkflowController(GatewayClient gatewayClient) {
    this.gatewayClient = gatewayClient;
  }

  /** 代理查询工作流网关就绪状态。 */
  @GetMapping("/gateway/ready")
  public JsonNode ready() {
    return gatewayClient.ready();
  }

  /** 查询指定工作流的当前状态。 */
  @GetMapping("/workflows/{workflowId}")
  public JsonNode status(@PathVariable String workflowId) {
    return gatewayClient.status(workflowId);
  }

  /** 按游标增量查询指定工作流事件，并校验分页参数。 */
  @GetMapping("/workflows/{workflowId}/events")
  public JsonNode events(
      @PathVariable String workflowId,
      @RequestParam(defaultValue = "0") long after,
      @RequestParam(defaultValue = "200") int limit) {
    if (after < 0) {
      throw new IllegalArgumentException("after 不能小于 0。");
    }
    if (limit < 1 || limit > 1000) {
      throw new IllegalArgumentException("limit 必须在 1 到 1000 之间。");
    }
    return gatewayClient.events(workflowId, after, limit);
  }

  /** 向指定工作流主监督会话发送消息并返回 HTTP 202。 */
  @PostMapping("/workflows/{workflowId}/messages")
  public ResponseEntity<JsonNode> sendMessage(
      @PathVariable String workflowId, @RequestBody JsonNode body) {
    return ResponseEntity.accepted().body(gatewayClient.sendMessage(workflowId, body));
  }
}
