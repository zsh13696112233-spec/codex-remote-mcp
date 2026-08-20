package com.example.codex.config;

import tools.jackson.databind.*;
import tools.jackson.databind.node.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;
import java.time.Instant;
import java.util.*;

@Service
public class ConfigService {
    static final Set<String> MODELS=Set.of("gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna");
    private final RoleRepository roles; private final SopRepository sops; private final SopStepRepository steps;
    private final TaskDefinitionRepository tasks; private final TaskRunRepository runs; private final GatewayClient gateway;
    private final ObjectMapper mapper; private final TransactionTemplate tx; private final String defaultModel; private final String monitorUrl;
    ConfigService(RoleRepository roles,SopRepository sops,SopStepRepository steps,TaskDefinitionRepository tasks,
                  TaskRunRepository runs,GatewayClient gateway,ObjectMapper mapper,TransactionTemplate tx,
                  @Value("${codex.default-step-model:gpt-5.6-sol}") String defaultModel,
                  @Value("${codex.monitor.base-url:http://127.0.0.1:8090}") String monitorUrl) {
        this.roles=roles;this.sops=sops;this.steps=steps;this.tasks=tasks;this.runs=runs;this.gateway=gateway;
        this.mapper=mapper;this.tx=tx;this.defaultModel=defaultModel;this.monitorUrl=monitorUrl.replaceAll("/+$","");
    }

    @Transactional(readOnly=true) List<ObjectNode> listRoles(String q){ return roles.findByNameContainingIgnoreCaseOrderByCreatedAtDesc(text(q)).stream().map(this::roleJson).toList(); }
    @Transactional ObjectNode createRole(JsonNode b){ var r=new RoleEntity();r.id=uuid();applyRole(r,b,false);return roleJson(roles.save(r)); }
    @Transactional ObjectNode updateRole(String id,JsonNode b){ var r=role(id); long expected=requiredLong(b,"version"); if(r.version!=expected)throw new ConflictFailure("角色已被其他操作修改，请刷新后重试。");applyRole(r,b,true);try{return roleJson(roles.saveAndFlush(r));}catch(OptimisticLockingFailureException e){throw new ConflictFailure("角色已被其他操作修改，请刷新后重试。");} }
    @Transactional void deleteRole(String id){ role(id);if(steps.existsByRoleId(id))throw new ConflictFailure("角色已被 SOP 引用，只能停用。");roles.deleteById(id); }
    private void applyRole(RoleEntity r,JsonNode b,boolean updating){String name=required(b,"name");if(roles.existsByNameIgnoreCaseAndIdNot(name,r.id))throw new ConflictFailure("角色名称已存在。");r.name=name;r.duty=required(b,"duty");r.enabled=b.has("enabled")?b.path("enabled").asBoolean():!updating||r.enabled;}

    @Transactional(readOnly=true) List<ObjectNode> listSops(String q){return sops.findByNameContainingIgnoreCaseOrderByCreatedAtDesc(text(q)).stream().map(this::sopJson).toList();}
    @Transactional(readOnly=true) ObjectNode getSop(String id){return sopJson(sop(id));}
    @Transactional ObjectNode createSop(JsonNode b){var s=new SopEntity();s.id=uuid();applySop(s,b);return sopJson(sops.save(s));}
    @Transactional ObjectNode updateSop(String id,JsonNode b){var s=sop(id);applySop(s,b);return sopJson(sops.save(s));}
    @Transactional void deleteSop(String id){sop(id);if(tasks.existsBySopIdAndDeletedFalse(id))throw new ConflictFailure("SOP 已被任务定义引用，只能停用。");sops.deleteById(id);}
    private void applySop(SopEntity s,JsonNode b){
        s.name=required(b,"name");s.description=optional(b,"description");s.supervisorAgentId="local";
        s.failurePolicy="stop";s.supervisorTimeoutSec=intRange(b,"supervisorTimeoutSec",7200,10,7200);
        s.defaultStepModel=b.path("defaultStepModel").asText(defaultModel);model(s.defaultStepModel);if(b.has("enabled"))s.enabled=b.path("enabled").asBoolean();
        var raw=b.path("steps");if(!raw.isArray()||raw.isEmpty())throw new IllegalArgumentException("SOP 至少需要一个步骤。");
        s.steps.clear();int pos=0;for(JsonNode n:raw){var st=new SopStepEntity();st.id=uuid();st.sop=s;st.positionNo=pos++;
            st.displayName=required(n,"displayName");st.role=role(required(n,"roleId"));st.instruction=required(n,"instruction");st.expectedOutput=optional(n,"expectedOutput");if(st.expectedOutput==null)st.expectedOutput="完成本步骤，并返回清晰、完整且可验证的结果。";
            st.executorType=n.path("executorType").asText("local");if(!Set.of("local","remote").contains(st.executorType))throw new IllegalArgumentException("执行位置只能是 local 或 remote。");
            st.agentId=required(n,"agentId");st.workingDirectory=optional(n,"workingDirectory");st.writeEnabled=n.path("writeEnabled").asBoolean(false);
            st.modelOverride=optional(n,"modelOverride");if(st.modelOverride!=null)model(st.modelOverride);st.timeoutSec=intRange(n,"timeoutSec",1800,10,7200);
            addTags(st.skills,n.path("skills"));addTags(st.mcps,n.path("mcps"));s.steps.add(st);}
    }

