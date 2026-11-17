let jiraAccountRows=[];
let jiraAccountVersion=0;
async function renderJiraAccounts(){
  const version=++jiraAccountVersion,group=groupState.selected;
  $('#content').className='content role-grid jira-account-content';
  $('#content').innerHTML='<div class="empty">正在加载 Jira 账号…</div>';
  try{
    const data=await api('/api/jira-accounts?'+new URLSearchParams({groupId:group}));
    if(version!==jiraAccountVersion||state.page!=='jira-accounts'||group!==groupState.selected)return;
    jiraAccountRows=data.accounts;
    $('#content').innerHTML=jiraAccountRows.length?`<p class="jira-account-note">共 ${jiraAccountRows.length} 个账号 · SOP 仅可选择本组已启用且检测通过的账号。</p>${jiraAccountRows.map(jiraAccountCard).join('')}`:jiraAccountEmpty(group);
  }catch(error){if(version===jiraAccountVersion&&state.page==='jira-accounts')$('#content').innerHTML=`<div class="empty" role="alert">${esc(error.message)}，请刷新重试。</div>`}
}
function jiraAccountEmpty(group){
  return `<div class="empty role-empty"><div class="role-empty-icon" aria-hidden="true"><svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="7" y="9" width="34" height="30" rx="5"/><circle cx="19" cy="21" r="5"/><path d="M11 33c0-9 16-9 16 0M30 19h5M30 25h5M30 31h5"/></svg></div><h2>${group?'本分组尚无 Jira 账号':'尚未添加 Jira 账号'}</h2><p>添加账号并完成检测后，即可在 SOP 中选择使用。</p><button type="button" class="primary" data-jira-account-new>＋ 新增账号</button></div>`;
}
function jiraAccountCard(a){
  const label={passed:'检测通过',failed:'检测失败',untested:'尚未检测'}[a.testStatus]||'尚未检测';
  return `<article class="card role-card jira-account-card">
    <div class="jira-account-heading"><span class="jira-account-avatar" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="9" r="3"/><path d="M6.5 18c.5-5.5 10.5-5.5 11 0"/></svg></span><div class="jira-account-title"><h3 title="${esc(a.name)}">${esc(a.name)}</h3><span class="group-tag">${esc(groupName(a.groupId))}</span></div><span class="badge ${a.enabled?'':'off'}">${a.enabled?'启用中':'已停用'}</span></div>
    <dl class="jira-account-facts"><dt>服务地址</dt><dd>${esc(a.server)}</dd><dt>执行机 Jira 账号</dt><dd>${esc(a.executorAlias||'检测后自动映射')}</dd></dl>
    <div class="jira-account-check"><span class="jira-account-state ${a.testStatus==='passed'?'passed':a.testStatus==='failed'?'failed':'untested'}"><i aria-hidden="true"></i>${label}</span>${a.testMessage?`<p>${esc(a.testMessage)}</p>`:''}</div>
    <div class="role-card-footer"><div class="actions"><div class="jira-account-primary-actions"><button data-jira-account-edit="${esc(a.id)}">编辑账号</button><button class="jira-account-reminder" data-jira-reminder="${esc(a.id)}">缺任务提醒</button></div><div class="jira-account-secondary-actions"><button data-jira-account-test="${esc(a.id)}">检测连接</button><button data-jira-account-toggle="${esc(a.id)}">${a.enabled?'停用':'启用'}</button><button class="role-delete" data-jira-account-delete="${esc(a.id)}">删除</button></div></div></div>
  </article>`;
}
function editJiraAccount(account){
  let dialog=$('#jiraAccountDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='jiraAccountDialog';dialog.setAttribute('aria-labelledby','jiraAccountTitle');document.body.append(dialog)}
  dialog.innerHTML=`<form><button type="button" class="dialog-close" data-account-close aria-label="关闭账号配置" title="关闭">×</button><h2 id="jiraAccountTitle">${account?'编辑':'新增'} Jira 账号</h2><label>分组<select name="groupId" required ${account?'disabled':''}>${groupOptions(account?.groupId||concreteGroup())}</select></label><label>自定义名称<input name="name" maxlength="100" required value="${esc(account?.name||'')}"></label><label>Jira 地址<input name="server" type="url" maxlength="1000" required ${account?'readonly':''} value="${esc(account?.server||'')}"></label><label>Bearer Token<input name="token" type="password" autocomplete="new-password" maxlength="8192" ${account?'':'required'} placeholder="${account?'留空保留已保存的 Token':'请输入 Token'}"></label><label>执行机 Jira 账号<input readonly aria-readonly="true" value="${esc(account?.executorAlias||'')}" placeholder="检测后自动使用登录账号名"><small>执行机本地 Jira 配置需使用同名账号。</small></label><label class="check"><input name="enabled" type="checkbox" ${account?.enabled!==false?'checked':''}>启用</label><p role="alert" data-account-error></p><footer><button type="button" data-account-close>取消</button><button type="submit" class="primary">保存</button></footer></form>`;
  const form=dialog.querySelector('form');
  dialog.querySelectorAll('[data-account-close]').forEach(button=>button.onclick=()=>dialog.close());
  dialog.onclose=()=>{dialog.innerHTML=''};
  form.onsubmit=async event=>{
    event.preventDefault();
    const button=form.querySelector('[type=submit]');if(button.disabled)return;button.disabled=true;
    const field=name=>form.elements.namedItem(name);
    const body={groupId:field('groupId').value,name:field('name').value.trim(),server:field('server').value.trim(),token:field('token').value,enabled:field('enabled').checked,...(account?{version:account.version}:{})};
    try{await api('/api/jira-accounts'+(account?'/'+account.id:''),{method:account?'PUT':'POST',body:JSON.stringify(body)});field('token').value='';dialog.close();await renderJiraAccounts()}
    catch(error){if(dialog.open)form.querySelector('[data-account-error]').textContent=error.message}
    finally{button.disabled=false}
  };
  dialog.showModal();
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-jira-account-new],[data-jira-account-edit],[data-jira-account-test],[data-jira-account-toggle],[data-jira-account-delete]');
  if(!button||button.disabled)return;
  if(button.hasAttribute('data-jira-account-new')){editJiraAccount();return}
  const id=button.dataset.jiraAccountEdit||button.dataset.jiraAccountTest||button.dataset.jiraAccountToggle||button.dataset.jiraAccountDelete;
  const account=jiraAccountRows.find(a=>a.id===id);if(!account)return;
  if(button.dataset.jiraAccountEdit){editJiraAccount(account);return}
  if(button.dataset.jiraAccountDelete&&!confirm(`删除账号“${account.name}”？被引用的账号不能删除。`))return;
  button.disabled=true;
  try{
    if(button.dataset.jiraAccountTest){const result=await api(`/api/jira-accounts/${id}/test`,{method:'POST',body:JSON.stringify({version:account.version})});if(typeof sopJiraCatalogs!=='undefined')sopJiraCatalogs.clear();toast(result.optionsMessage||(result.testStatus==='passed'?'账号检测通过':result.testMessage||'账号检测失败'))}
    else if(button.dataset.jiraAccountDelete)await api(`/api/jira-accounts/${id}?version=${account.version}`,{method:'DELETE'});
    else await api(`/api/jira-accounts/${id}`,{method:'PUT',body:JSON.stringify({groupId:account.groupId,name:account.name,server:account.server,enabled:!account.enabled,version:account.version})});
    if(state.page==='jira-accounts')await renderJiraAccounts();
  }catch(error){toast(error.message)}finally{button.disabled=false}
});
function jiraAccountOptions(group,id){
  const choices=(state.jiraAccounts||[]).filter(a=>a.groupId===group&&a.enabled&&a.testStatus==='passed'&&a.executorAlias);
  return '<option value="">请选择中央 Jira 账号</option>'+(id&&!choices.some(a=>a.id===id)?`<option value="${esc(id)}" selected disabled>原账号不可用，请重新选择</option>`:'')+choices.map(a=>`<option value="${esc(a.id)}" ${a.id===id?'selected':''}>${esc(a.name)}（${esc(a.username||'未获取登录名')}）</option>`).join('');
}

