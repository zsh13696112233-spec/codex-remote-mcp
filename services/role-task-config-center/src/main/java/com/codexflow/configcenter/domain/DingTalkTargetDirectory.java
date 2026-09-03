package com.codexflow.configcenter.domain;

import java.time.Instant;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 钉钉通知对象的同步、发现和管理事务边界。 */
@Service
public class DingTalkTargetDirectory {

  private final DingTalkTargetRepository targets;
  private final TaskDefinitionRepository tasks;

  DingTalkTargetDirectory(DingTalkTargetRepository targets, TaskDefinitionRepository tasks) {
    this.targets = targets;
    this.tasks = tasks;
  }

  @Transactional(readOnly = true)
  public List<TargetView> list(String clientId) {
    return targets
        .findByClientIdAndDeletedFalseOrderByTargetTypeAscDisplayNameAsc(clientId)
        .stream()
        .map(DingTalkTargetDirectory::view)
        .toList();
  }

  @Transactional
  public SyncResult syncPeople(String clientId, List<RemotePerson> people) {
    Instant now = Instant.now();
    Set<String> seen = new HashSet<>();
    int created = 0;
    int updated = 0;
    for (RemotePerson person : people) {
      String userId = required(person.userId(), "钉钉人员 userId");
      String name = required(person.displayName(), "钉钉人员姓名");
      if (!seen.add(userId)) continue;
      DingTalkTargetEntity target =
          targets.findByClientIdAndTargetTypeAndExternalId(clientId, "PERSON", userId).orElse(null);
      if (target == null) {
        target = new DingTalkTargetEntity();
        target.id = UUID.randomUUID().toString();
        target.clientId = clientId;
        target.targetType = "PERSON";
        target.externalId = userId;
        target.source = "DIRECTORY";
        target.enabled = false;
        created++;
      } else {
        updated++;
      }
      target.displayName = abbreviate(name, 160);
      target.departmentDisplay = nullableAbbreviate(person.departmentDisplay(), 1000);
      target.available = true;
      target.deleted = false;
      target.lastSyncedAt = now;
      targets.save(target);
    }
    int unavailable = 0;
    for (DingTalkTargetEntity target :
        targets.findByClientIdAndDeletedFalseOrderByTargetTypeAscDisplayNameAsc(clientId)) {
      if ("PERSON".equals(target.targetType)
          && !seen.contains(target.externalId)
          && target.available) {
        target.available = false;
        target.enabled = false;
        target.lastSyncedAt = now;
        unavailable++;
      }
    }
    return new SyncResult(created, updated, unavailable, seen.size(), now);
  }

  @Transactional
  public TargetView discoverGroup(String clientId, String conversationId, String displayName) {
    String externalId = required(conversationId, "钉钉群会话 ID");
    DingTalkTargetEntity target =
        targets
            .findByClientIdAndTargetTypeAndExternalId(clientId, "GROUP", externalId)
            .orElseGet(
                () -> {
                  DingTalkTargetEntity value = new DingTalkTargetEntity();
                  value.id = UUID.randomUUID().toString();
                  value.clientId = clientId;
                  value.targetType = "GROUP";
                  value.externalId = externalId;
                  value.source = "OBSERVED";
                  value.enabled = false;
                  return value;
                });
    if (target.displayName == null || target.displayName.isBlank()) {
      target.displayName =
          displayName == null || displayName.isBlank()
              ? "待命名群聊 " + abbreviate(externalId, 12)
              : abbreviate(displayName.trim(), 160);
    }
    target.available = true;
    target.deleted = false;
    target.lastSyncedAt = Instant.now();
    return view(targets.save(target));
  }

  @Transactional
  public TargetView update(String clientId, String id, String displayName, boolean enabled) {
    DingTalkTargetEntity target = requiredOwned(clientId, id);
    if (enabled && !target.available) throw new ConflictFailure("钉钉通知对象当前不可用，不能启用。");
    if (displayName != null && !displayName.isBlank()) {
      target.displayName = abbreviate(displayName.trim(), 160);
    }
    target.enabled = enabled;
    return view(targets.save(target));
  }

  @Transactional
  public void delete(String clientId, String id) {
    DingTalkTargetEntity target = requiredOwned(clientId, id);
    if (tasks.existsByDingtalkTargetIdAndDeletedFalse(id)) {
      throw new ConflictFailure("通知对象已被任务定义绑定，只能停用。");
    }
    target.enabled = false;
    target.deleted = true;
    targets.save(target);
  }

  @Transactional
  DingTalkTargetEntity requiredSelectable(String id, String taskId) {
    DingTalkTargetEntity target =
        targets.findForUpdate(id).orElseThrow(() -> new NotFoundFailure("找不到钉钉通知对象：" + id));
    if (target.deleted || !target.enabled || !target.available) {
      throw new ConflictFailure("所选钉钉通知对象未启用或当前不可用。");
    }
    tasks
        .findFirstByDingtalkTargetIdAndDeletedFalse(id)
        .filter(owner -> !owner.id.equals(taskId))
        .ifPresent(
            owner -> {
              throw new ConflictFailure("该钉钉通知对象已绑定其他任务定义。");
            });
    return target;
  }

  private DingTalkTargetEntity requiredOwned(String clientId, String id) {
    DingTalkTargetEntity target =
        targets.findById(id).orElseThrow(() -> new NotFoundFailure("找不到钉钉通知对象：" + id));
    if (target.deleted || !target.clientId.equals(clientId)) {
      throw new NotFoundFailure("找不到钉钉通知对象：" + id);
    }
    return target;
  }

  static TargetView view(DingTalkTargetEntity value) {
    return new TargetView(
        value.id,
        value.clientId,
        value.targetType,
        value.externalId,
        value.displayName,
        value.departmentDisplay,
        value.source,
        value.available,
        value.enabled,
        value.lastSyncedAt,
        value.createdAt,
        value.updatedAt);
  }

  private static String required(String value, String label) {
    if (value == null || value.isBlank()) throw new IllegalArgumentException(label + "不能为空。");
    return value.trim();
  }

  private static String abbreviate(String value, int max) {
    return value.length() <= max ? value : value.substring(0, max);
  }

  private static String nullableAbbreviate(String value, int max) {
    if (value == null || value.isBlank()) return null;
    return abbreviate(value.trim(), max);
  }

  public record RemotePerson(String userId, String displayName, String departmentDisplay) {}

  public record TargetView(
      String id,
      String clientId,
      String targetType,
      String externalId,
      String displayName,
      String departmentDisplay,
      String source,
      boolean available,
      boolean enabled,
      Instant lastSyncedAt,
      Instant createdAt,
      Instant updatedAt) {}

  public record SyncResult(
      int created, int updated, int unavailable, int total, Instant syncedAt) {}
}
