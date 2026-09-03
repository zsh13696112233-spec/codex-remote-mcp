package com.codexflow.configcenter.web;

import com.codexflow.configcenter.domain.DingTalkTargetDirectory;
import com.codexflow.configcenter.integration.dingtalk.DingTalkTargetAdminService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import java.util.List;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 配置中心内网页面使用的钉钉通知对象管理接口。 */
@RestController
@RequestMapping("/api/dingtalk/targets")
public class DingTalkTargetController {

  private final DingTalkTargetAdminService service;

  public DingTalkTargetController(DingTalkTargetAdminService service) {
    this.service = service;
  }

  @GetMapping
  public List<DingTalkTargetDirectory.TargetView> list() {
    return service.list();
  }

  @PostMapping("/sync-people")
  public DingTalkTargetDirectory.SyncResult syncPeople() {
    return service.syncPeople();
  }

  @PutMapping("/{id}")
  public DingTalkTargetDirectory.TargetView update(
      @PathVariable String id, @Valid @RequestBody UpdateRequest request) {
    return service.update(id, request.displayName(), request.enabled());
  }

  @DeleteMapping("/{id}")
  public void delete(@PathVariable String id) {
    service.delete(id);
  }

  @PostMapping("/{id}/test")
  public DingTalkTargetAdminService.TestResult test(@PathVariable String id) {
    return service.test(id);
  }

  public record UpdateRequest(@Size(max = 160) String displayName, @NotNull Boolean enabled) {}
}
