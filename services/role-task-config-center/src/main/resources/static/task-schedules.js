let scheduleRows=[];
let scheduleRenderVersion=0;
function scheduleCheckSummary(x){
  const labels={pending:"等待检查",checking:"检查中",passed:"检查通过",skipped:"条件不满足",failed:"检查失败"};
  if(x.waitingResources)return "等待空闲监督机和执行机，已合并为一次待调度请求。";
  if(!x.lastCheckStatus)return x.preCheck?.enabled?"执行前检查已启用，尚未检查":"未启用执行前检查";
  const time=x.lastCheckAt?new Date(x.lastCheckAt).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false}):"—";
  return `${x.preCheck?.enabled?"":"检查已停用 · "}最近检查（北京时间）：${time} · ${labels[x.lastCheckStatus]||"结果未知"} · ${x.lastCheckReason||""}`;
}
async function renderSchedules(){
  const version=++scheduleRenderVersion;
  $(".toolbar").classList.remove("hidden");
  $("#content").className="content schedule-content";
  $("#content").innerHTML='<div class="empty">正在加载定时任务…</div>';
  try {
    const rows=await api("/api/task-schedules?groupId="+encodeURIComponent(groupState.selected));
    if(state.page!=="schedules"||version!==scheduleRenderVersion)return;
    scheduleRows=rows;
    const beijing=v=>v?new Date(v).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false}):"—";
    $("#content").innerHTML=rows.length?rows.map(x=>`<article class="card"><div><h3>${esc(x.name)} ${status(x)} ${groupMark(x)}</h3><p>SOP：${esc(x.sopName)} · 任务定义：${esc(x.taskName)}</p><span class="meta">${x.mode==="daily"?`每天 ${esc(x.dailyTime)}`:`每隔 ${Number(x.intervalMinutes)} 分钟`} · 下次执行（北京时间）：${beijing(x.nextScheduleAt)}${x.available?"":" · 关联任务待归组、已停用或删除"}</span><p class="meta">${esc(scheduleCheckSummary(x))}</p></div><div class="actions"><button data-schedule-edit="${esc(x.id)}">编辑</button><button data-schedule-toggle="${esc(x.id)}" ${!x.enabled&&!x.available?"disabled":""}>${x.enabled?"停用":"启用"}</button><button data-schedule-delete="${esc(x.id)}">删除</button></div></article>`).join(""):'<div class="empty">暂无定时任务，点击右上角“新建定时任务”。</div>';
  } catch(e){if(state.page==="schedules"&&version===scheduleRenderVersion)$("#content").innerHTML=`<div class="empty">加载失败：${esc(e.message)}，请点击刷新重试。</div>`;}
}
async function openSchedule(x={}){
  const [sops,tasks]=await Promise.all([api("/api/sops"),api("/api/task-definitions")]);
  let dialog=$("#scheduleDialog");
  if(!dialog){dialog=document.createElement("dialog");dialog.id="scheduleDialog";document.body.append(dialog);}
  dialog.innerHTML=`<form id="scheduleForm"><button type="button" class="dialog-close" data-dialog-close aria-label="关闭弹窗" title="关闭">×</button><h2>${x.id?"编辑":"新建"}定时任务</h2><label>定时名称<input name="name" maxlength="160" required></label><label>选择 SOP<select name="sopId" required></select></label><label>选择任务定义<select name="taskDefinitionId" required></select></label><small data-schedule-hint>任务目标与钉钉通知沿用所选任务定义的配置。</small><label>时间规则<select name="mode"><option value="daily">每天定时</option><option value="interval">每隔指定分钟</option></select></label><label data-daily>每天执行时间（北京时间）<input name="dailyTime" type="time" step="60"></label><label data-interval>间隔分钟数<input name="intervalMinutes" type="number" min="5" max="1440" step="1" value="40"></label><label class="check"><input name="enabled" type="checkbox" checked>启用</label><p data-schedule-error role="alert"></p><footer><button type="button" data-dialog-close>取消</button><button class="primary" type="submit">保存</button></footer></form>`;
  const f=dialog.querySelector("form");
  f.querySelector("[data-schedule-error]").insertAdjacentHTML("beforebegin", `<label class="check"><input name="checkEnabled" type="checkbox">启用执行前检查</label><fieldset data-precheck class="hidden"><legend>执行前检查（Windows BAT/CMD）</legend><label>检查执行机<select name="checkAgentId"><option value="">正在加载执行机…</option></select></label><small data-check-machines-error></small><button type="button" data-check-reload>刷新执行机</button><label>脚本绝对路径<input name="checkScriptPath" maxlength="1024" placeholder="E:/ZJSG_Patch/AgentBug/jira_task.bat"></label><label>脚本参数<input name="checkArguments" maxlength="2048" placeholder="-user PlanAgent --executer Planner -check_path False"></label><label>超时（秒）<input name="checkTimeout" type="number" min="5" max="120" value="60"></label><small>脚本须已存在于所选执行机；使用该机器登记的工作目录和执行服务账号。需要机器已授权写入及完全访问。参数中有空格时用双引号包围，不支持变量展开、管道、重定向或命令连接符。退出码为 0 且首条非空输出为 TRUE 时运行，FALSE 时跳过；检查失败等待下次。</small></fieldset>`);
  const check=x.preCheck||{};
  f.checkEnabled.checked=!!check.enabled;
  f.checkScriptPath.value=check.scriptPath||"";
  f.checkArguments.value=check.arguments||"";
  f.checkTimeout.value=check.timeoutSeconds||60;
  let checkMachines=[],machinesLoaded=false;
  const updateMachines=()=>{
    const selected=f.checkAgentId.value||check.agentId||"";
    const rows=checkMachines.filter(a=>a.groupId===f.elements.groupId?.value&&a.capabilities?.includes("executor"));
    f.checkAgentId.innerHTML='<option value="">请选择同组执行机</option>'+rows.map(a=>`<option value="${esc(a.agentId)}">${esc(a.remark||a.name||a.agentId)}${a.enabled?"":"（已停用）"}</option>`).join("");
    if(selected&&!rows.some(a=>a.agentId===selected))f.checkAgentId.insertAdjacentHTML("beforeend",`<option value="${esc(selected)}" disabled>原执行机不可用，请重新选择</option>`);
    f.checkAgentId.value=selected;
  };
  const loadMachines=async()=>{
    try{const data=await api("/api/agents");checkMachines=data.agents||[];machinesLoaded=true;f.querySelector("[data-check-machines-error]").textContent="";updateMachines();}
    catch(e){machinesLoaded=false;f.querySelector("[data-check-machines-error]").textContent="执行机加载失败，请刷新重试。";}
  };
  const updateCheck=()=>{const enabled=f.checkEnabled.checked;f.querySelector("[data-precheck]").classList.toggle("hidden",!enabled);for(const name of ["checkAgentId","checkScriptPath","checkArguments","checkTimeout"]){f[name].disabled=!enabled;f[name].required=enabled&&name!=="checkArguments";}if(enabled&&!machinesLoaded)loadMachines();};
  f.checkEnabled.onchange=updateCheck;
  f.querySelector("[data-check-reload]").onclick=loadMachines;
  f.elements.name.value=x.name||"";
  f.sopId.innerHTML='<option value="">请选择 SOP</option>'+sops.filter(s=>s.enabled||s.id===x.sopId).map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  f.sopId.value=x.sopId||"";
  const updateTasks=()=>{
    const eligible=tasks.filter(t=>t.sopId===f.sopId.value&&(t.enabled||t.id===x.taskDefinitionId));
    f.taskDefinitionId.innerHTML='<option value="">请选择任务定义</option>'+eligible.map(t=>`<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
    f.querySelector("[data-schedule-hint]").textContent=f.sopId.value&&!eligible.length?"该 SOP 下没有可用任务定义，请先在任务定义中创建或启用。":"任务目标与钉钉通知沿用所选任务定义的配置。";
  };
  f.sopId.onchange=updateTasks;updateTasks();f.taskDefinitionId.value=x.taskDefinitionId||"";
  // 已保存规则固定关联任务；更换任务需删除后重新创建。
  if(x.id){f.sopId.disabled=true;f.taskDefinitionId.disabled=true;}
  f.mode.value=x.mode||"daily";f.dailyTime.value=x.dailyTime||"";f.intervalMinutes.value=x.intervalMinutes||40;f.enabled.checked=x.enabled!==false;
  const updateMode=()=>{const interval=f.mode.value==="interval";f.querySelector("[data-daily]").classList.toggle("hidden",interval);f.querySelector("[data-interval]").classList.toggle("hidden",!interval);f.dailyTime.disabled=interval;f.dailyTime.required=!interval;f.intervalMinutes.disabled=!interval;f.intervalMinutes.required=interval;};
  f.mode.onchange=updateMode;updateMode();
  f.onsubmit=async e=>{e.preventDefault();const button=f.querySelector('[type="submit"]');if(button.disabled)return;button.disabled=true;
    try{if(f.checkEnabled.checked&&!machinesLoaded)throw new Error("请先加载检查执行机列表。");await api(x.id?`/api/task-schedules/${encodeURIComponent(x.id)}`:"/api/task-schedules",{method:x.id?"PUT":"POST",body:JSON.stringify({groupId:f.elements.groupId.value,name:f.elements.name.value,sopId:f.sopId.value,taskDefinitionId:f.taskDefinitionId.value,mode:f.mode.value,dailyTime:f.mode.value==="daily"?f.dailyTime.value:null,intervalMinutes:f.mode.value==="interval"?Number(f.intervalMinutes.value):null,enabled:f.enabled.checked,preCheck:{enabled:f.checkEnabled.checked,agentId:f.checkAgentId.value,scriptPath:f.checkScriptPath.value,arguments:f.checkArguments.value,timeoutSeconds:Number(f.checkTimeout.value)}})});dialog.close();toast("定时任务已保存");await render();}
    catch(error){f.querySelector("[data-schedule-error]").textContent=error.message;}
    finally{button.disabled=false;}
  };bindGroupForm(f,x,id=>{updateMachines();const old=f.sopId.value;f.sopId.innerHTML='<option value="">请选择同组 SOP</option>'+sops.filter(s=>s.groupId===id&&(s.enabled||s.id===x.sopId)).map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");f.sopId.value=old;updateTasks();f.taskDefinitionId.value=x.taskDefinitionId||"";if(x.id){f.elements.groupId.disabled=true;f.sopId.disabled=true;f.taskDefinitionId.disabled=true;}});updateCheck();dialog.showModal();
}
document.addEventListener("click",async e=>{
  const b=e.target.closest("[data-schedule-edit],[data-schedule-toggle],[data-schedule-delete]");if(!b||b.disabled)return;
  const id=b.dataset.scheduleEdit||b.dataset.scheduleToggle||b.dataset.scheduleDelete,x=scheduleRows.find(row=>row.id===id);if(!x)return;
  b.disabled=true;
  try{if(b.dataset.scheduleEdit)await openSchedule(x);
    else if(b.dataset.scheduleDelete){if(!confirm(`删除定时任务“${x.name}”？已启动的运行不受影响。`))return;await api(`/api/task-schedules/${encodeURIComponent(id)}`,{method:"DELETE"});await render();}
    else{await api(`/api/task-schedules/${encodeURIComponent(id)}`,{method:"PUT",body:JSON.stringify({groupId:x.groupId,name:x.name,sopId:x.sopId,taskDefinitionId:x.taskDefinitionId,mode:x.mode,dailyTime:x.dailyTime,intervalMinutes:x.intervalMinutes,enabled:!x.enabled,preCheck:x.preCheck})});await render();}
  }catch(error){toast(error.message);}finally{b.disabled=false;}
});
