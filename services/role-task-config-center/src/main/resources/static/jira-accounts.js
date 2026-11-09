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
  return `<article class="card role-card jira-account-card"><div class="role-card-heading"><h3 title="${esc(a.name)}">${esc(a.name)}</h3><span class="badge ${a.enabled?'':'off'}">${a.enabled?'启用中':'已停用'}</span></div><div class="role-card-group"><span class="group-tag">${esc(groupName(a.groupId))}</span></div><dl class="jira-account-facts"><dt>服务地址</dt><dd>${esc(a.server)}</dd><dt>执行机 Jira 账号</dt><dd>${esc(a.executorAlias||'检测后自动映射')}</dd></dl><div class="jira-account-check"><span class="machine-state ${a.testStatus==='passed'?'online':a.testStatus==='failed'?'offline':'unknown'}">${label}</span>${a.testMessage?`<p>${esc(a.testMessage)}</p>`:''}</div><div class="role-card-footer"><div class="actions"><button data-jira-account-edit="${esc(a.id)}">编辑</button><button data-jira-account-test="${esc(a.id)}">检测</button><button data-jira-account-toggle="${esc(a.id)}">${a.enabled?'停用':'启用'}</button><button class="role-delete" data-jira-account-delete="${esc(a.id)}">删除</button></div></div></article>`;
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
