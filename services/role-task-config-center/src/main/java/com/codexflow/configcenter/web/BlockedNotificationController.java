package com.codexflow.configcenter.web;

import com.codexflow.configcenter.domain.BlockedNotificationStore;
import com.codexflow.configcenter.integration.dingtalk.BlockedNotificationService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/dingtalk/blocked-notifications")
public class BlockedNotificationController {
  private final BlockedNotificationService service;

  public BlockedNotificationController(BlockedNotificationService service) {
    this.service = service;
  }

  @GetMapping("/mappings")
  public List<BlockedNotificationStore.Mapping> mappings() {
    return service.mappings();
  }

  @PutMapping("/mappings/{targetId}")
  public void mapping(@PathVariable String targetId, @Valid @RequestBody MappingRequest request) {
    service.mapping(
        new BlockedNotificationStore.Mapping(targetId, request.accountType(), request.accountId()));
  }

  @GetMapping("/config")
  public Map<String, String> config() {
    return service.config();
  }

  @PutMapping("/config")
  public void config(@Valid @RequestBody ConfigRequest request) {
    service.configure(request.templateId());
  }

  @GetMapping("/{id}")
  public Map<String, Object> status(@PathVariable String id) {
    return service.status(id);
  }

  @PostMapping("/test")
  public Map<String, Object> test(@Valid @RequestBody TestRequest request) {
    return service.test(request.requestId(), request.groupId(), request.personId(), request.text());
  }

  public record MappingRequest(
      @NotNull @Pattern(regexp = "accountId|key|name") String accountType,
      @NotNull @Size(max = 256) String accountId) {}

  public record ConfigRequest(@NotNull @Size(max = 256) String templateId) {}

  public record TestRequest(
      @NotBlank
          @Pattern(
              regexp =
                  "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
          String requestId,
      @NotBlank @Size(max = 36) String groupId,
      @NotBlank @Size(max = 36) String personId,
      @NotBlank @Size(max = 2000) String text) {}
}
