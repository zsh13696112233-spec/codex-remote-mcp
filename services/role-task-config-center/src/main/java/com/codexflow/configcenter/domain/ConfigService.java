package com.codexflow.configcenter.domain;

import com.codexflow.configcenter.dto.RoleSaveRequest;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.SopStepRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.node.ObjectNode;

/** 管理角色、SOP 和可重复运行任务定义的领域服务。 */
@Service
public class ConfigService {

  private static final Set<String> SUPPORTED_MODELS =
      Set.of("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna");
  private static final Set<String> EXECUTOR_TYPES = Set.of("local", "remote");
  private static final Set<String> ADVANCE_MODES = Set.of("automatic", "semi_automatic");
  private static final Set<String> HANDOFF_MODES = Set.of("legacy_text", "cumulative_files");
  private static final Set<String> PERMISSION_PROFILES =
      Set.of("read_only", "workspace_write", "auto_review", "full_access");

  private static final String DEFAULT_EXPECTED_OUTPUT = "完成本步骤，并返回清晰、完整且可验证的结果。";

  private final RoleRepository roles;
  private final GroupService groups;
  private final SopRepository sops;
  private final SopStepRepository steps;
  private final TaskDefinitionRepository tasks;
  private final DomainJsonMapper json;
  private final DingTalkTargetDirectory dingtalkTargets;
  private final String defaultModel;
  private final TaskScheduleStore schedules;

  /** 注入配置数据访问组件、JSON 映射器和默认模型配置。 */
  ConfigService(
      RoleRepository roles,
      GroupService groups,
      SopRepository sops,
      SopStepRepository steps,
      TaskDefinitionRepository tasks,
      DomainJsonMapper json,
      DingTalkTargetDirectory dingtalkTargets,
      TaskScheduleStore schedules,
      @Value("${codex.default-step-model:gpt-5.6-sol}") String defaultModel) {
    this.roles = roles;
    this.groups = groups;
    this.sops = sops;
    this.steps = steps;
    this.tasks = tasks;
    this.json = json;
    this.dingtalkTargets = dingtalkTargets;
    this.defaultModel = defaultModel;
    this.schedules = schedules;
  }

  /** 按可选关键字查询角色列表。 */
  @Transactional(readOnly = true)
  public List<ObjectNode> listRoles(String query) {
    return listRoles(query, "");
  }

  @Transactional(readOnly = true)
  public List<ObjectNode> listRoles(String query, String groupId) {
    GroupService.validateFilter(groupId);
    List<RoleEntity> values;
    if ("unassigned".equals(groupId))
      values =
          roles.findByDeletedFalseAndGroupIdIsNullAndNameContainingIgnoreCaseOrderByCreatedAtDesc(
              normalize(query));
    else if (groupId != null && !groupId.isEmpty())
      values =
          roles.findByDeletedFalseAndGroupIdAndNameContainingIgnoreCaseOrderByCreatedAtDesc(
              groupId, normalize(query));
    else
      values =
          roles.findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(normalize(query));
    return values.stream().map(json::role).toList();
  }

  /** 创建角色并返回稳定的 API JSON。 */
  @Transactional
  public ObjectNode createRole(RoleSaveRequest body) {
    groups.lock();
    RoleEntity role = new RoleEntity();
    role.id = newId();
    applyRole(role, body, false);
    return json.role(roles.save(role));
  }

  /** 按乐观锁版本更新角色，避免并发编辑相互覆盖。 */
  @Transactional
  public ObjectNode updateRole(String id, RoleSaveRequest body) {
    groups.lock();
    RoleEntity role = findRole(id);
    if (body.version() == null) {
      throw new IllegalArgumentException("version 必须提供。");
    }
    long expectedVersion = body.version();
    if (role.version != expectedVersion) {
      throw new ConflictFailure("角色已被其他操作修改，请刷新后重试。");
    }
    applyRole(role, body, true);
    try {
      return json.role(roles.saveAndFlush(role));
    } catch (OptimisticLockingFailureException error) {
      throw new ConflictFailure("角色已被其他操作修改，请刷新后重试。");
    }
  }

