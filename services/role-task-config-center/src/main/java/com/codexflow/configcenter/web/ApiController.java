package com.codexflow.configcenter.web;

import com.codexflow.configcenter.application.WorkflowRunService;
import com.codexflow.configcenter.domain.ConfigService;
import com.codexflow.configcenter.dto.RoleSaveRequest;
import com.codexflow.configcenter.dto.SopSaveRequest;
import com.codexflow.configcenter.dto.TaskDefinitionSaveRequest;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** 提供角色、SOP、任务定义和工作流运行操作的 HTTP API。 */
@RestController
@RequestMapping("/api")
public class ApiController {

  private final ConfigService service;
  private final WorkflowRunService workflowRuns;

  /** 注入配置领域服务和工作流运行应用服务。 */
  public ApiController(ConfigService service, WorkflowRunService workflowRuns) {
    this.service = service;
    this.workflowRuns = workflowRuns;
  }

  /** 按可选关键字查询角色列表。 */
  @GetMapping("/roles")
  public List<ObjectNode> roles(@RequestParam(name = "q", defaultValue = "") String query) {
    return service.listRoles(query);
  }

  /** 创建角色并返回 HTTP 201。 */
  @PostMapping("/roles")
  @ResponseStatus(HttpStatus.CREATED)
  public ObjectNode createRole(@Valid @RequestBody RoleSaveRequest body) {
    return service.createRole(body);
  }

  /** 根据角色 ID 和乐观锁版本更新角色。 */
  @PutMapping("/roles/{id}")
  public ObjectNode updateRole(@PathVariable String id, @Valid @RequestBody RoleSaveRequest body) {
    return service.updateRole(id, body);
  }

  /** 删除未被 SOP 引用的角色并返回 HTTP 204。 */
  @DeleteMapping("/roles/{id}")
  @ResponseStatus(HttpStatus.NO_CONTENT)
  public void deleteRole(@PathVariable String id) {
    service.deleteRole(id);
  }

  /** 按可选关键字查询 SOP 列表。 */
  @GetMapping("/sops")
  public List<ObjectNode> sops(@RequestParam(name = "q", defaultValue = "") String query) {
    return service.listSops(query);
  }

  /** 根据 ID 查询一个包含完整步骤的 SOP。 */
  @GetMapping("/sops/{id}")
  public ObjectNode sop(@PathVariable String id) {
    return service.getSop(id);
  }

  /** 创建 SOP 及其步骤并返回 HTTP 201。 */
  @PostMapping("/sops")
  @ResponseStatus(HttpStatus.CREATED)
  public ObjectNode createSop(@Valid @RequestBody SopSaveRequest body) {
    return service.createSop(body);
  }

  /** 根据 ID 更新 SOP 及其完整步骤集合。 */
  @PutMapping("/sops/{id}")
  public ObjectNode updateSop(@PathVariable String id, @Valid @RequestBody SopSaveRequest body) {
    return service.updateSop(id, body);
  }

  /** 删除未被任务定义引用的 SOP 并返回 HTTP 204。 */
  @DeleteMapping("/sops/{id}")
  @ResponseStatus(HttpStatus.NO_CONTENT)
  public void deleteSop(@PathVariable String id) {
    service.deleteSop(id);
  }

  /** 按可选关键字查询未软删除的任务定义。 */
  @GetMapping("/task-definitions")
  public List<ObjectNode> tasks(@RequestParam(name = "q", defaultValue = "") String query) {
    return service.listTasks(query);
  }

  /** 根据 ID 查询任务定义。 */
  @GetMapping("/task-definitions/{id}")
  public ObjectNode task(@PathVariable String id) {
    return service.getTask(id);
  }

  /** 创建任务定义并返回 HTTP 201。 */
  @PostMapping("/task-definitions")
  @ResponseStatus(HttpStatus.CREATED)
  public ObjectNode createTask(@Valid @RequestBody TaskDefinitionSaveRequest body) {
    return service.createTask(body);
  }

  /** 根据 ID 更新任务定义。 */
  @PutMapping("/task-definitions/{id}")
  public ObjectNode updateTask(
      @PathVariable String id, @Valid @RequestBody TaskDefinitionSaveRequest body) {
    return service.updateTask(id, body);
  }

  /** 复制指定任务定义并返回默认停用的副本。 */
  @PostMapping("/task-definitions/{id}/copy")
  @ResponseStatus(HttpStatus.CREATED)
  public ObjectNode copyTask(@PathVariable String id) {
    return service.copyTask(id);
  }

  /** 软删除指定任务定义并返回 HTTP 204。 */
  @DeleteMapping("/task-definitions/{id}")
  @ResponseStatus(HttpStatus.NO_CONTENT)
  public void deleteTask(@PathVariable String id) {
    service.deleteTask(id);
  }

  /** 使用任务定义最新配置提交运行并返回 HTTP 202。 */
  @PostMapping("/task-definitions/{id}/runs")
  public ResponseEntity<ObjectNode> run(@PathVariable String id) {
    return ResponseEntity.accepted().body(workflowRuns.runLatest(id));
  }

  /** 查询指定任务定义的运行历史。 */
  @GetMapping("/task-definitions/{id}/runs")
  public List<ObjectNode> runs(
      @PathVariable String id,
      @RequestParam(defaultValue = "false") boolean summary,
      @RequestParam(defaultValue = "0") int page,
      @RequestParam(defaultValue = "20") int size) {
    return summary ? workflowRuns.listRunSummaries(id, page, size) : workflowRuns.listRuns(id);
  }

  /** 按需读取运行的完整不可变快照。 */
  @GetMapping("/task-runs/{workflowId}")
  public ObjectNode runDetail(@PathVariable String workflowId) {
    return workflowRuns.runDetail(workflowId);
  }

  /** 取消指定工作流运行。 */
  @PostMapping("/task-runs/{workflowId}/cancel")
  public ObjectNode cancel(@PathVariable String workflowId) {
    return workflowRuns.cancel(workflowId);
  }

  /** 使用指定历史运行的冻结快照提交重试并返回 HTTP 202。 */
  @PostMapping("/task-runs/{workflowId}/retry")
  public ResponseEntity<ObjectNode> retry(@PathVariable String workflowId) {
    return ResponseEntity.accepted().body(workflowRuns.retry(workflowId));
  }

  /** 代理查询工作流网关的执行机列表。 */
  @GetMapping("/agents")
  public JsonNode agents() {
    return workflowRuns.agents();
  }

  /** 代理查询工作流网关的就绪状态。 */
  @GetMapping("/gateway/ready")
  public JsonNode ready() {
    return workflowRuns.ready();
  }
}
