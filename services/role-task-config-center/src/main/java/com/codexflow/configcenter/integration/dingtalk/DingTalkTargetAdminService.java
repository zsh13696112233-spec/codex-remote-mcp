package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.domain.DingTalkTargetDirectory;
import java.util.List;
import org.springframework.stereotype.Service;

/** 为配置中心页面提供钉钉人员、群目录和测试发送能力。 */
@Service
public class DingTalkTargetAdminService {

  private final DingTalkSettingsStore settings;
  private final DingTalkTargetDirectory targets;
  private final DingTalkTransport transport;

  DingTalkTargetAdminService(
      DingTalkSettingsStore settings,
      DingTalkTargetDirectory targets,
      DingTalkTransport transport) {
    this.settings = settings;
    this.targets = targets;
    this.transport = transport;
  }

  public List<DingTalkTargetDirectory.TargetView> list() {
    String clientId = settings.current().clientId();
    return clientId.isBlank() ? List.of() : targets.list(clientId);
  }

  public DingTalkTargetDirectory.SyncResult syncPeople() {
    DingTalkSettingsStore.Settings value = requiredSettings();
    return targets.syncPeople(
        value.clientId(), transport.listPeople(value.clientId(), value.clientSecret()));
  }

  public DingTalkTargetDirectory.TargetView update(String id, String displayName, boolean enabled) {
    return targets.update(requiredClientId(), id, displayName, enabled);
  }

  public void delete(String id) {
    targets.delete(requiredClientId(), id);
  }

  public TestResult test(String id) {
    DingTalkTargetDirectory.TargetView target =
        targets.list(requiredSettings().clientId()).stream()
            .filter(item -> item.id().equals(id))
            .findFirst()
            .orElseThrow(() -> new IllegalArgumentException("找不到钉钉通知对象。"));
    String text = "Codex SOP 通知对象测试成功。";
    if ("PERSON".equals(target.targetType())) {
      transport.sendPersonText(target.externalId(), text);
    } else {
      transport.sendText(target.externalId(), null, text);
    }
    return new TestResult(true, "测试消息已发送。");
  }

  private DingTalkSettingsStore.Settings requiredSettings() {
    DingTalkSettingsStore.Settings value = settings.current();
    if (value.clientId().isBlank() || value.clientSecret().isBlank()) {
      throw new IllegalArgumentException("请先保存钉钉 Client ID 和 Client Secret。");
    }
    return value;
  }

  private String requiredClientId() {
    String clientId = settings.current().clientId();
    if (clientId.isBlank()) {
      throw new IllegalArgumentException("请先保存钉钉 Client ID。");
    }
    return clientId;
  }

  public record TestResult(boolean success, String message) {}
}
