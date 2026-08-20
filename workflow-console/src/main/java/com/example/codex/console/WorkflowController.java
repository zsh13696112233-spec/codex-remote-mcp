package com.example.codex.console;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@RestController
@RequestMapping("/api")
public class WorkflowController {

    private final GatewayClient gatewayClient;
    private final ObjectMapper objectMapper;

    public WorkflowController(GatewayClient gatewayClient, ObjectMapper objectMapper) {
        this.gatewayClient = gatewayClient;
        this.objectMapper = objectMapper;
    }

    @GetMapping("/gateway/ready")
    public JsonNode ready() {
        return gatewayClient.ready();
    }

    @GetMapping("/workflows/{workflowId}")
    public JsonNode status(@PathVariable String workflowId) {
        return gatewayClient.status(workflowId);
    }

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

    @PostMapping("/workflows/{workflowId}/messages")
    public ResponseEntity<JsonNode> sendMessage(
            @PathVariable String workflowId,
            @RequestBody JsonNode body) {
        return ResponseEntity.accepted().body(gatewayClient.sendMessage(workflowId, body));
    }

    @ExceptionHandler(GatewayException.class)
    public ResponseEntity<JsonNode> gatewayError(GatewayException error) {
        int upstream = error.getStatusCode();
        org.springframework.http.HttpStatus status = upstream >= 400 && upstream < 500
                ? org.springframework.http.HttpStatus.valueOf(upstream)
                : org.springframework.http.HttpStatus.BAD_GATEWAY;
        ObjectNode body = objectMapper.createObjectNode();
        body.put("error", error.getMessage());
        body.put("upstreamStatus", upstream);
        body.put("upstreamBody", error.getResponseBody());
        return ResponseEntity.status(status).body(body);
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<JsonNode> badRequest(IllegalArgumentException error) {
        ObjectNode body = objectMapper.createObjectNode();
        body.put("error", error.getMessage());
        return ResponseEntity.badRequest().body(body);
    }
}