  /** 软删除未被有效 SOP 步骤引用的角色，同时保留历史 SOP 步骤的外键关系。 */
  @Transactional
  public void deleteRole(String id) {
    groups.lock();
    RoleEntity role = findRole(id);
    if (steps.existsByRoleIdAndSopDeletedFalse(id)) {
      throw new ConflictFailure("角色已被 SOP 引用，只能停用。");
    }
    role.deleted = true;
    role.enabled = false;
    roles.save(role);
  }

  /** 按可选关键字查询 SOP 列表。 */
  @Transactional(readOnly = true)
  public List<ObjectNode> listSops(String query) {
    return listSops(query, "");
  }

  @Transactional(readOnly = true)
  public List<ObjectNode> listSops(String query, String groupId) {
    GroupService.validateFilter(groupId);
    List<SopEntity> values;
    if ("unassigned".equals(groupId))
      values =
          sops.findByDeletedFalseAndGroupIdIsNullAndNameContainingIgnoreCaseOrderByCreatedAtDesc(
              normalize(query));
    else if (groupId != null && !groupId.isEmpty())
      values =
          sops.findByDeletedFalseAndGroupIdAndNameContainingIgnoreCaseOrderByCreatedAtDesc(
              groupId, normalize(query));
    else
      values =
          sops.findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(normalize(query));
    return values.stream().map(json::sop).toList();
  }

  /** 根据 ID 获取包含完整步骤的 SOP。 */
  @Transactional(readOnly = true)
  public ObjectNode getSop(String id) {
    return json.sop(findSop(id));
  }

  /** 创建 SOP 及其全部串行步骤。 */
  @Transactional
  public ObjectNode createSop(SopSaveRequest body) {
    groups.lock();
    SopEntity sop = new SopEntity();
    sop.id = newId();
    applySop(sop, body);
    return json.sop(sops.save(sop));
  }

  /** 使用请求中的完整步骤集合替换现有 SOP 内容。 */
  @Transactional
  public ObjectNode updateSop(String id, SopSaveRequest body) {
    groups.lock();
    SopEntity sop = findSop(id);
    applySop(sop, body);
    return json.sop(sops.save(sop));
  }

  /** 软删除未被有效任务定义引用的 SOP，同时保留历史任务的外键关系。 */
  @Transactional
  public void deleteSop(String id) {
    groups.lock();
    SopEntity sop = findSop(id);
    if (tasks.existsBySopIdAndDeletedFalse(id)) {
      throw new ConflictFailure("SOP 已被任务定义引用，只能停用。");
    }
    sop.deleted = true;
    sop.enabled = false;
    sops.save(sop);
  }

  /** 查询未软删除且名称匹配的任务定义。 */
  @Transactional(readOnly = true)
  public List<ObjectNode> listTasks(String query) {
    return listTasks(query, "");
  }

  @Transactional(readOnly = true)
  public List<ObjectNode> listTasks(String query, String groupId) {
    GroupService.validateFilter(groupId);
    List<TaskDefinitionEntity> values;
    if ("unassigned".equals(groupId))
      values =
          tasks.findByDeletedFalseAndGroupIdIsNullAndNameContainingIgnoreCaseOrderByCreatedAtDesc(
              normalize(query));
    else if (groupId != null && !groupId.isEmpty())
      values =
          tasks.findByDeletedFalseAndGroupIdAndNameContainingIgnoreCaseOrderByCreatedAtDesc(
              groupId, normalize(query));
    else
      values =
          tasks.findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(normalize(query));
    return values.stream().map(json::task).toList();
  }

  /** 根据 ID 获取任务定义，历史场景允许读取已软删除记录。 */
  @Transactional(readOnly = true)
  public ObjectNode getTask(String id) {
    return json.task(findTask(id, true));
  }

