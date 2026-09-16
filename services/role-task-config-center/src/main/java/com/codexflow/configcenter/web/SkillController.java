package com.codexflow.configcenter.web;

import com.codexflow.configcenter.application.SkillService;
import jakarta.servlet.http.HttpServletRequest;
import java.io.IOException;
import java.util.UUID;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

@RestController
public class SkillController {
  private static final int ZIP_LIMIT = 20 * 1024 * 1024;
  private final SkillService service;
  private final ObjectMapper mapper;

  public SkillController(SkillService service, ObjectMapper mapper) {
    this.service = service;
    this.mapper = mapper;
  }

  @GetMapping("/api/skills")
  public ResponseEntity<JsonNode> list(@RequestParam(required = false) String groupId) {
    return service.list(false, groupId);
  }

  @GetMapping("/api/skills/machines")
  public ResponseEntity<JsonNode> machines(@RequestParam(required = false) String groupId) {
    return service.list(true, groupId);
  }

  @GetMapping("/api/skills/{id}")
  public ResponseEntity<JsonNode> detail(@PathVariable String id) {
    return service.detail(id);
  }

  @GetMapping("/api/skills/inventory")
  public ResponseEntity<JsonNode> inventory(@RequestParam(required = false) String groupId) {
    return service.inventory(groupId);
  }

  @PostMapping("/api/skills/machines/{id}/check")
  public ResponseEntity<JsonNode> checkDirectory(@PathVariable String id) {
    return service.checkDirectory(id);
  }

  @PostMapping("/api/skills")
  public ResponseEntity<JsonNode> upload(HttpServletRequest request) throws IOException {
    String type = request.getContentType();
    if (type == null
        || !(type.split(";")[0].equals("application/zip")
            || type.split(";")[0].equals("application/octet-stream"))) {
      return error(415, "请上传 ZIP 文件。");
    }
    if (request.getContentLengthLong() > ZIP_LIMIT) return error(413, "ZIP 不能超过 20 MiB。");
    byte[] content = request.getInputStream().readNBytes(ZIP_LIMIT + 1);
    if (content.length > ZIP_LIMIT) return error(413, "ZIP 不能超过 20 MiB。");
    return service.upload(content, request.getParameter("groupId"));
  }

  @GetMapping("/api/skill-deployments")
  public ResponseEntity<JsonNode> deployments(@RequestParam(required = false) String groupId) {
    return service.deployments(null, groupId);
  }

  @GetMapping("/api/skill-deployments/{id}")
  public ResponseEntity<JsonNode> deployment(@PathVariable UUID id) {
    return service.deployments(id.toString(), null);
  }

  @PostMapping("/api/skills/groups/assign")
  public ResponseEntity<JsonNode> assign(HttpServletRequest request) throws IOException {
    byte[] content = request.getInputStream().readNBytes(32769);
    if (content.length > 32768) return error(413, "请求内容过大。");
    return service.assign(readJson(content));
  }

  @PostMapping("/api/skill-deployments")
  public ResponseEntity<JsonNode> create(HttpServletRequest request) throws IOException {
    byte[] content = request.getInputStream().readNBytes(32769);
    if (content.length > 32768) return error(413, "请求内容过大。");
    return service.create(readJson(content));
  }

  @PostMapping("/api/skill-deployment-tasks/{id}/{action}")
  public ResponseEntity<JsonNode> action(
      @PathVariable UUID id, @PathVariable String action, HttpServletRequest request)
      throws IOException {
    byte[] content = request.getInputStream().readNBytes(32769);
    if (content.length > 32768) return error(413, "请求内容过大。");
    return service.action(id.toString(), action, readJson(content));
  }

  private JsonNode readJson(byte[] content) {
    try {
      JsonNode body = mapper.readTree(content);
      if (body == null || !body.isObject()) throw new IllegalArgumentException("请求必须为 JSON 对象。");
      return body;
    } catch (tools.jackson.core.JacksonException error) {
      throw new IllegalArgumentException("请求必须为合法 JSON。");
    }
  }

  private ResponseEntity<JsonNode> error(int status, String message) {
    return ResponseEntity.status(status).body(mapper.createObjectNode().put("error", message));
  }
}
