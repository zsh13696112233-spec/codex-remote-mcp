package com.codexflow.configcenter.domain;

import java.time.Instant;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 钉钉通知对象的同步、发现和管理事务边界。 */
@Service
public class DingTalkTargetDirectory {

  private final DingTalkTargetRepository targets;
  private final DingTalkDepartmentRepository departments;
  private final TaskDefinitionRepository tasks;

  DingTalkTargetDirectory(
      DingTalkTargetRepository targets,
      DingTalkDepartmentRepository departments,
      TaskDefinitionRepository tasks) {
    this.targets = targets;
    this.departments = departments;
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

  @Transactional(readOnly = true)
  public DirectoryView directory(String clientId) {
    List<DepartmentView> departmentViews =
        departments.findByClientIdOrderByDisplayNameAsc(clientId).stream()
            .filter(value -> value.available)
            .map(DingTalkTargetDirectory::departmentView)
            .toList();
    List<TargetView> people =
        targets.findByClientIdAndDeletedFalseOrderByTargetTypeAscDisplayNameAsc(clientId).stream()
            .filter(value -> "PERSON".equals(value.targetType))
            .map(DingTalkTargetDirectory::view)
            .toList();
    return new DirectoryView(departmentViews, people);
  }

  @Transactional
  public SyncResult syncPeople(String clientId, List<RemotePerson> people) {
    return syncDirectory(clientId, new RemoteDirectory(List.of(), people));
  }

  @Transactional
  public SyncResult syncDirectory(String clientId, RemoteDirectory directory) {
    Instant now = Instant.now();
    Map<String, DingTalkDepartmentEntity> savedDepartments =
        syncDepartments(clientId, directory.departments(), now);
    Set<String> seen = new HashSet<>();
    int created = 0;
    int updated = 0;
    for (RemotePerson person : directory.people()) {
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
      target.departments.clear();
      for (String departmentId : person.departmentIds()) {
        DingTalkDepartmentEntity department = savedDepartments.get(departmentId);
        if (department != null && department.available) target.departments.add(department);
      }
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

  private Map<String, DingTalkDepartmentEntity> syncDepartments(
      String clientId, List<RemoteDepartment> remoteDepartments, Instant now) {
    Map<String, DingTalkDepartmentEntity> saved = new HashMap<>();
    Set<String> seen = new HashSet<>();
    for (RemoteDepartment remote : remoteDepartments) {
      String externalId = required(remote.externalId(), "钉钉部门 ID");
      String name = required(remote.displayName(), "钉钉部门名称");
      if (!seen.add(externalId)) continue;
      DingTalkDepartmentEntity department =
          departments.findByClientIdAndExternalId(clientId, externalId).orElse(null);
      if (department == null) {
        department = new DingTalkDepartmentEntity();
        department.id = UUID.randomUUID().toString();
        department.clientId = clientId;
        department.externalId = externalId;
      }
      department.parentExternalId = normalizeNullable(remote.parentExternalId());
      department.displayName = abbreviate(name, 160);
      department.available = true;
      department.lastSyncedAt = now;
      saved.put(externalId, departments.save(department));
    }
    if (!remoteDepartments.isEmpty()) {
      for (DingTalkDepartmentEntity department :
          departments.findByClientIdOrderByDisplayNameAsc(clientId)) {
        if (!seen.contains(department.externalId) && department.available) {
          department.available = false;
          department.lastSyncedAt = now;
          departments.save(department);
        }
      }
    }
    return saved;
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
    if ("PERSON".equals(target.targetType)) {
      throw new ConflictFailure("人员由钉钉通讯录同步维护，不能手动删除。");
    }
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
        value.departments.stream().map(item -> item.externalId).sorted().toList(),
        value.lastSyncedAt,
        value.createdAt,
        value.updatedAt);
  }

  private static DepartmentView departmentView(DingTalkDepartmentEntity value) {
    return new DepartmentView(
        value.externalId,
        value.parentExternalId,
        value.displayName,
        value.available,
        value.lastSyncedAt);
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

  private static String normalizeNullable(String value) {
    return value == null || value.isBlank() ? null : value.trim();
  }

  public record RemoteDepartment(String externalId, String parentExternalId, String displayName) {}

  public record RemotePerson(
      String userId, String displayName, String departmentDisplay, List<String> departmentIds) {

    public RemotePerson {
      departmentIds = departmentIds == null ? List.of() : List.copyOf(departmentIds);
    }

    public RemotePerson(String userId, String displayName, String departmentDisplay) {
      this(userId, displayName, departmentDisplay, List.of());
    }
  }

  public record RemoteDirectory(List<RemoteDepartment> departments, List<RemotePerson> people) {

    public RemoteDirectory {
      departments = departments == null ? List.of() : List.copyOf(departments);
      people = people == null ? List.of() : List.copyOf(people);
    }
  }

  public record DepartmentView(
      String externalId,
      String parentExternalId,
      String displayName,
      boolean available,
      Instant lastSyncedAt) {}

  public record DirectoryView(List<DepartmentView> departments, List<TargetView> people) {}

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
      List<String> departmentIds,
      Instant lastSyncedAt,
      Instant createdAt,
      Instant updatedAt) {}

  public record SyncResult(
      int created, int updated, int unavailable, int total, Instant syncedAt) {}
}
