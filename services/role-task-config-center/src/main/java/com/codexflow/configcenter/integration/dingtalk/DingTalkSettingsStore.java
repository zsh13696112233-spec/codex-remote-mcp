package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.dto.DingTalkConfigSaveRequest;
import com.codexflow.configcenter.integration.bot.BotPlatformGuard;
import java.time.Instant;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.node.ObjectNode;

/** 保存页面配置，并把数据库配置应用到运行中的钉钉属性对象。 */
@Service
class DingTalkSettingsStore {

  private static final byte SETTINGS_ID = 1;

  private final DingTalkSettingsRepository repository;
  private final DingTalkProperties properties;
  private final ConfigService configService;
  private final BotPlatformGuard platformGuard;

  DingTalkSettingsStore(
      DingTalkSettingsRepository repository,
      DingTalkProperties properties,
      ConfigService configService,
      BotPlatformGuard platformGuard) {
    this.repository = repository;
    this.properties = properties;
    this.configService = configService;
    this.platformGuard = platformGuard;
  }

  @Transactional(readOnly = true)
  public Settings current() {
    return repository
        .findById(SETTINGS_ID)
        .map(DingTalkSettingsStore::toSettings)
        .orElseGet(this::environmentSettings);
  }

  @Transactional(readOnly = true)
  public void applyPersisted() {
    repository.findById(SETTINGS_ID).ifPresent(value -> apply(toSettings(value)));
  }

  @Transactional
  public Settings save(DingTalkConfigSaveRequest request) {
    platformGuard.assertCanEnable("dingtalk", request.enabled());
    ObjectNode task = configService.getTask(request.taskDefinitionId().trim());
    if (!task.path("enabled").asBoolean() || task.path("deleted").asBoolean()) {
      throw new ConflictFailure("机器人只能绑定已启用且未删除的任务定义。");
    }
    Settings previous = current();
    String secret = normalized(request.clientSecret());
    if (secret.isBlank()) secret = previous.clientSecret();
    if (secret.isBlank()) throw new IllegalArgumentException("请填写钉钉 Client Secret。");

    DingTalkSettingsEntity entity =
        repository.findById(SETTINGS_ID).orElseGet(DingTalkSettingsEntity::new);
    entity.id = SETTINGS_ID;
    entity.enabled = request.enabled();
    entity.clientId = request.clientId().trim();
    entity.clientSecret = secret;
    entity.taskDefinitionId = request.taskDefinitionId().trim();
    entity.cardTemplateId = request.cardTemplateId().trim();
    entity.eventPollIntervalMs = request.eventPollIntervalMs();
    entity.updatedAt = Instant.now();
    repository.saveAndFlush(entity);
    Settings saved = toSettings(entity);
    apply(saved);
    return saved;
  }

  @Transactional(readOnly = true)
  public Settings forTest(DingTalkConfigSaveRequest request) {
    String secret = normalized(request.clientSecret());
    if (secret.isBlank()) secret = current().clientSecret();
    if (secret.isBlank()) throw new IllegalArgumentException("请填写钉钉 Client Secret。");
    return new Settings(
        request.enabled(),
        request.clientId().trim(),
        secret,
        request.taskDefinitionId().trim(),
        request.cardTemplateId().trim(),
        request.eventPollIntervalMs(),
        repository.existsById(SETTINGS_ID));
  }

  private Settings environmentSettings() {
    return new Settings(
        properties.isEnabled(),
        properties.getClientId(),
        properties.getClientSecret(),
        properties.getTaskDefinitionId(),
        properties.getCardTemplateId(),
        properties.getEventPollIntervalMs(),
        false);
  }

  private void apply(Settings settings) {
    properties.setEnabled(settings.enabled());
    properties.setClientId(settings.clientId());
    properties.setClientSecret(settings.clientSecret());
    properties.setTaskDefinitionId(settings.taskDefinitionId());
    properties.setCardTemplateId(settings.cardTemplateId());
    properties.setEventPollIntervalMs(settings.eventPollIntervalMs());
  }

  private static Settings toSettings(DingTalkSettingsEntity entity) {
    return new Settings(
        entity.enabled,
        entity.clientId,
        entity.clientSecret,
        entity.taskDefinitionId,
        entity.cardTemplateId,
        entity.eventPollIntervalMs,
        true);
  }

  private static String normalized(String value) {
    return value == null ? "" : value.trim();
  }

  record Settings(
      boolean enabled,
      String clientId,
      String clientSecret,
      String taskDefinitionId,
      String cardTemplateId,
      long eventPollIntervalMs,
      boolean persisted) {}
}
