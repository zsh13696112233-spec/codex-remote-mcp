package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.dto.DingTalkConfigSaveRequest;
import java.time.Instant;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 保存页面配置，并把数据库配置应用到运行中的钉钉属性对象。 */
@Service
class DingTalkSettingsStore {

  private static final byte SETTINGS_ID = 1;

  private final DingTalkSettingsRepository repository;
  private final DingTalkProperties properties;

  DingTalkSettingsStore(DingTalkSettingsRepository repository, DingTalkProperties properties) {
    this.repository = repository;
    this.properties = properties;
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
    entity.taskDefinitionId = null;
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
        request.cardTemplateId().trim(),
        request.eventPollIntervalMs(),
        repository.existsById(SETTINGS_ID));
  }

  private Settings environmentSettings() {
    return new Settings(
        properties.isEnabled(),
        properties.getClientId(),
        properties.getClientSecret(),
        properties.getCardTemplateId(),
        properties.getEventPollIntervalMs(),
        false);
  }

  private void apply(Settings settings) {
    properties.setEnabled(settings.enabled());
    properties.setClientId(settings.clientId());
    properties.setClientSecret(settings.clientSecret());
    properties.setCardTemplateId(settings.cardTemplateId());
    properties.setEventPollIntervalMs(settings.eventPollIntervalMs());
  }

  private static Settings toSettings(DingTalkSettingsEntity entity) {
    return new Settings(
        entity.enabled,
        entity.clientId,
        entity.clientSecret,
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
      String cardTemplateId,
      long eventPollIntervalMs,
      boolean persisted) {}
}