  /** 创建可重复运行的任务定义。 */
  @Transactional
  public ObjectNode createTask(TaskDefinitionSaveRequest body) {
    groups.lock();
    TaskDefinitionEntity task = new TaskDefinitionEntity();
    task.id = newId();
    applyTask(task, body);
    return json.task(tasks.save(task));
  }

  /** 更新未软删除的任务定义。 */
  @Transactional
  public ObjectNode updateTask(String id, TaskDefinitionSaveRequest body) {
    groups.lock();
    TaskDefinitionEntity task = findTaskForUpdate(id);
    applyTask(task, body);
    return json.task(tasks.save(task));
  }

  /** 复制任务定义并将副本默认设为停用。 */
  @Transactional
  public ObjectNode copyTask(String id) {
    groups.lock();
    TaskDefinitionEntity source = findTask(id, true);
    TaskDefinitionEntity copy = new TaskDefinitionEntity();
    copy.id = newId();
    groups.require(source.groupId);
    GroupService.same(source.groupId, source.sop.groupId);
    copy.groupId = source.groupId;
    copy.name = source.name + "（副本）";
    copy.objective = source.objective;
    copy.sop = source.sop;
    copy.additionalNotes = source.additionalNotes;
    copy.scheduleMode = source.scheduleMode;
    copy.scheduleTime = source.scheduleTime;
    copy.scheduleIntervalMinutes = source.scheduleIntervalMinutes;
    copy.scheduleEnabled = false;
    copy.notifyDingTalk = false;
    copy.enabled = false;
    return json.task(tasks.save(copy));
  }

  /** 通过设置删除标记和停用标记软删除任务定义。 */
  @Transactional
  public void deleteTask(String id) {
    groups.lock();
    TaskDefinitionEntity task = findTaskForUpdate(id);
    if (task.activeWorkflowId != null || task.dingtalkActiveWorkflowId != null) {
      throw new ConflictFailure("当前任务仍在运行，不能删除任务定义。");
    }
    schedules.disableTask(task.id);
    task.deleted = true;
    task.enabled = false;
    task.scheduleEnabled = false;
    task.nextIntervalAt = null;
    task.dingtalkTarget = null;
    tasks.save(task);
  }

  /** 将角色请求字段应用到实体，并执行名称唯一性检查。 */
  private void applyRole(RoleEntity role, RoleSaveRequest body, boolean updating) {
    assignRole(role, body.groupId());
    String name = body.name().trim();
    if (roles.existsByDeletedFalseAndNameIgnoreCaseAndIdNot(name, role.id)) {
      throw new ConflictFailure("角色名称已存在。");
    }
    role.name = name;
    role.duty = body.duty().trim();
    role.enabled = body.enabled() == null ? !updating || role.enabled : body.enabled();
  }

