package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.dto.DingTalkConfigSaveRequest;
import java.time.Instant;
import org.springframework.stereotype.Service;

/** 为配置中心页面提供不回显密钥的钉钉机器人管理能力。 */
@Service
public class DingTalkBotAdminService {

  private final DingTalkSettingsStore settings;
  private final DingTalkStore botStore;
  private final DingTalkBotCoordinator coordinator;
  private final DingTalkTransport transport;

  DingTalkBotAdminService(
      DingTalkSettingsStore settings,
      DingTalkStore botStore,
      DingTalkBotCoordinator coordinator,
      DingTalkTransport transport) {
    this.settings = settings;
    this.botStore = botStore;
    this.coordinator = coordinator;
    this.transport = transport;
  }

  public ConfigView current() {
    return view(settings.current(), null);
  }

  public ConfigView save(DingTalkConfigSaveRequest request) {
    DingTalkSettingsStore.Settings previous = settings.current();
    boolean active = !previous.clientId().isBlank() && botStore.hasActive(previous.clientId());
    if (active && criticalChange(previous, request)) {
      throw new ConflictFailure("当前有钉钉任务正在运行，不能停用或修改应用、密钥和卡片模板。");
    }
    DingTalkSettingsStore.Settings saved = settings.save(request);
    boolean reconnect =
        previous.enabled() != saved.enabled()
            || !previous.clientId().equals(saved.clientId())
            || !previous.clientSecret().equals(saved.clientSecret());
    if (reconnect) coordinator.reconfigure();
    return view(saved, reconnect ? "配置已保存，并已重新应用长连接。" : "配置已保存。");
  }

  public ConnectionTestResult test(DingTalkConfigSaveRequest request) {
    DingTalkSettingsStore.Settings candidate = settings.forTest(request);
    try {
      transport.testConnection(candidate.clientId(), candidate.clientSecret());
      return new ConnectionTestResult(
          true, "连接成功，Client ID 与 Client Secret 可用于钉钉长连接。", Instant.now());
    } catch (RuntimeException error) {
      return new ConnectionTestResult(false, "连接失败，请检查应用凭据、机器人能力、应用发布状态和网络。", Instant.now());
    }
  }

  private ConfigView view(DingTalkSettingsStore.Settings value, String message) {
    return new ConfigView(
        value.enabled(),
        value.clientId(),
        !value.clientSecret().isBlank(),
        value.cardTemplateId(),
        value.eventPollIntervalMs(),
        value.persisted(),
        coordinator.connectionStatus(),
        message);
  }

  private static boolean criticalChange(
      DingTalkSettingsStore.Settings previous, DingTalkConfigSaveRequest request) {
    String submittedSecret = request.clientSecret() == null ? "" : request.clientSecret().trim();
    return previous.enabled() != request.enabled()
        || !previous.clientId().equals(request.clientId().trim())
        || (!submittedSecret.isBlank() && !previous.clientSecret().equals(submittedSecret))
        || !previous.cardTemplateId().equals(request.cardTemplateId().trim());
  }

  public record ConfigView(
      boolean enabled,
      String clientId,
      boolean secretConfigured,
      String cardTemplateId,
      long eventPollIntervalMs,
      boolean persisted,
      String connectionStatus,
      String message) {}

  public record ConnectionTestResult(boolean success, String message, Instant checkedAt) {}
}