    @Transactional(readOnly=true) List<ObjectNode> listTasks(String q){return tasks.findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(text(q)).stream().map(this::taskJson).toList();}
    @Transactional(readOnly=true) ObjectNode getTask(String id){return taskJson(task(id,true));}
    @Transactional ObjectNode createTask(JsonNode b){var t=new TaskDefinitionEntity();t.id=uuid();applyTask(t,b);return taskJson(tasks.save(t));}
    @Transactional ObjectNode updateTask(String id,JsonNode b){var t=task(id,false);applyTask(t,b);return taskJson(tasks.save(t));}
    @Transactional ObjectNode copyTask(String id){var old=task(id,true);var t=new TaskDefinitionEntity();t.id=uuid();t.name=old.name+"（副本）";t.objective=old.objective;t.sop=old.sop;t.additionalNotes=old.additionalNotes;t.enabled=false;return taskJson(tasks.save(t));}
    @Transactional void deleteTask(String id){var t=task(id,false);t.deleted=true;t.enabled=false;tasks.save(t);}
    private void applyTask(TaskDefinitionEntity t,JsonNode b){t.name=required(b,"name");t.objective=required(b,"objective");t.sop=sop(required(b,"sopId"));t.additionalNotes=optional(b,"additionalNotes");if(b.has("enabled"))t.enabled=b.path("enabled").asBoolean();}

    ObjectNode runLatest(String taskId){Prepared p=tx.execute(status->prepareLatest(taskId));return submit(p);}
    ObjectNode retry(String workflowId){Prepared p=tx.execute(status->prepareRetry(workflowId));return submit(p);}
    ObjectNode cancel(String workflowId){var response=gateway.post("/workflows/"+workflowId+"/cancel",null);tx.executeWithoutResult(s->{var r=run(workflowId);r.status=response.path("status").asText("cancelled");r.gatewayResponseJson=write(response);r.updatedAt=Instant.now();runs.save(r);});var out=tx.execute(s->runJson(run(workflowId)));out.set("gatewayResponse",response);return out;}
    List<ObjectNode> listRuns(String taskId){
        var values=tx.execute(s->{task(taskId,true);return new ArrayList<>(runs.findByTaskDefinitionIdOrderBySubmittedAtDesc(taskId).stream().map(this::runJson).toList());});
        for(var value:values){String status=value.path("status").asText();if(!Set.of("submitting","queued","running","cancelling").contains(status))continue;try{var live=gateway.get("/workflows/"+value.path("workflowId").asText());String liveStatus=live.path("status").asText(status);value.put("status",liveStatus);value.set("live",live);tx.executeWithoutResult(s->{var r=run(value.path("workflowId").asText());r.status=liveStatus;r.gatewayResponseJson=write(live);r.updatedAt=Instant.now();runs.save(r);});}catch(RuntimeException ignored){/* 网关不可用时保留最后一次已知状态。 */}}
        return values;
    }
    JsonNode agents(){return gateway.get("/agents");} JsonNode ready(){return gateway.get("/readyz");}