  /** 将 SOP 请求字段和完整步骤列表应用到聚合根。 */
  private void applySop(SopEntity sop, SopSaveRequest body) {
    var orderedSteps = SopGraphValidator.ordered(body.editorGraph(), body.steps());
    assignSop(sop, body.groupId(), false);
    groups.validateMachines(
        body.groupId(),
        body.supervisorAgentId(),
        body.steps().stream().map(SopStepRequest::agentId).toList());
    sop.name = body.name().trim();
    sop.description = normalizeNullable(body.description());
    sop.supervisorAgentId = body.supervisorAgentId().trim();
    sop.failurePolicy = "stop";
    sop.supervisorTimeoutSec =
        integerInRange(body.supervisorTimeoutSec(), "supervisorTimeoutSec", 7200, 10, 7200);
    sop.maxRetryCount = integerInRange(body.maxRetryCount(), "maxRetryCount", 10, 0, 100);
    sop.advanceMode = normalizeNullable(body.advanceMode());
    if (sop.advanceMode == null) sop.advanceMode = "automatic";
    if (!ADVANCE_MODES.contains(sop.advanceMode)) {
      throw new IllegalArgumentException("advanceMode 只能是 automatic 或 semi_automatic。");
    }
    sop.handoffMode = normalizeNullable(body.handoffMode());
    if (sop.handoffMode == null) sop.handoffMode = "legacy_text";
    if (!HANDOFF_MODES.contains(sop.handoffMode)) {
      throw new IllegalArgumentException("handoffMode 只能是 legacy_text 或 cumulative_files。");
    }
    sop.defaultStepModel = normalizeNullable(body.defaultStepModel());
    if (sop.defaultStepModel == null) sop.defaultStepModel = defaultModel;
    validateModel(sop.defaultStepModel);
    if (body.enabled() != null) sop.enabled = body.enabled();
    sop.steps.clear();
    int position = 0;
    sop.editorGraphJson = body.editorGraph() == null ? null : json.write(body.editorGraph());
    for (SopStepRequest rawStep : orderedSteps) {
      var step = createStep(sop, rawStep, position++);
      if (body.editorGraph() != null) {
        for (var edge : body.editorGraph().edges()) {
          if (edge.source().equals(step.nodeKey)) {
            body.editorGraph().nodes().stream()
                .filter(n -> n.id().equals(edge.target()) && "acceptance".equals(n.type()))
                .findFirst()
                .ifPresent(n -> step.acceptanceJson = json.write(n.acceptance()));
          }
        }
      }
      sop.steps.add(step);
    }
  }

  /** 根据步骤请求创建一个已关联所属 SOP 和角色的步骤实体。 */
  private SopStepEntity createStep(SopEntity sop, SopStepRequest body, int position) {
    SopStepEntity step = new SopStepEntity();
    step.id = newId();
    step.nodeKey = body.nodeKey() == null ? step.id : body.nodeKey();
    step.sop = sop;
    step.positionNo = position;
    step.displayName = body.displayName().trim();
    step.role = findRole(body.roleId().trim());
    GroupService.same(sop.groupId, step.role.groupId);
    step.instruction = body.instruction().trim();
    step.expectedOutput = normalizeNullable(body.expectedOutput());
    if (step.expectedOutput == null) step.expectedOutput = DEFAULT_EXPECTED_OUTPUT;
    step.executorType = normalizeNullable(body.executorType());
    if (step.executorType == null) step.executorType = "local";
    if (!EXECUTOR_TYPES.contains(step.executorType)) {
      throw new IllegalArgumentException("执行位置只能是 local 或 remote。");
    }
    step.agentId = body.agentId().trim();
    step.workingDirectory = normalizeNullable(body.workingDirectory());
    step.permissionProfile = normalizeNullable(body.permissionProfile());
    if (step.permissionProfile == null) {
      step.permissionProfile =
          Boolean.TRUE.equals(body.writeEnabled()) ? "workspace_write" : "read_only";
    }
    if (!PERMISSION_PROFILES.contains(step.permissionProfile)) {
      throw new IllegalArgumentException(
          "permissionProfile 只能是 read_only、workspace_write、auto_review 或 full_access。");
    }
    step.writeEnabled = !"read_only".equals(step.permissionProfile);
    if (body.writeEnabled() != null && body.writeEnabled() != step.writeEnabled) {
      throw new IllegalArgumentException("permissionProfile 与 writeEnabled 字段矛盾。");
    }
    step.modelOverride = normalizeNullable(body.modelOverride());
    if (step.modelOverride != null) validateModel(step.modelOverride);
    step.timeoutSec = integerInRange(body.timeoutSec(), "timeoutSec", 1800, 10, 7200);
    addTags(step.skills, body.skills());
    addTags(step.mcps, body.mcps());
    return step;
  }

