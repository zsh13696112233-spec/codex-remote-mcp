package com.codexflow.configcenter.integration.feishu;

import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.dto.FeishuConfigSaveRequest;
import java.time.Instant;
import java.util.Optional;
import org.springframework.stereotype.Service;

/** 为配置中心页面提供不回显密钥的飞书机器人管理能力。 */
@Service
public class FeishuBotAdminService {

  private final FeishuSettingsStore settings;
  private final FeishuStore botStore;
  private final FeishuBotCoordinator coordinator;
  private final FeishuTransport transport;

  FeishuBotAdminService(
      FeishuSettingsStore settings,
      FeishuStore botStore,
      FeishuBotCoordinator coordinator,
      FeishuTransport transport) {
    this.settings = settings;
    this.botStore = botStore;
    this.coordinator = coordinator;
    this.transport = transport;
  }

  public ConfigView current() {
    return view(settings.current(), null);
  }

  public ConfigView save(FeishuConfigSaveRequest request) {
    FeishuSettingsStore.Settings previous = settings.current();
    Optional<FeishuModels.Binding> active =
        previous.appId().isBlank() ? Optional.empty() : botStore.active(previous.appId());
    if (active.isPresent() && criticalChange(previous, request)) {
      throw new ConflictFailure("当前有机器人任务正在运行，不能停用或修改应用、密钥和固定任务。任务编号：" + active.get().workflowId());
    }
    FeishuSettingsStore.Settings saved = settings.save(request);
    boolean reconnect =
        previous.enabled() != saved.enabled()
            || !previous.appId().equals(saved.appId())
            || !previous.appSecret().equals(saved.appSecret());
    if (reconnect) coordinator.reconfigure();
    return view(saved, reconnect ? "配置已保存，并已重新应用长连接。" : "配置已保存。");
  }

  public ConnectionTestResult test(FeishuConfigSaveRequest request) {
    FeishuSettingsStore.Settings candidate = settings.forTest(request);
    try {
      transport.testConnection(candidate.appId(), candidate.appSecret());
      return new ConnectionTestResult(true, "连接成功，App ID 与 App Secret 可用于飞书长连接。", Instant.now());
    } catch (RuntimeException error) {
      return new ConnectionTestResult(false, "连接失败，请检查应用凭据、机器人能力、应用发布状态和网络。", Instant.now());
    }
  }

  private ConfigView view(FeishuSettingsStore.Settings value, String message) {
    return new ConfigView(
        value.enabled(),
        value.appId(),
        !value.appSecret().isBlank(),
        value.taskDefinitionId(),
        value.eventPollIntervalMs(),
        value.persisted(),
        coordinator.connectionStatus(),
        message);
  }

  private static boolean criticalChange(
      FeishuSettingsStore.Settings previous, FeishuConfigSaveRequest request) {
    String submittedSecret = request.appSecret() == null ? "" : request.appSecret().trim();
    return previous.enabled() != request.enabled()
        || !previous.appId().equals(request.appId().trim())
        || (!submittedSecret.isBlank() && !previous.appSecret().equals(submittedSecret))
        || !previous.taskDefinitionId().equals(request.taskDefinitionId().trim());
  }

  public record ConfigView(
      boolean enabled,
      String appId,
      boolean secretConfigured,
      String taskDefinitionId,
      long eventPollIntervalMs,
      boolean persisted,
      String connectionStatus,
      String message) {}

  public record ConnectionTestResult(boolean success, String message, Instant checkedAt) {}
}
