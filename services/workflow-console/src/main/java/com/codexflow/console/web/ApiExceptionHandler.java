package com.codexflow.console.web;

import com.codexflow.console.client.GatewayException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 将参数异常和上游网关异常转换为监控中心统一错误响应。 */
@RestControllerAdvice
public class ApiExceptionHandler {

  private final ObjectMapper objectMapper;

  /** 注入用于构造 JSON 错误响应的 Jackson 映射器。 */
  public ApiExceptionHandler(ObjectMapper objectMapper) {
    this.objectMapper = objectMapper;
  }

  /** 根据上游状态码将网关异常映射为客户端错误或 HTTP 502。 */
  @ExceptionHandler(GatewayException.class)
  public ResponseEntity<JsonNode> gatewayError(GatewayException error) {
    int upstreamStatus = error.getStatusCode();
    HttpStatus responseStatus = resolveStatus(upstreamStatus);
    ObjectNode body = objectMapper.createObjectNode();
    body.put("error", error.getMessage());
    body.put("upstreamStatus", upstreamStatus);
    body.put("upstreamBody", error.getResponseBody());
    return ResponseEntity.status(responseStatus).body(body);
  }

  /** 将接口参数检查异常映射为 HTTP 400。 */
  @ExceptionHandler(IllegalArgumentException.class)
  public ResponseEntity<JsonNode> badRequest(IllegalArgumentException error) {
    ObjectNode body = objectMapper.createObjectNode();
    body.put("error", error.getMessage());
    return ResponseEntity.badRequest().body(body);
  }

  /** 仅透传标准 4xx 状态，其余上游状态统一转换为 HTTP 502。 */
  private static HttpStatus resolveStatus(int upstreamStatus) {
    if (upstreamStatus < 400 || upstreamStatus >= 500) return HttpStatus.BAD_GATEWAY;
    HttpStatus resolved = HttpStatus.resolve(upstreamStatus);
    return resolved == null ? HttpStatus.BAD_GATEWAY : resolved;
  }
}