  /** 将任务定义请求字段应用到实体，并解析其关联 SOP。 */
  private void applyTask(TaskDefinitionEntity task, TaskDefinitionSaveRequest body) {
    if (Boolean.TRUE.equals(body.scheduleEnabled()))
      throw new IllegalArgumentException("定时配置已移至定时任务管理，请从新入口创建。");
    assignTask(task, body.groupId(), false);
    task.name = body.name().trim();
    task.objective = body.objective().trim();
    task.sop = findSop(body.sopId().trim());
    GroupService.same(task.groupId, task.sop.groupId);
    task.additionalNotes = normalizeNullable(body.additionalNotes());
    if (body.enabled() != null) task.enabled = body.enabled();
    String targetId = normalizeNullable(body.dingtalkTargetId());
    String currentTargetId = task.dingtalkTarget == null ? null : task.dingtalkTarget.id;
    if (!java.util.Objects.equals(currentTargetId, targetId)) {
      if (task.activeWorkflowId != null || task.dingtalkActiveWorkflowId != null) {
        throw new ConflictFailure("当前钉钉任务仍在运行，不能解除或更换通知对象。");
      }
      task.dingtalkTarget =
          targetId == null ? null : dingtalkTargets.requiredSelectable(targetId, task.id);
    }
    task.scheduleEnabled = false;
    task.nextIntervalAt = null;
    if (body.notifyDingTalk() != null) task.notifyDingTalk = body.notifyDingTalk();
    if (task.notifyDingTalk) {
      if (task.dingtalkTarget == null) {
        throw new IllegalArgumentException("启用钉钉通知时必须选择钉钉通知对象。");
      }
      task.dingtalkTarget = dingtalkTargets.requiredSelectable(task.dingtalkTarget.id, task.id);
    }
  }

  @Transactional
  public void assignGroups(String kind, List<String> ids, String groupId) {
    if (kind == null || !java.util.Set.of("roles", "sops", "tasks").contains(kind))
      throw new IllegalArgumentException("不支持的归组数据类型。");
    groups.lock();
    groups.require(groupId);
    if (ids == null
        || ids.isEmpty()
        || ids.size() > 200
        || ids.stream().anyMatch(id -> id == null || id.isBlank() || id.length() > 36))
      throw new IllegalArgumentException("请选择 1 至 200 条数据。");
    for (String id : ids.stream().distinct().sorted().toList()) {
      switch (kind) {
        case "roles" -> {
          RoleEntity r = findRole(id);
          assignRole(r, groupId);
          roles.save(r);
        }
        case "sops" -> {
          SopEntity s = findSop(id);
          assignSop(s, groupId, true);
          sops.save(s);
        }
        case "tasks" -> {
          TaskDefinitionEntity t = findTaskForUpdate(id);
          assignTask(t, groupId, true);
          tasks.save(t);
        }
        default -> throw new IllegalArgumentException("不支持的归组数据类型。");
      }
    }
  }

  private void assignRole(RoleEntity r, String id) {
    groups.require(id);
    if (r.groupId != null && !r.groupId.equals(id) && steps.existsByRoleIdAndSopDeletedFalse(r.id))
      throw new ConflictFailure("角色已被有效 SOP 引用，请先解除引用后再改组。");
    if (r.groupId == null
        && groups.count(
                "SELECT COUNT(*) FROM codex_sop_steps n JOIN codex_sop_sops s ON s.id=n.sop_id WHERE n.role_id=? AND s.deleted=FALSE AND s.group_id IS NOT NULL AND s.group_id<>?",
                r.id,
                id)
            > 0) throw new ConflictFailure("角色存在其他分组的 SOP 引用。");
    r.groupId = id;
  }

  private void assignSop(SopEntity s, String id, boolean validateExisting) {
    groups.require(id);
    if (s.groupId != null && !s.groupId.equals(id) && tasks.existsBySopIdAndDeletedFalse(s.id))
      throw new ConflictFailure("SOP 已被有效任务引用，请先解除引用后再改组。");
    if (validateExisting) {
      for (SopStepEntity step : s.steps) GroupService.same(id, step.role.groupId);
      groups.validateMachines(
          id, s.supervisorAgentId, s.steps.stream().map(n -> n.agentId).toList());
    }
    s.groupId = id;
  }