    private Prepared prepareLatest(String id){var t=task(id,false);if(!t.enabled)throw new ConflictFailure("任务定义已停用。");if(!t.sop.enabled)throw new ConflictFailure("所选 SOP 已停用。");String workflowId=uuid();ObjectNode payload=workflowPayload(t,workflowId);ObjectNode snapshot=taskSnapshot(t,payload);return persistPrepared(t,null,payload,snapshot);}
    private Prepared prepareRetry(String oldId){var old=run(oldId);ObjectNode payload=(ObjectNode)read(old.submittedJson);String newId=uuid();payload.put("workflowId",newId);ObjectNode snapshot=(ObjectNode)read(old.snapshotJson);snapshot.put("workflowId",newId);snapshot.put("sourceWorkflowId",oldId);snapshot.set("submittedJson",payload.deepCopy());return persistPrepared(old.taskDefinition,oldId,payload,snapshot);}
    private Prepared persistPrepared(TaskDefinitionEntity task,String source,ObjectNode payload,ObjectNode snapshot){var r=new TaskRunEntity();r.workflowId=payload.path("workflowId").asText();r.taskDefinition=task;r.sourceWorkflowId=source;r.status="submitting";r.snapshotJson=write(snapshot);r.submittedJson=write(payload);r.submittedAt=Instant.now();r.updatedAt=r.submittedAt;runs.saveAndFlush(r);return new Prepared(r.workflowId,payload);}
    private ObjectNode submit(Prepared p){try{var response=gateway.post("/workflows",p.payload);tx.executeWithoutResult(s->{var r=run(p.workflowId);r.status=response.path("status").asText("queued");r.gatewayResponseJson=write(response);r.updatedAt=Instant.now();runs.save(r);});var out=tx.execute(s->runJson(run(p.workflowId)));out.set("gatewayResponse",response);out.put("monitorUrl",monitorUrl+"/?workflowId="+p.workflowId);return out;}catch(RuntimeException e){tx.executeWithoutResult(s->{var r=run(p.workflowId);r.status="submit_failed";r.errorMessage=e.getMessage();r.updatedAt=Instant.now();runs.save(r);});throw e;}}

    private ObjectNode workflowPayload(TaskDefinitionEntity t,String workflowId){if(t.sop.steps.isEmpty())throw new ConflictFailure("所选 SOP 没有可执行步骤，请先编辑并保存 SOP。");var root=mapper.createObjectNode();root.put("workflowId",workflowId);root.put("name",t.name);root.put("supervisorAgentId",t.sop.supervisorAgentId);root.put("failurePolicy","stop");root.put("supervisorTimeoutSec",t.sop.supervisorTimeoutSec);var nodes=root.putArray("nodes");String previous=null;int i=1;for(var st:t.sop.steps){var n=nodes.addObject();n.put("id",st.id);n.put("displayName",st.displayName);n.put("roleName",st.role.name);var ex=n.putObject("executor");ex.put("type",st.executorType);ex.put("agentId",st.agentId);n.put("prompt",basePrompt(t,st,i));var deps=n.putArray("dependsOn");if(previous!=null)deps.add(previous);previous=st.id;if(st.workingDirectory!=null)n.put("cwd",st.workingDirectory);n.put("write",st.writeEnabled);n.put("model",st.modelOverride==null?t.sop.defaultStepModel:st.modelOverride);n.put("timeoutSec",st.timeoutSec);i++;}return root;}
    private String basePrompt(TaskDefinitionEntity t,SopStepEntity s,int i){String objective=t.objective+(t.additionalNotes==null?"":"\n\n补充说明：\n"+t.additionalNotes);return "任务名称：\n"+t.name+"\n\n任务目标：\n"+objective+"\n\n你当前负责：\n第"+i+"步："+s.displayName+"\n\n你的角色：\n"+s.role.name+"\n\n你的职责：\n"+s.role.duty+"\n\n本步骤执行要求：\n"+s.instruction+"\n\n期望输出：\n"+s.expectedOutput+"\n\n请只完成当前步骤，不要代替其他步骤执行。\n请使用普通用户容易理解的语言返回结果。";}
    private ObjectNode taskSnapshot(TaskDefinitionEntity t,ObjectNode submitted){var o=taskJson(t);o.put("workflowId",submitted.path("workflowId").asText());o.set("sop",sopJson(t.sop));o.set("submittedJson",submitted.deepCopy());return o;}

