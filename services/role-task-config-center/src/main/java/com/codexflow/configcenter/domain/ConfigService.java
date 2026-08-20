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
  private static final String DEFAULT_EXPECTED_OUTPUT = "完成本步骤，并返回清晰、完整且可验证的结果。";

  private final RoleRepository roles;
  private final SopRepository sops;
  private final SopStepRepository steps;
  private final TaskDefinitionRepository tasks;
  private final DomainJsonMapper json;
  private final String defaultModel;

  /** 注入配置数据访问组件、JSON 映射器和默认模型配置。 */
  ConfigService(
      RoleRepository roles,
      SopRepository sops,
      SopStepRepository steps,
      TaskDefinitionRepository tasks,
      DomainJsonMapper json,
      @Value("${codex.default-step-model:gpt-5.6-sol}") String defaultModel) {
    this.roles = roles;
    this.sops = sops;
    this.steps = steps;
    this.tasks = tasks;
    this.json = json;
    this.defaultModel = defaultModel;
  }

  /** 按可选关键字查询角色列表。 */
  @Transactional(readOnly = true)
  public List<ObjectNode> listRoles(String query) {
    return roles.findByNameContainingIgnoreCaseOrderByCreatedAtDesc(normalize(query)).stream()
        .map(json::role)
        .toList();
  }

  /** 创建角色并返回稳定的 API JSON。 */
  @Transactional
  public ObjectNode createRole(RoleSaveRequest body) {
    RoleEntity role = new RoleEntity();
    role.id = newId();
    applyRole(role, body, false);
    return json.role(roles.save(role));
  }

  /** 按乐观锁版本更新角色，避免并发编辑相互覆盖。 */
  @Transactional
  public ObjectNode updateRole(String id, RoleSaveRequest body) {
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

  /** 删除未被 SOP 步骤引用的角色。 */
  @Transactional
  public void deleteRole(String id) {
    findRole(id);
    if (steps.existsByRoleId(id)) {
      throw new ConflictFailure("角色已被 SOP 引用，只能停用。");
    }
    roles.deleteById(id);
  }

  /** 按可选关键字查询 SOP 列表。 */
  @Transactional(readOnly = true)
  public List<ObjectNode> listSops(String query) {
    return sops.findByNameContainingIgnoreCaseOrderByCreatedAtDesc(normalize(query)).stream()
        .map(json::sop)
        .toList();
  }

  /** 根据 ID 获取包含完整步骤的 SOP。 */
  @Transactional(readOnly = true)
  public ObjectNode getSop(String id) {
    return json.sop(findSop(id));
  }

  /** 创建 SOP 及其全部串行步骤。 */
  @Transactional
  public ObjectNode createSop(SopSaveRequest body) {
    SopEntity sop = new SopEntity();
    sop.id = newId();
    applySop(sop, body);
    return json.sop(sops.save(sop));
  }

  /** 使用请求中的完整步骤集合替换现有 SOP 内容。 */
  @Transactional
  public ObjectNode updateSop(String id, SopSaveRequest body) {
    SopEntity sop = findSop(id);
    applySop(sop, body);
    return json.sop(sops.save(sop));
  }

  /** 删除未被有效任务定义引用的 SOP。 */
  @Transactional
  public void deleteSop(String id) {
    findSop(id);
    if (tasks.existsBySopIdAndDeletedFalse(id)) {
      throw new ConflictFailure("SOP 已被任务定义引用，只能停用。");
    }
    sops.deleteById(id);
  }

  /** 查询未软删除且名称匹配的任务定义。 */
  @Transactional(readOnly = true)
  public List<ObjectNode> listTasks(String query) {
    return tasks
        .findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(normalize(query))
        .stream()
        .map(json::task)
        .toList();
  }

  /** 根据 ID 获取任务定义，历史场景允许读取已软删除记录。 */
  @Transactional(readOnly = true)
  public ObjectNode getTask(String id) {
    return json.task(findTask(id, true));
  }

  /** 创建可重复运行的任务定义。 */
  @Transactional
  public ObjectNode createTask(TaskDefinitionSaveRequest body) {
    TaskDefinitionEntity task = new TaskDefinitionEntity();
    task.id = newId();
    applyTask(task, body);
    return json.task(tasks.save(task));
  }

  /** 更新未软删除的任务定义。 */
  @Transactional
  public ObjectNode updateTask(String id, TaskDefinitionSaveRequest body) {
    TaskDefinitionEntity task = findTask(id, false);
    applyTask(task, body);
    return json.task(tasks.save(task));
  }

  /** 复制任务定义并将副本默认设为停用。 */
  @Transactional
  public ObjectNode copyTask(String id) {
    TaskDefinitionEntity source = findTask(id, true);
    TaskDefinitionEntity copy = new TaskDefinitionEntity();
    copy.id = newId();
    copy.name = source.name + "（副本）";
    copy.objective = source.objective;
    copy.sop = source.sop;
    copy.additionalNotes = source.additionalNotes;
    copy.enabled = false;
    return json.task(tasks.save(copy));
  }

  /** 通过设置删除标记和停用标记软删除任务定义。 */
  @Transactional
  public void deleteTask(String id) {
    TaskDefinitionEntity task = findTask(id, false);
    task.deleted = true;
    task.enabled = false;
    tasks.save(task);
  }

  /** 将角色请求字段应用到实体，并执行名称唯一性检查。 */
  private void applyRole(RoleEntity role, RoleSaveRequest body, boolean updating) {
    String name = body.name().trim();
    if (roles.existsByNameIgnoreCaseAndIdNot(name, role.id)) {
      throw new ConflictFailure("角色名称已存在。");
    }
    role.name = name;
    role.duty = body.duty().trim();
    role.enabled = body.enabled() == null ? !updating || role.enabled : body.enabled();
  }

  /** 将 SOP 请求字段和完整步骤列表应用到聚合根。 */
  private void applySop(SopEntity sop, SopSaveRequest body) {
    sop.name = body.name().trim();
    sop.description = normalizeNullable(body.description());
    sop.supervisorAgentId = "local";
    sop.failurePolicy = "stop";
    sop.supervisorTimeoutSec =
        integerInRange(body.supervisorTimeoutSec(), "supervisorTimeoutSec", 7200, 10, 7200);
    sop.defaultStepModel = normalizeNullable(body.defaultStepModel());
    if (sop.defaultStepModel == null) sop.defaultStepModel = defaultModel;
    validateModel(sop.defaultStepModel);
    if (body.enabled() != null) sop.enabled = body.enabled();

    sop.steps.clear();
    int position = 0;
    for (SopStepRequest rawStep : body.steps()) {
      sop.steps.add(createStep(sop, rawStep, position++));
    }
  }

  /** 根据步骤请求创建一个已关联所属 SOP 和角色的步骤实体。 */
  private SopStepEntity createStep(SopEntity sop, SopStepRequest body, int position) {
    SopStepEntity step = new SopStepEntity();
    step.id = newId();
    step.sop = sop;
    step.positionNo = position;
    step.displayName = body.displayName().trim();
    step.role = findRole(body.roleId().trim());
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
    step.writeEnabled = Boolean.TRUE.equals(body.writeEnabled());
    step.modelOverride = normalizeNullable(body.modelOverride());
    if (step.modelOverride != null) validateModel(step.modelOverride);
    step.timeoutSec = integerInRange(body.timeoutSec(), "timeoutSec", 1800, 10, 7200);
    addTags(step.skills, body.skills());
    addTags(step.mcps, body.mcps());
    return step;
  }

  /** 将任务定义请求字段应用到实体，并解析其关联 SOP。 */
  private void applyTask(TaskDefinitionEntity task, TaskDefinitionSaveRequest body) {
    task.name = body.name().trim();
    task.objective = body.objective().trim();
    task.sop = findSop(body.sopId().trim());
    task.additionalNotes = normalizeNullable(body.additionalNotes());
    if (body.enabled() != null) task.enabled = body.enabled();
  }

  /** 根据 ID 查询角色，不存在时抛出领域未找到异常。 */
  private RoleEntity findRole(String id) {
    return roles.findById(id).orElseThrow(() -> new NotFoundFailure("找不到角色：" + id));
  }

  /** 根据 ID 查询 SOP，不存在时抛出领域未找到异常。 */
  private SopEntity findSop(String id) {
    return sops.findById(id).orElseThrow(() -> new NotFoundFailure("找不到 SOP：" + id));
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
