const sopJiraCatalogs = new Map();
function sopJiraKey(d){return JSON.stringify([d.groupId,d.jiraConnectionId,d.jiraProject,d.jiraIssueType])}
function sopJiraSelected(value){return [...new Set((value||'').split(/[,，]/).map(v=>v.trim()).filter(Boolean))]}
function sopJiraChoices(field, values, selected){
  const matches=(value,item)=>value===item.id||value.toLowerCase()===item.name.toLowerCase()||value===item.key||(field==='jiraStatus'&&value==='待办'&&item.id==='10205');
  const choices=values.map(item=>({value:selected.find(v=>matches(v,item))||(field==='jiraRoles'?item.name:item.id),name:item.name,missing:false}));
  for(const value of selected)if(!choices.some(item=>item.value===value))choices.push({value,name:value,missing:true});
  return choices;
}
function sopJiraSelect(field,label,values,value,empty){
  const choices=sopJiraChoices(field,values,value?[value]:[]);
  return `<label>${label}<select data-sop-field="${field}" data-jira-select="${field}"><option value="">${empty}</option>${choices.map(item=>`<option value="${esc(item.value)}" ${item.value===value?'selected':''}>${esc(item.name)}${item.missing?'（待确认）':''}</option>`).join('')}</select></label>`;
}
function sopJiraMulti(field,label,values,value){
  const selected=sopJiraSelected(value),choices=sopJiraChoices(field,values,selected);
  const names=choices.filter(item=>selected.includes(item.value)).map(item=>item.name+(item.missing?'（待确认）':''));
  return `<div class="jira-option-field"><span>${label}</span><details class="jira-option-multi" data-jira-menu="${field}"><summary>${esc(names.join('、')||(field==='jiraIssueType'?'不限类型':'请选择'))}</summary><div role="group" aria-label="${label}">${choices.length?choices.map(item=>`<label><input type="checkbox" data-jira-choice="${field}" value="${esc(item.value)}" ${selected.includes(item.value)?'checked':''}>${esc(item.name)}${item.missing?'（待确认）':''}</label>`).join(''):'<small>暂无可用选项</small>'}</div></details></div>`;
}
function sopJiraOptionsBody(d){
  const entry=sopJiraCatalogs.get(sopJiraKey(d))||{},data=entry.data||{};
  const message=entry.loading?'正在读取 Jira 选项…':entry.error||data.rolesError||(!d.jiraConnectionId?'请先选择账号。':!d.jiraProject?'请选择项目。':'');
  const missing=entry.projects&&!entry.loading&&[
    ['jiraProject',entry.projects,d.jiraProject?[d.jiraProject]:[]],
    ['jiraIssueType',data.issueTypes||[],sopJiraSelected(d.jiraIssueType)],
    ['jiraStatus',data.statuses||[],sopJiraSelected(d.jiraStatus)],
    ['jiraRoles',data.roles||[],sopJiraSelected(d.jiraRoles)]
  ].some(([field,values,selected])=>sopJiraChoices(field,values,selected).some(item=>item.missing));
  return `<label>Jira 账号<select data-sop-field="jiraConnectionId" data-jira-select="jiraConnectionId">${jiraAccountOptions(d.groupId,d.jiraConnectionId)}</select><small>仅领取所选账号负责的工单。</small></label>
    ${sopJiraSelect('jiraProject','Jira 项目 *',entry.projects||[],d.jiraProject,'请选择项目')}
    ${sopJiraMulti('jiraIssueType','工单类型',data.issueTypes||[],d.jiraIssueType)}
    ${sopJiraMulti('jiraStatus','Jira 状态 *',data.statuses||[],d.jiraStatus)}
    ${sopJiraMulti('jiraRoles','处理角色 *',data.roles||[],d.jiraRoles)}
    <div class="jira-options-actions"><small>工单类型、状态、处理角色可多选；不选类型表示不限。</small><button type="button" data-jira-options-refresh ${!d.jiraConnectionId||entry.loading?'disabled':''}>刷新选项</button></div>
    ${message?`<p class="meta" role="status">${esc(message)}</p>`:''}
    ${missing?'<small>“待确认”为原配置值，请核对后选择。</small>':''}`;
}
function sopJiraOptionsHtml(d){return `${d.jiraJql&&!jiraFieldsFromQuery(d.jiraJql)?'<p class="meta">旧查询无法回填，请重新选择领取条件。</p>':''}<section class="jira-sop-options" data-jira-options>${sopJiraOptionsBody(d)}</section>`}
function refreshSopJiraOptionsView(d){
  if(state.sop.draft!==d)return;
  const panel=document.querySelector('[data-jira-options]');if(!panel)return;
  const active=document.activeElement;
  const focus=active&&panel.contains(active)?{field:active.dataset.jiraChoice||active.dataset.jiraSelect,value:active.value}:null;
  const open=[...panel.querySelectorAll('details[open]')].map(item=>item.dataset.jiraMenu);
  panel.innerHTML=sopJiraOptionsBody(d);
  panel.querySelectorAll('details').forEach(item=>{item.open=open.includes(item.dataset.jiraMenu)});
  if(focus?.field)[...panel.querySelectorAll('[data-jira-choice],[data-jira-select]')].find(item=>(item.dataset.jiraChoice||item.dataset.jiraSelect)===focus.field&&item.value===focus.value)?.focus();
}
async function loadSopJiraOptions(force=false){
  const d=state.sop.draft;if(!d?.jiraEnabled||!d.jiraConnectionId)return;
  const key=sopJiraKey(d),previous=sopJiraCatalogs.get(key);
  if(previous?.loading||(!force&&previous?.attempted&&Date.now()-previous.checkedAt<600000))return;
  const entry={...previous,loading:true,attempted:true,checkedAt:Date.now(),error:''};sopJiraCatalogs.set(key,entry);refreshSopJiraOptionsView(d);
  try{
    if(!(state.jiraAccounts||[]).some(a=>a.id===d.jiraConnectionId&&a.groupId===d.groupId&&a.enabled&&a.testStatus==='passed'))throw Error('请重新选择本组可用账号。');
    const root=await api(`/api/jira-accounts/${encodeURIComponent(d.jiraConnectionId)}/options?refresh=${force}`);
    entry.projects=root.projects;
    if(sopJiraKey(d)!==key)return;
    if(d.jiraProject){
      const project=root.projects.find(p=>p.id===d.jiraProject||p.key===d.jiraProject||p.name===d.jiraProject);
      if(!project)throw Error('原项目不在当前账号的项目列表中，请重新选择。');
      entry.data=await api(`/api/jira-accounts/${encodeURIComponent(d.jiraConnectionId)}/options?`+new URLSearchParams({project:project.key,issueType:d.jiraIssueType||'',refresh:String(force)}));
    }
  }catch(error){entry.error=error.message}
  finally{entry.loading=false;if(state.sop.draft&&sopJiraKey(state.sop.draft)===key)refreshSopJiraOptionsView(state.sop.draft)}
}
function handleSopJiraChange(event){
  const d=state.sop.draft;if(!d)return false;
  const choice=event.target.dataset.jiraChoice,field=event.target.dataset.jiraSelect;
  if(choice){
    const previous=sopJiraCatalogs.get(sopJiraKey(d));
    const values=new Set(sopJiraSelected(d[choice]));
    if(event.target.checked)values.add(event.target.value);else values.delete(event.target.value);
    d[choice]=[...values].join(', ');
    if(choice==='jiraIssueType'&&previous&&!sopJiraCatalogs.has(sopJiraKey(d)))sopJiraCatalogs.set(sopJiraKey(d),{projects:previous.projects,data:{issueTypes:previous.data?.issueTypes||[]}});
    syncDirtyUi();refreshSopJiraOptionsView(d);if(choice==='jiraIssueType')loadSopJiraOptions();return true;
  }
  if(field){d[field]=event.target.value;syncDirtyUi();refreshSopJiraOptionsView(d);loadSopJiraOptions();return true}
  return false;
}
document.addEventListener('click',event=>{if(event.target.closest('[data-jira-options-refresh]'))loadSopJiraOptions(true)});
