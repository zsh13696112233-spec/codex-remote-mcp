package com.codexflow.configcenter.web;

import com.codexflow.configcenter.dto.FeishuConfigSaveRequest;
import com.codexflow.configcenter.integration.feishu.FeishuBotAdminService;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 配置中心内网页面使用的飞书机器人配置与连接测试接口。 */
@RestController
@RequestMapping("/api/feishu/config")
public class FeishuConfigController {

  private final FeishuBotAdminService service;

  public FeishuConfigController(FeishuBotAdminService service) {
    this.service = service;
  }

  @GetMapping
  public FeishuBotAdminService.ConfigView get() {
    return service.current();
  }

  @PutMapping
  public FeishuBotAdminService.ConfigView save(
      @Valid @RequestBody FeishuConfigSaveRequest request) {
    return service.save(request);
  }

  @PostMapping("/test")
  public FeishuBotAdminService.ConnectionTestResult test(
      @Valid @RequestBody FeishuConfigSaveRequest request) {
    return service.test(request);
  }
}
