let runCatalogRequest = 0;
let runCatalogBusy = false;
const runCatalogFilters = (() => {
  const params = new URLSearchParams(location.search);
  return {name:params.get("name")||"",type:params.get("type")||"",startDate:params.get("startDate")||"",endDate:params.get("endDate")||"",page:Math.max(0,Number(params.get("runPage"))||0)};
})();
function saveRunCatalogFilters(){
  const url=new URL(location.href);
  url.searchParams.set("page","runs");
  for(const [key,value] of Object.entries(runCatalogFilters)){
    const param=key==="page"?"runPage":key;
    if(value)url.searchParams.set(param,value);else url.searchParams.delete(param);
  }
  history.replaceState(null,"",url);
}
async function renderRunCatalog(){
  saveRunCatalogFilters();
  loadGatewayReady();
  $("#search").closest(".toolbar").classList.add("hidden");
  $("#content").className="content run-catalog";
  const f=runCatalogFilters;
  $("#content").innerHTML=`<section class="run-catalog-panel">
    <form id="runCatalogForm" class="run-filters">
      <label>任务名称<input name="name" type="search" maxlength="255" value="${esc(f.name)}" placeholder="输入任务名称"></label>
      <label>任务种类<select name="type"><option value="">全部</option><option value="dingtalk">钉钉触发</option><option value="schedule">定时任务</option></select></label>
      <label>开始日期<input name="startDate" type="date" min="1970-01-01" max="9998-12-31" value="${esc(f.startDate)}"></label>
      <label>结束日期<input name="endDate" type="date" min="1970-01-01" max="9998-12-31" value="${esc(f.endDate)}"></label>
      <div class="actions"><button class="primary" type="submit">查询</button><button type="button" id="runReset">重置</button><button type="button" id="runRefresh">刷新</button></div>
    </form>
    <p id="runNotice" role="status" aria-live="polite"></p><div id="runRows" class="run-table-wrap"><div class="empty">正在加载运行记录…</div></div>
    <div id="runPagination" class="run-pagination"></div></section>`;
  $("#runCatalogForm").elements.type.value=f.type;
  $("#runCatalogForm").onsubmit=event=>{
    event.preventDefault();const form=event.currentTarget;
    if(form.elements.startDate.value&&form.elements.endDate.value&&form.elements.startDate.value>form.elements.endDate.value){toast("开始日期不能晚于结束日期。");return}
    for(const key of ["name","type","startDate","endDate"])f[key]=form.elements[key].value.trim();
    f.page=0;saveRunCatalogFilters();loadRunCatalog();
  };
  $("#runReset").onclick=()=>{Object.assign(f,{name:"",type:"",startDate:"",endDate:"",page:0});saveRunCatalogFilters();renderRunCatalog()};
  $("#runRefresh").onclick=()=>loadRunCatalog();
  await loadRunCatalog();
}
async function loadRunCatalog({background=false}={}){
  const request=++runCatalogRequest;runCatalogBusy=true;
  const container=$("#runRows");
  if(!background){container.innerHTML='<div class="empty">正在加载运行记录…</div>';$("#runPagination").innerHTML=""}
  $("#runRefresh").disabled=true;
  const current=()=>request===runCatalogRequest&&state.page==="runs"&&container===$("#runRows");
  try{
    const params=new URLSearchParams({...runCatalogFilters,groupId:groupState.selected,size:20});
    const result=await api(`/api/task-runs?${params}`);
    if(!current())return;
    if(runCatalogFilters.page>0&&!result.items.length){runCatalogFilters.page=Math.max(0,Math.floor((result.total-1)/20));saveRunCatalogFilters();return loadRunCatalog()}
    $("#runNotice").textContent=result.statusFresh?"每10秒自动刷新当前页。":"状态暂未更新，以下为最近已知状态。";
    const labels={submitting:"提交中",queued:"排队中",running:"运行中",completed:"已完成",failed:"出错",submit_failed:"出错",cancelling:"停止中",cancelled:"已停止"};
    const displayTime=value=>new Date(value).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit"});
    container.innerHTML=result.items.length?`<table class="run-table"><thead><tr><th>任务名称</th><th>启动时间</th><th>任务种类</th><th>运行状态</th><th>操作</th></tr></thead><tbody>${result.items.map(row=>`<tr><td>${esc(row.name||"历史任务（名称缺失）")} ${groupMark(row)}</td><td>${esc(displayTime(row.submittedAt))}</td><td>${row.triggerSource==="dingtalk"?"钉钉触发":"定时任务"}</td><td><span class="badge run-status-${Object.hasOwn(labels,row.status)?row.status:"unknown"}">${esc(labels[row.status]||"状态未知")}</span></td><td><a href="${esc(row.monitorUrl)}" target="_blank" rel="noopener">查看详情</a></td></tr>`).join("")}</tbody></table>`:'<div class="empty">暂无符合条件的运行记录</div>';
    $("#runPagination").innerHTML=`<span>共 ${Number(result.total)} 条 · 第 ${result.page+1} / ${Math.max(1,Math.ceil(result.total/20))} 页</span><div class="actions"><button id="runPrevious" ${result.page===0?"disabled":""}>上一页</button><button id="runNext" ${(result.page+1)*20>=result.total?"disabled":""}>下一页</button></div>`;
    for(const [id,delta] of [["runPrevious",-1],["runNext",1]])$("#"+id).onclick=()=>{runCatalogFilters.page+=delta;saveRunCatalogFilters();loadRunCatalog()};
  }catch(error){if(current()){$("#runNotice").textContent="运行记录加载失败，请点击刷新重试。";if(!background)container.innerHTML='<div class="empty">无法加载运行记录</div>'}}
  finally{if(request===runCatalogRequest)runCatalogBusy=false;if(current())$("#runRefresh").disabled=false}
}
setInterval(()=>{if(typeof state!=="undefined"&&state.page==="runs"&&!document.hidden&&!runCatalogBusy)loadRunCatalog({background:true})},10000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&state.page==="runs"&&!runCatalogBusy)loadRunCatalog({background:true})});
