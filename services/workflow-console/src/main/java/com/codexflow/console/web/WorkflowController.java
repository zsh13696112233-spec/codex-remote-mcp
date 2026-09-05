package com.codexflow.console.web;

import com.codexflow.console.client.GatewayClient;
import java.nio.charset.StandardCharsets;
import org.springframework.http.CacheControl;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
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
  public JsonNode status(
      @PathVariable String workflowId,
      @RequestParam(required = false) String knownRevision,
      @RequestParam(required = false) String knownResults) {
    if (knownRevision != null && knownRevision.length() > 128) {
      throw new IllegalArgumentException("查询版本过长。");
    }
    if (knownResults != null && knownResults.length() > 2400) {
      throw new IllegalArgumentException("步骤结果版本过长。");
    }
    return gatewayClient.status(workflowId, knownRevision, knownResults);
  }

  /** 按游标增量查询指定工作流事件，并校验分页参数。 */
  @GetMapping("/workflows/{workflowId}/events")
  public JsonNode events(
      @PathVariable String workflowId,
      @RequestParam(defaultValue = "0") long after,
      @RequestParam(defaultValue = "200") int limit,
      @RequestParam(defaultValue = "all") String view,
      @RequestParam(required = false) Long before,
      @RequestParam(defaultValue = "false") boolean tail) {
    if (after < 0) {
      throw new IllegalArgumentException("after 不能小于 0。");
    }
    if (limit < 1 || limit > 1000) {
      throw new IllegalArgumentException("limit 必须在 1 到 1000 之间。");
    }
    if (!java.util.Set.of("all", "monitor").contains(view)
        || (before != null && before <= 0)
        || ((before != null || tail) && after != 0)) {
      throw new IllegalArgumentException("事件查询参数无效。");
    }
    return gatewayClient.events(workflowId, after, limit, view, before, tail);
  }

  /** 代理读取工作流文件，浏览器不直接访问 Python 网关或执行机路径。 */
  @GetMapping("/workflows/{workflowId}/artifacts/{artifactId}")
  public ResponseEntity<byte[]> artifact(
      @PathVariable String workflowId, @PathVariable String artifactId) {
    GatewayClient.BinaryResponse artifact = gatewayClient.artifact(workflowId, artifactId);
    boolean image = artifact.contentType().toLowerCase().startsWith("image/");
    ContentDisposition disposition =
        (image ? ContentDisposition.inline() : ContentDisposition.attachment())
            .filename(artifact.filename(), StandardCharsets.UTF_8)
            .build();
    return ResponseEntity.ok()
        .contentType(MediaType.parseMediaType(artifact.contentType()))
        .cacheControl(
            CacheControl.maxAge(java.time.Duration.ofDays(365)).cachePrivate().immutable())
        .header("X-Content-Type-Options", "nosniff")
        .header(HttpHeaders.CONTENT_DISPOSITION, disposition.toString())
        .body(artifact.body());
  }

  /** 向指定工作流主监督会话发送消息并返回 HTTP 202。 */
  @PostMapping("/workflows/{workflowId}/messages")
  public ResponseEntity<JsonNode> sendMessage(
      @PathVariable String workflowId, @RequestBody JsonNode body) {
    return ResponseEntity.accepted().body(gatewayClient.sendMessage(workflowId, body));
  }

  /** 确认当前半自动等待并立即进入下一步骤。 */
  @PostMapping("/workflows/{workflowId}/advance/{gateId}/confirm")
  public JsonNode confirmAdvance(@PathVariable String workflowId, @PathVariable String gateId) {
    return gatewayClient.confirmAdvance(workflowId, gateId);
  }

  /** 暂停当前半自动等待，不再按倒计时自动进入下一步骤。 */
  @PostMapping("/workflows/{workflowId}/advance/{gateId}/hold")
  public JsonNode holdAdvance(@PathVariable String workflowId, @PathVariable String gateId) {
    return gatewayClient.holdAdvance(workflowId, gateId);
  }
}