    private ObjectNode roleJson(RoleEntity r){var o=mapper.createObjectNode();o.put("id",r.id);o.put("name",r.name);o.put("duty",r.duty);o.put("enabled",r.enabled);o.put("version",r.version);times(o,r.createdAt,r.updatedAt);return o;}
    private ObjectNode sopJson(SopEntity s){var o=mapper.createObjectNode();o.put("id",s.id);o.put("name",s.name);put(o,"description",s.description);o.put("supervisorAgentId",s.supervisorAgentId);o.put("failurePolicy",s.failurePolicy);o.put("supervisorTimeoutSec",s.supervisorTimeoutSec);o.put("defaultStepModel",s.defaultStepModel);o.put("enabled",s.enabled);var a=o.putArray("steps");for(var st:s.steps){var n=a.addObject();n.put("id",st.id);n.put("order",st.positionNo+1);n.put("displayName",st.displayName);n.put("roleId",st.role.id);n.put("roleName",st.role.name);n.put("roleDuty",st.role.duty);n.put("instruction",st.instruction);n.put("expectedOutput",st.expectedOutput);n.put("executorType",st.executorType);n.put("agentId",st.agentId);put(n,"workingDirectory",st.workingDirectory);n.put("writeEnabled",st.writeEnabled);put(n,"modelOverride",st.modelOverride);n.put("effectiveModel",st.modelOverride==null?s.defaultStepModel:st.modelOverride);n.put("timeoutSec",st.timeoutSec);array(n,"skills",st.skills);array(n,"mcps",st.mcps);}times(o,s.createdAt,s.updatedAt);return o;}
    private ObjectNode taskJson(TaskDefinitionEntity t){var o=mapper.createObjectNode();o.put("id",t.id);o.put("name",t.name);o.put("objective",t.objective);o.put("sopId",t.sop.id);o.put("sopName",t.sop.name);put(o,"additionalNotes",t.additionalNotes);o.put("enabled",t.enabled);o.put("deleted",t.deleted);times(o,t.createdAt,t.updatedAt);return o;}
    private ObjectNode runJson(TaskRunEntity r){var o=mapper.createObjectNode();o.put("workflowId",r.workflowId);o.put("taskDefinitionId",r.taskDefinition.id);o.put("monitorUrl",monitorUrl+"/?workflowId="+r.workflowId);put(o,"sourceWorkflowId",r.sourceWorkflowId);o.put("status",r.status);o.set("snapshot",read(r.snapshotJson));o.set("submittedJson",read(r.submittedJson));if(r.gatewayResponseJson!=null)o.set("gatewayResponse",read(r.gatewayResponseJson));put(o,"error",r.errorMessage);o.put("submittedAt",r.submittedAt.toString());o.put("updatedAt",r.updatedAt.toString());return o;}

    private RoleEntity role(String id){return roles.findById(id).orElseThrow(()->new NotFoundFailure("找不到角色："+id));}
    private SopEntity sop(String id){return sops.findById(id).orElseThrow(()->new NotFoundFailure("找不到 SOP："+id));}
    private TaskDefinitionEntity task(String id,boolean includeDeleted){var t=tasks.findById(id).orElseThrow(()->new NotFoundFailure("找不到任务定义："+id));if(t.deleted&&!includeDeleted)throw new NotFoundFailure("找不到任务定义："+id);return t;}
    private TaskRunEntity run(String id){return runs.findById(id).orElseThrow(()->new NotFoundFailure("找不到运行记录："+id));}
    private static String uuid(){return UUID.randomUUID().toString();} private static String text(String s){return s==null?"":s.trim();}
    private static String required(JsonNode n,String f){String v=n.path(f).asText("").trim();if(v.isEmpty())throw new IllegalArgumentException(f+" 不能为空。");return v;}
    private static String optional(JsonNode n,String f){String v=n.path(f).asText("").trim();return v.isEmpty()?null:v;}
    private static long requiredLong(JsonNode n,String f){if(!n.has(f)||!n.path(f).canConvertToLong())throw new IllegalArgumentException(f+" 必须提供。");return n.path(f).asLong();}
    private static int intRange(JsonNode n,String f,int d,int min,int max){int v=n.has(f)?n.path(f).asInt():d;if(v<min||v>max)throw new IllegalArgumentException(f+" 必须在 "+min+" 到 "+max+" 之间。");return v;}
    private static void model(String m){if(!MODELS.contains(m))throw new IllegalArgumentException("不支持的模型："+m);}
    private static void addTags(Set<String> out,JsonNode n){if(n.isArray())n.forEach(x->{String v=x.asText("").trim();if(!v.isEmpty())out.add(v);});}
    private static void times(ObjectNode o,Instant c,Instant u){if(c!=null)o.put("createdAt",c.toString());if(u!=null)o.put("updatedAt",u.toString());}
    private static void put(ObjectNode o,String f,String v){if(v==null)o.putNull(f);else o.put(f,v);} private static void array(ObjectNode o,String f,Collection<String> c){var a=o.putArray(f);c.forEach(a::add);}
    private String write(JsonNode n){try{return mapper.writeValueAsString(n);}catch(Exception e){throw new IllegalStateException(e);}} private JsonNode read(String s){try{return mapper.readTree(s);}catch(Exception e){throw new IllegalStateException(e);}}
    private record Prepared(String workflowId,ObjectNode payload){}
}
