package com.codexflow.configcenter.web;

import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.NotFoundFailure;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 将领域异常、参数异常和网关异常转换为稳定的 HTTP 错误响应。 */
@RestControllerAdvice
public class ApiExceptionHandler {

  private final ObjectMapper objectMapper;

  /** 注入用于构造 JSON 错误响应的 Jackson 映射器。 */
  public ApiExceptionHandler(ObjectMapper objectMapper) {
    this.objectMapper = objectMapper;
  }

  /** 将领域对象不存在异常映射为 HTTP 404。 */
  @ExceptionHandler(NotFoundFailure.class)
  public ResponseEntity<ObjectNode> notFound(NotFoundFailure error) {
    return body(HttpStatus.NOT_FOUND, error.getMessage());
  }

  /** 将业务冲突和乐观锁冲突映射为 HTTP 409。 */
  @ExceptionHandler({ConflictFailure.class, OptimisticLockingFailureException.class})
  public ResponseEntity<ObjectNode> conflict(RuntimeException error) {
    return body(HttpStatus.CONFLICT, error.getMessage());
  }

  /** 将业务参数检查异常映射为 HTTP 400。 */
  @ExceptionHandler(IllegalArgumentException.class)
  public ResponseEntity<ObjectNode> badRequest(IllegalArgumentException error) {
    return body(HttpStatus.BAD_REQUEST, error.getMessage());
  }

  /** 将 Bean Validation 字段错误映射为带首个字段说明的 HTTP 400。 */
  @ExceptionHandler(MethodArgumentNotValidException.class)
  public ResponseEntity<ObjectNode> validation(MethodArgumentNotValidException error) {
    String message =
        error.getBindingResult().getFieldErrors().stream()
            .findFirst()
            .map(fieldError -> fieldError.getField() + " " + fieldError.getDefaultMessage())
            .orElse("请求参数校验失败。");
    return body(HttpStatus.BAD_REQUEST, message);
  }

  /** 将工作流网关调用异常映射为 HTTP 502。 */
  @ExceptionHandler(GatewayFailure.class)
  public ResponseEntity<ObjectNode> gateway(GatewayFailure error) {
    return body(HttpStatus.BAD_GATEWAY, "网关调用失败：" + error.getMessage());
  }

  /** 按统一的 {@code error} 字段构造 JSON 错误响应。 */
  private ResponseEntity<ObjectNode> body(HttpStatus status, String message) {
    ObjectNode body = objectMapper.createObjectNode();
    body.put("error", message == null ? status.getReasonPhrase() : message);
    return ResponseEntity.status(status).body(body);
  }
}