function jiraReminderStatus(s){
  const names={disabled:'未启用或账号已停用',outside:'当前不在生效时段',unconfigured:'未配置采集范围',available:'有任务，已重置提醒',unknown:'等待新的完整采集结果',waiting:'无任务，计时中',sending:'正在发送',failed:'通知失败，需修正配置'};
  const time=v=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'暂无';
  return `${names[s.phase]||'等待检查'} · 上次发送：${time(s.lastSentAt)} · 下次预计：${time(s.nextAt)}${s.lastResult?' · '+s.lastResult:''}（北京时间）`;
}
function jiraReminderTargets(targets,type,keyword=''){
  const normalize=value=>String(value??'').normalize('NFKC').toLocaleLowerCase('zh-CN');
  const words=normalize(keyword).trim().split(/\s+/).filter(Boolean);
  return targets.filter(t=>t.targetType===type&&t.available&&(type!=='GROUP'||t.enabled)
    &&words.every(word=>normalize(`${t.displayName??''} ${t.departmentDisplay??''}`).includes(word)));
}
function jiraReminderOptions(targets,type,selected,keyword=''){
  const choices=jiraReminderTargets(targets,type,keyword);
  const current=jiraReminderTargets(targets,type).find(t=>t.id===selected);
  const label=t=>`${esc(t.displayName)}${t.departmentDisplay?'（'+esc(t.departmentDisplay)+'）':''}`;
  const retained=selected&&!choices.some(t=>t.id===selected)?`<option value="${esc(selected)}" selected>${current?label(current)+'（已选择）':'原对象不可用，请重新选择'}</option>`:'';
  return `<option value="">${keyword.trim()&&!choices.length?'未找到匹配人员':'请选择'}</option>`+retained+choices.map(t=>`<option value="${esc(t.id)}" ${t.id===selected?'selected':''}>${label(t)}</option>`).join('');
}
function jiraReminderWindowRow(w={start:'09:00',end:'18:00'}){
  return `<div data-reminder-window><input type="time" aria-label="开始时间" data-start required value="${esc(w.start)}"> — <input type="time" aria-label="结束时间" data-end required value="${esc(w.end)}"><button type="button" data-remove-window>移除</button></div>`;
}
async function editJiraReminder(account){
  let dialog=document.querySelector('#jiraReminderDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='jiraReminderDialog';document.body.append(dialog)}
  if(dialog.open)return;
  dialog.setAttribute('aria-label','Jira 缺任务提醒');
  dialog.innerHTML='<div class="jira-reminder-placeholder"><h2>缺任务提醒</h2><p role="status">正在加载提醒配置…</p><button type="button" class="dialog-close" data-reminder-close aria-label="关闭缺任务提醒" title="关闭">×</button></div>';
  dialog.querySelector('[data-reminder-close]').onclick=()=>dialog.close();
  dialog.showModal();
  const loadId=Symbol();dialog.loadId=loadId;
  try{
    const [config,targets]=await Promise.all([api(`/api/jira-accounts/${account.id}/empty-reminder`),api('/api/dingtalk/targets')]);
    if(!dialog.open||dialog.loadId!==loadId)return;
    dialog.innerHTML=`<form class="jira-reminder-form">
      <header class="jira-reminder-header"><h2>缺任务提醒</h2><p>${esc(account.name)}</p><button type="button" class="dialog-close" data-reminder-close aria-label="关闭缺任务提醒" title="关闭">×</button></header>
      <div class="jira-reminder-body">
        <label class="check jira-reminder-enabled"><input type="checkbox" name="enabled" ${config.enabled?'checked':''}>启用提醒</label>
        <div class="jira-reminder-targets"><label>通知群<select name="groupId">${jiraReminderOptions(targets,'GROUP',config.groupId)}</select></label><div class="jira-reminder-person"><label for="jiraReminderPerson">@ 人员</label><input type="search" data-reminder-person-search aria-label="搜索人员姓名或部门" placeholder="搜索姓名或部门" autocomplete="off"><select id="jiraReminderPerson" name="personId" aria-describedby="jiraReminderPersonMatches">${jiraReminderOptions(targets,'PERSON',config.personId)}</select><small id="jiraReminderPersonMatches" data-reminder-person-matches role="status"></small></div></div>
        <fieldset><legend>生效星期 <span>北京时间</span></legend><div class="jira-reminder-days">${['一','二','三','四','五','六','日'].map((d,i)=>`<label class="check"><input type="checkbox" name="day" value="${i+1}" ${config.days.includes(i+1)?'checked':''}>周${d}</label>`).join('')}</div></fieldset>
        <fieldset><legend>每日时段</legend><div data-windows>${config.windows.map(jiraReminderWindowRow).join('')}</div><button type="button" data-add-window>＋ 添加时段</button></fieldset>
        <div class="jira-reminder-status"><p data-reminder-status role="status">${esc(jiraReminderStatus(config.status))}</p><button type="button" data-refresh-status>刷新状态</button></div>
        <p role="alert" data-reminder-error></p>
      </div>
      <footer><button type="button" data-reminder-close>关闭</button><button type="submit" class="primary">保存</button></footer>
    </form>`;
    const form=dialog.querySelector('form'),error=form.querySelector('[data-reminder-error]');
    const enabled=form.elements.namedItem('enabled');
    const toggle=()=>{for(const name of ['groupId','personId'])form.elements.namedItem(name).required=enabled.checked};
    enabled.onchange=toggle;toggle();
    const personSearch=form.querySelector('[data-reminder-person-search]');
    personSearch.oninput=()=>{
      const person=form.elements.namedItem('personId'),selected=person.value;
      person.innerHTML=jiraReminderOptions(targets,'PERSON',selected,personSearch.value);
      person.value=selected;
      const count=jiraReminderTargets(targets,'PERSON',personSearch.value).length;
      form.querySelector('[data-reminder-person-matches]').textContent=personSearch.value.trim()?(count?`匹配 ${count} 位人员`:'未找到匹配人员'):'';
    };
    personSearch.onkeydown=event=>{if(event.key==='Enter'&&!event.isComposing)event.preventDefault()};
    form.querySelector('[data-reminder-close]').onclick=()=>dialog.close();
    form.querySelector('[data-add-window]').onclick=()=>{if(form.querySelectorAll('[data-reminder-window]').length>=24){error.textContent='最多配置24个时段。';return}form.querySelector('[data-windows]').insertAdjacentHTML('beforeend',jiraReminderWindowRow())};
    form.onclick=event=>{if(event.target.closest('[data-reminder-close]'))dialog.close();if(event.target.matches('[data-remove-window]'))event.target.closest('[data-reminder-window]').remove()};
    form.querySelector('[data-refresh-status]').onclick=async event=>{
      const button=event.currentTarget;button.disabled=true;
      try{const latest=await api(`/api/jira-accounts/${account.id}/empty-reminder`);if(dialog.open&&dialog.loadId===loadId)form.querySelector('[data-reminder-status]').textContent=jiraReminderStatus(latest.status)}catch(e){error.textContent=e.message}finally{button.disabled=false}
    };
    form.onsubmit=async event=>{
      event.preventDefault();const button=form.querySelector('[type=submit]');if(button.disabled)return;
      const body={version:config.version,enabled:enabled.checked,groupId:form.elements.namedItem('groupId').value,personId:form.elements.namedItem('personId').value,days:Array.from(form.querySelectorAll('[name=day]:checked'),el=>Number(el.value)),windows:Array.from(form.querySelectorAll('[data-reminder-window]'),el=>({start:el.querySelector('[data-start]').value,end:el.querySelector('[data-end]').value}))};
      if(!body.days.length||!body.windows.length){error.textContent='请选择生效星期并至少添加一个时段。';return}
      if(body.windows.some(w=>w.start===w.end)){error.textContent='开始与结束时间不能相同。';return}
      button.disabled=true;error.textContent='';
      try{const saved=await api(`/api/jira-accounts/${account.id}/empty-reminder`,{method:'PUT',body:JSON.stringify(body)});if(dialog.open&&dialog.loadId===loadId){config.version=saved.version;form.querySelector('[data-reminder-status]').textContent=jiraReminderStatus(saved.status);toast('提醒配置已保存')}}catch(e){if(dialog.open&&dialog.loadId===loadId)error.textContent=e.message}finally{button.disabled=false}
    };
  }catch(error){if(dialog.open&&dialog.loadId===loadId){dialog.innerHTML=`<div class="jira-reminder-placeholder"><h2>缺任务提醒</h2><p role="alert">${esc(error.message)}，请关闭后重试。</p><button type="button" class="dialog-close" aria-label="关闭缺任务提醒" title="关闭">×</button></div>`;dialog.querySelector('button').onclick=()=>dialog.close()}}
}
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-jira-reminder]');if(!button)return;
  const account=jiraAccountRows.find(a=>a.id===button.dataset.jiraReminder);if(account)editJiraReminder(account);
});
