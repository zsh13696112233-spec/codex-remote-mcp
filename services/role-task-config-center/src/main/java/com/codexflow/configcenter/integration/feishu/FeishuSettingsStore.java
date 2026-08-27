package com.codexflow.configcenter.integration.feishu;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.dto.FeishuConfigSaveRequest;
import java.time.Instant;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.node.ObjectNode;

/** 保存页面配置，并把数据库配置应用到运行中的飞书属性对象。 */
@Service
class FeishuSettingsStore {

  private static final byte SETTINGS_ID = 1;

  private final FeishuSettingsRepository repository;
  private final FeishuProperties properties;
  private final ConfigService configService;

  FeishuSettingsStore(
      FeishuSettingsRepository repository,
      FeishuProperties properties,
      ConfigService configService) {
    this.repository = repository;
    this.properties = properties;
    this.configService = configService;
  }

  @Transactional(readOnly = true)
  public Settings current() {
    return repository
        .findById(SETTINGS_ID)
        .map(FeishuSettingsStore::toSettings)
        .orElseGet(this::environmentSettings);
  }

  @Transactional(readOnly = true)
  public void applyPersisted() {
    repository.findById(SETTINGS_ID).ifPresent(value -> apply(toSettings(value)));
  }

  @Transactional
  public Settings save(FeishuConfigSaveRequest request) {
    ObjectNode task = configService.getTask(request.taskDefinitionId().trim());
    if (!task.path("enabled").asBoolean() || task.path("deleted").asBoolean()) {
      throw new ConflictFailure("机器人只能绑定已启用且未删除的任务定义。");
    }
    Settings previous = current();
    String secret = normalized(request.appSecret());
    if (secret.isBlank()) secret = previous.appSecret();
    if (secret.isBlank()) throw new IllegalArgumentException("请填写飞书 App Secret。");

    FeishuSettingsEntity entity =
        repository.findById(SETTINGS_ID).orElseGet(FeishuSettingsEntity::new);
    entity.id = SETTINGS_ID;
    entity.enabled = request.enabled();
    entity.appId = request.appId().trim();
    entity.appSecret = secret;
    entity.taskDefinitionId = request.taskDefinitionId().trim();
    entity.eventPollIntervalMs = request.eventPollIntervalMs();
    entity.updatedAt = Instant.now();
    repository.saveAndFlush(entity);
    Settings saved = toSettings(entity);
    apply(saved);
    return saved;
  }

  @Transactional(readOnly = true)
  public Settings forTest(FeishuConfigSaveRequest request) {
    String secret = normalized(request.appSecret());
    if (secret.isBlank()) secret = current().appSecret();
    if (secret.isBlank()) throw new IllegalArgumentException("请填写飞书 App Secret。");
    return new Settings(
        request.enabled(),
        request.appId().trim(),
        secret,
        request.taskDefinitionId().trim(),
        request.eventPollIntervalMs(),
        repository.existsById(SETTINGS_ID));
  }

  private Settings environmentSettings() {
    return new Settings(
        properties.isEnabled(),
        properties.getAppId(),
        properties.getAppSecret(),
        properties.getTaskDefinitionId(),
        properties.getEventPollIntervalMs(),
        false);
  }

  private void apply(Settings settings) {
    properties.setEnabled(settings.enabled());
    properties.setAppId(settings.appId());
    properties.setAppSecret(settings.appSecret());
    properties.setTaskDefinitionId(settings.taskDefinitionId());
    properties.setEventPollIntervalMs(settings.eventPollIntervalMs());
  }

  private static Settings toSettings(FeishuSettingsEntity entity) {
    return new Settings(
        entity.enabled,
        entity.appId,
        entity.appSecret,
        entity.taskDefinitionId,
        entity.eventPollIntervalMs,
        true);
  }

  private static String normalized(String value) {
    return value == null ? "" : value.trim();
  }

  record Settings(
      boolean enabled,
      String appId,
      String appSecret,
      String taskDefinitionId,
      long eventPollIntervalMs,
      boolean persisted) {}
}
