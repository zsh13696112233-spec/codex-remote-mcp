const groupUrl=new URL(location.href);
if(groupUrl.searchParams.get('groupId')==='unassigned'){groupUrl.searchParams.delete('groupId');history.replaceState(null,'',groupUrl)}
const groupState={selected:groupUrl.searchParams.get('groupId')||'',items:[],error:''};
const groupedPages=['roles','sops','tasks','schedules','runs','machines'];
function concreteGroup(){return groupState.items.some(g=>g.id===groupState.selected)?groupState.selected:''}
function groupMatches(x){return !groupState.selected||(groupState.selected==='unassigned'?!x.groupId:x.groupId===groupState.selected)}
function visibleSops(){return state.sops.filter(groupMatches)}
function groupName(id){return id?(groupState.items.find(g=>g.id===id)?.name||'分组暂不可用'):'待归组'}
function groupOptions(id='',placeholder='请先选择分组'){return `<option value="">${placeholder}</option>`+groupState.items.map(g=>`<option value="${esc(g.id)}" ${id===g.id?'selected':''}>${esc(g.name)}</option>`).join('')}
function groupMark(x){return `<span class="group-tag">${esc(x.groupName||groupName(x.groupId))}</span>`}
function groupCheckbox(x){return `<input type="checkbox" data-group-item="${esc(x.id)}" aria-label="选择${esc(x.name)}归组">`}
async function loadGroups(){
  try{const result=await api('/api/groups');groupState.items=result.groups||[];groupState.error=''}
  catch(error){groupState.error='分组暂时无法加载，请刷新重试。'}
}
function renderGroupSidebar(){
  const panel=$('#groupSidebar'),active=groupedPages.includes(state.page);
  panel.hidden=!active;$('.group-layout').classList.toggle('has-groups',active);
  const countKey={roles:'roleCount',sops:'sopCount',tasks:'taskCount',schedules:'scheduleCount',machines:'machineCount'}[state.page];
  const rows=[{id:'',name:'全部分组'},...groupState.items];
  panel.innerHTML=`<div class="group-sidebar-title"><strong>分组</strong><button data-open-groups title="管理分组">管理</button></div><select class="group-mobile" aria-label="筛选分组">${rows.map(g=>`<option value="${esc(g.id)}" ${g.id===groupState.selected?'selected':''}>${esc(g.name)}</option>`).join('')}</select><div class="group-sidebar-list">${rows.map(g=>`<button data-select-group="${esc(g.id)}" class="${g.id===groupState.selected?'active':''}" aria-pressed="${g.id===groupState.selected}"><span>${esc(g.name)}</span>${countKey&&g[countKey]!=null?`<small>${g[countKey]}</small>`:''}</button>`).join('')}</div>${groupState.error?`<p role="alert">${esc(groupState.error)}</p>`:''}`;
  panel.querySelector('select').onchange=e=>selectGroup(e.target.value).catch(error=>toast(error.message));
  const actions=$('#groupActions');actions.hidden=!['roles','sops','tasks'].includes(state.page);
  actions.innerHTML='<span>选中条目后可统一归组；已有引用时须先处理关联。</span><button data-assign-group>归组 / 改组</button>';
}
async function selectGroup(id){
  if(state.page==='sops'&&!confirmDiscard()){renderGroupSidebar();return false}
  groupState.selected=id;state.sop.draft=null;state.sop.baseline='';state.sop.selectedNodeId=null;
  runCatalogFilters.page=0;runCatalogRequest++;scheduleRenderVersion++;
  $('#search').value='';const url=new URL(location.href);if(id)url.searchParams.set('groupId',id);else url.searchParams.delete('groupId');url.searchParams.delete('runPage');history.replaceState(null,'',url);
  await render();return true;
}
async function renderGroups(){
  $('#search').closest('.toolbar').classList.add('hidden');$('#content').className='content group-catalog';
  if(groupState.error){$('#content').innerHTML=`<div class="empty">${esc(groupState.error)}<button data-group-retry>重新加载</button></div>`;return}
  const cols=[['roleCount','角色','roles'],['sopCount','SOP','sops'],['taskCount','任务定义','tasks'],['scheduleCount','定时规则','schedules'],['machineCount','机器','machines']];
  $('#content').innerHTML=`<div class="group-intro"><h2>按分组组织整套业务</h2><p>角色、工作流、任务与机器严格同组。点击数量，查看该组的数据。</p></div><div class="group-table-wrap"><table class="group-table"><thead><tr><th>分组名称</th>${cols.map(c=>`<th>${c[1]}</th>`).join('')}<th>操作</th></tr></thead><tbody>${groupState.items.map(g=>`<tr><td><strong>${esc(g.name)}</strong></td>${cols.map(c=>`<td><button class="group-count" data-group-jump="${c[2]}" data-id="${esc(g.id)}">${Number(g[c[0]])||0}</button></td>`).join('')}<td><div class="actions"><button data-group-edit="${esc(g.id)}">重命名</button><button data-group-delete="${esc(g.id)}">删除</button></div></td></tr>`).join('')||'<tr><td colspan="7">暂无分组，请点击“新建分组”。</td></tr>'}</tbody></table></div>`;
}
function groupDialog(title,html,onSave){
  let d=$('#sharedGroupDialog');if(!d){d=document.createElement('dialog');d.id='sharedGroupDialog';document.body.append(d)}
  d.innerHTML=`<form><h2>${esc(title)}</h2>${html}<p role="alert"></p><footer><button type="button" data-dialog-close>取消</button><button type="submit" class="primary">确定</button></footer></form>`;
  d.querySelector('form').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,b=f.querySelector('[type=submit]');if(b.disabled)return;b.disabled=true;try{await onSave(f);d.close()}catch(error){f.querySelector('[role=alert]').textContent=error.message}finally{b.disabled=false}};d.showModal();
}
function editGroup(id){const g=groupState.items.find(x=>x.id===id);groupDialog(g?'重命名分组':'新建分组',`<label>分组名称<input name="groupName" maxlength="100" required value="${esc(g?.name||'')}"></label>`,async f=>{await api(g?`/api/groups/${encodeURIComponent(g.id)}`:'/api/groups',{method:g?'PUT':'POST',body:JSON.stringify({name:f.elements.groupName.value.trim()})});await render()})}
function chooseGroup(next){if(concreteGroup())return next(concreteGroup());groupDialog('先选择分组',`<label>所属分组<select name="groupId" required>${groupOptions()}</select></label><p>选定分组后继续配置。</p>`,async f=>{const id=f.elements.groupId.value;$('#sharedGroupDialog').close();await selectGroup(id);next(id)})}
function bindGroupForm(form,x,onChange=()=>{}){
  form.querySelector('[data-form-group]')?.remove();
  const field=document.createElement('label');field.dataset.formGroup='';field.innerHTML=`所属分组 *<select name="groupId" required>${groupOptions(x.id?x.groupId:concreteGroup())}</select>`;form.querySelector('h2').after(field);
  const update=()=>{const selected=form.elements.groupId.value;form.querySelectorAll('input,textarea,select,button[type=submit],button.primary').forEach(el=>{if(el===form.elements.groupId||el.type==='hidden'||el.type==='button')return;if(!selected){if(!el.disabled){el.dataset.groupBlocked='true';el.disabled=true}}else if(el.dataset.groupBlocked){el.disabled=false;delete el.dataset.groupBlocked}});onChange(selected)};
  field.querySelector('select').onchange=update;update();
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('[data-group-retry],[data-select-group],[data-open-groups],[data-group-jump],[data-group-edit],[data-group-delete],[data-assign-group]');if(!b||b.disabled)return;
  try{
    if(b.hasAttribute('data-group-retry'))return await render();
    if(b.hasAttribute('data-select-group'))return await selectGroup(b.dataset.selectGroup);
    if(b.hasAttribute('data-open-groups'))return document.querySelector('nav [data-page=groups]').click();
    if(b.dataset.groupJump){if(!await selectGroup(b.dataset.id))return;return document.querySelector(`nav [data-page="${b.dataset.groupJump}"]`).click()}
    if(b.dataset.groupEdit)return editGroup(b.dataset.groupEdit);
    if(b.dataset.groupDelete){if(!confirm('仅无配置、机器和运行历史引用的空分组可删除，确定删除？'))return;b.disabled=true;await api(`/api/groups/${encodeURIComponent(b.dataset.groupDelete)}`,{method:'DELETE'});await render();return}
    if(b.hasAttribute('data-assign-group')){const ids=[...document.querySelectorAll('[data-group-item]:checked')].map(x=>x.dataset.groupItem);if(!ids.length){toast('请先勾选需要归组的数据。');return}if(state.page==='sops'&&!confirmDiscard())return;const kind=state.page;groupDialog(`为 ${ids.length} 条数据指定分组`,`<label>目标分组<select name="groupId" required>${groupOptions(concreteGroup())}</select></label><p>整批校验通过后保存，关联不符时整批保持原样。</p>`,async f=>{await api('/api/groups/assign',{method:'POST',body:JSON.stringify({kind,ids,groupId:f.elements.groupId.value})});state.sop.draft=null;state.sop.baseline='';await render();toast('归组已完成')})}
  }catch(error){toast(error.message)}finally{b.disabled=false}
});
