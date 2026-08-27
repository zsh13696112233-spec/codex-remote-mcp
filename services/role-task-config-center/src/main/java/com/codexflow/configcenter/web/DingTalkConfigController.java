package com.codexflow.configcenter.web;

import com.codexflow.configcenter.dto.DingTalkConfigSaveRequest;
import com.codexflow.configcenter.integration.dingtalk.DingTalkBotAdminService;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 配置中心内网页面使用的钉钉机器人配置与连接测试接口。 */
@RestController
@RequestMapping("/api/dingtalk/config")
public class DingTalkConfigController {

  private final DingTalkBotAdminService service;

  public DingTalkConfigController(DingTalkBotAdminService service) {
    this.service = service;
  }

  @GetMapping
  public DingTalkBotAdminService.ConfigView get() {
    return service.current();
  }

  @PutMapping
  public DingTalkBotAdminService.ConfigView save(
      @Valid @RequestBody DingTalkConfigSaveRequest request) {
    return service.save(request);
  }

  @PostMapping("/test")
  public DingTalkBotAdminService.ConnectionTestResult test(
      @Valid @RequestBody DingTalkConfigSaveRequest request) {
    return service.test(request);
  }
}