  private void assignTask(TaskDefinitionEntity t, String id, boolean validateExisting) {
    groups.require(id);
    boolean changed = !java.util.Objects.equals(t.groupId, id);
    if (changed && (t.activeWorkflowId != null || t.dingtalkActiveWorkflowId != null))
      throw new ConflictFailure("任务仍在运行，暂时不能归组或改组。");
    if (t.groupId != null
        && changed
        && groups.count(
                "SELECT COUNT(*) FROM codex_task_schedules WHERE task_definition_id=?", t.id)
            > 0) throw new ConflictFailure("任务存在定时规则，请先删除规则后再改组。");
    if (validateExisting) GroupService.same(id, t.sop.groupId);
    if (t.groupId == null && changed) schedules.resetAfterGrouping(t.id);
    t.groupId = id;
  }

  /** 根据 ID 查询角色，不存在时抛出领域未找到异常。 */
  private RoleEntity findRole(String id) {
    RoleEntity role = roles.findById(id).orElseThrow(() -> new NotFoundFailure("找不到角色：" + id));
    if (role.deleted) {
      throw new NotFoundFailure("找不到角色：" + id);
    }
    return role;
  }

  /** 根据 ID 查询 SOP，不存在时抛出领域未找到异常。 */
  private SopEntity findSop(String id) {
    SopEntity sop = sops.findById(id).orElseThrow(() -> new NotFoundFailure("找不到 SOP：" + id));
    if (sop.deleted) {
      throw new NotFoundFailure("找不到 SOP：" + id);
    }
    return sop;
  }

  /** 根据 ID 查询任务定义，并按调用场景决定是否接受软删除记录。 */
  private TaskDefinitionEntity findTask(String id, boolean includeDeleted) {
    TaskDefinitionEntity task =
        tasks.findById(id).orElseThrow(() -> new NotFoundFailure("找不到任务定义：" + id));
    if (task.deleted && !includeDeleted) {
      throw new NotFoundFailure("找不到任务定义：" + id);
    }
    return task;
  }

  /** 锁定未删除任务定义，避免配置保存覆盖并发写入的运行槽或定时领取日期。 */
  private TaskDefinitionEntity findTaskForUpdate(String id) {
    TaskDefinitionEntity task =
        tasks.findForUpdate(id).orElseThrow(() -> new NotFoundFailure("找不到任务定义：" + id));
    if (task.deleted) throw new NotFoundFailure("找不到任务定义：" + id);
    return task;
  }

  /** 生成不带大括号的随机 UUID 字符串。 */
  private static String newId() {
    return UUID.randomUUID().toString();
  }

  /** 将可空查询文本转换为去除首尾空白的字符串。 */
  private static String normalize(String value) {
    return value == null ? "" : value.trim();
  }

  /** 将可空文本去除首尾空白，并把空字符串转换为 {@code null}。 */
  private static String normalizeNullable(String value) {
    if (value == null) return null;
    String normalized = value.trim();
    return normalized.isEmpty() ? null : normalized;
  }

  /** 返回请求整数或默认值，并校验其是否位于允许区间。 */
  private static int integerInRange(
      Integer requestedValue, String field, int defaultValue, int min, int max) {
    int value = requestedValue == null ? defaultValue : requestedValue;
    if (value < min || value > max) {
      throw new IllegalArgumentException(field + " 必须在 " + min + " 到 " + max + " 之间。");
    }
    return value;
  }

  /** 校验模型标识是否在系统支持列表中。 */
  private static void validateModel(String model) {
    if (!SUPPORTED_MODELS.contains(model)) {
      throw new IllegalArgumentException("不支持的模型：" + model);
    }
  }

  /** 将非空标签清理后加入目标集合，自动去除重复值。 */
  private static void addTags(Set<String> target, Set<String> values) {
    if (values == null) return;
    values.forEach(
        value -> {
          String tag = value == null ? "" : value.trim();
          if (!tag.isEmpty()) target.add(tag);
        });
  }
}
