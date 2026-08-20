package com.example.codex.config;

import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;
import org.springframework.http.*;
import org.springframework.web.bind.annotation.*;

@RestControllerAdvice
class ApiExceptionHandler {
    private final ObjectMapper mapper; ApiExceptionHandler(ObjectMapper mapper){this.mapper=mapper;}
    @ExceptionHandler(NotFoundFailure.class) ResponseEntity<ObjectNode> notFound(RuntimeException e){return body(HttpStatus.NOT_FOUND,e.getMessage());}
    @ExceptionHandler({ConflictFailure.class,org.springframework.dao.OptimisticLockingFailureException.class}) ResponseEntity<ObjectNode> conflict(RuntimeException e){return body(HttpStatus.CONFLICT,e.getMessage());}
    @ExceptionHandler({IllegalArgumentException.class}) ResponseEntity<ObjectNode> bad(RuntimeException e){return body(HttpStatus.BAD_REQUEST,e.getMessage());}
    @ExceptionHandler(GatewayFailure.class) ResponseEntity<ObjectNode> gateway(GatewayFailure e){return body(HttpStatus.BAD_GATEWAY,"网关调用失败："+e.getMessage());}
    private ResponseEntity<ObjectNode> body(HttpStatus status,String message){var o=mapper.createObjectNode();o.put("error",message==null?status.getReasonPhrase():message);return ResponseEntity.status(status).body(o);}
}
