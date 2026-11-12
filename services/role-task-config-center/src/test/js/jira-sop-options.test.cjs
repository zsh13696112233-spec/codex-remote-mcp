const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function fixture(){
 const panel={innerHTML:'',querySelectorAll:()=>[]};
 const draft={id:'saved',groupId:'group',jiraEnabled:true,jiraConnectionId:'account',jiraProject:'TEST',jiraIssueType:'sub-bug',jiraStatus:'Open',jiraRoles:'Planner'};
 const root={projects:[{id:'10',key:'TEST',name:'项目'}]};
 const data={issueTypes:[{id:'6',name:'sub-bug'}],statuses:[{id:'1',name:'Open'},{id:'4',name:'重新打开'}],roles:[{id:'101',name:'Planner'},{id:'102',name:'Reviewer'}],rolesError:''};
 const calls=[];
 const ctx=vm.createContext({state:{sop:{draft},jiraAccounts:[{id:'account',groupId:'group',enabled:true,testStatus:'passed'}]},URLSearchParams,
 document:{querySelector:()=>panel,addEventListener(){}},
 esc:value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
 jiraAccountOptions:()=>'<option>账号</option>',jiraFieldsFromQuery:()=>({}),syncDirtyUi(){},
 api:async url=>{calls.push(url);return url.includes('project=')?data:root}});
 vm.runInContext(fs.readFileSync('src/main/resources/static/jira-sop-options.js','utf8'),ctx);
 return {ctx,panel,draft,root,data,calls,run:code=>vm.runInContext(code,ctx)};
}
test('loads linked catalogs without rewriting old names or saved values',async()=>{
 const h=fixture(),before=JSON.stringify(h.draft);
 await h.run('loadSopJiraOptions()');
 assert.equal(h.calls.length,2);
 assert.match(h.calls[1],/project=TEST&issueType=sub-bug/);
 assert.equal(JSON.stringify(h.draft),before);
 assert.match(h.panel.innerHTML,/>项目<\/option>/);
 assert.match(h.panel.innerHTML,/value="Open" checked/);
 assert.match(h.panel.innerHTML,/value="4"/);
 assert.match(h.panel.innerHTML,/value="Reviewer"/);
 assert.doesNotMatch(h.panel.innerHTML,/value="102"/);
 await h.run('loadSopJiraOptions()');assert.equal(h.calls.length,2);
});
test('multi-select stores status IDs and role names, and permits removing old unavailable values',()=>{
 const h=fixture();
 h.ctx.event={target:{dataset:{jiraChoice:'jiraStatus'},value:'4',checked:true}};
 h.run('handleSopJiraChange(event)');assert.equal(h.draft.jiraStatus,'Open, 4');
 h.ctx.event={target:{dataset:{jiraChoice:'jiraRoles'},value:'Reviewer',checked:true}};
 h.run('handleSopJiraChange(event)');assert.equal(h.draft.jiraRoles,'Planner, Reviewer');
 h.draft.jiraStatus='<旧状态>';
 assert.match(h.run('sopJiraOptionsBody(state.sop.draft)'),/&lt;旧状态>（待确认）/);
 h.ctx.event={target:{dataset:{jiraChoice:'jiraStatus'},value:'<旧状态>',checked:false}};
 h.run('handleSopJiraChange(event)');assert.equal(h.draft.jiraStatus,'');
});
test('failed refresh preserves selections and previously loaded options',async()=>{
 const h=fixture();await h.run('loadSopJiraOptions()');
 h.ctx.api=async()=>{throw Error('读取失败')};
 await h.run('loadSopJiraOptions(true)');
 assert.match(h.panel.innerHTML,/读取失败/);
 assert.match(h.panel.innerHTML,/重新打开/);
 assert.equal(h.draft.jiraStatus,'Open');
});
test('switching project discards stale UI responses',async()=>{
 const h=fixture();let resolve;
 h.ctx.api=()=>new Promise(r=>{resolve=r});
 const old=h.run('loadSopJiraOptions()');
 h.draft.jiraProject='OTHER';h.panel.innerHTML='new project';
 resolve(h.root);await old;
 assert.equal(h.panel.innerHTML,'new project');
});
test('reopening same SOP during load displays completion without changing new draft',async()=>{
 const h=fixture();let resolve;
 h.ctx.api=url=>url.includes('project=')?Promise.resolve(h.data):new Promise(r=>{resolve=r});
 const old=h.run('loadSopJiraOptions()');
 h.ctx.state.sop.draft={...h.draft};resolve(h.root);await old;
 assert.match(h.panel.innerHTML,/重新打开/);
 assert.doesNotMatch(h.panel.innerHTML,/正在读取/);
});
test('unavailable account and empty lists do not invent options',async()=>{
 const h=fixture();h.ctx.state.jiraAccounts=[];
 await h.run('loadSopJiraOptions()');
 assert.equal(h.calls.length,0);assert.match(h.panel.innerHTML,/本组可用账号/);
 const html=h.run("sopJiraMulti('jiraStatus','状态',[],'')");
 assert.match(html,/暂无可用选项/);
});

test('issue types support multiple selections, linked reload and unrestricted selection',async()=>{
 const h=fixture();h.data.issueTypes.push({id:'7',name:'任务'});
 await h.run('loadSopJiraOptions()');
 assert.doesNotMatch(h.panel.innerHTML,/data-jira-select="jiraIssueType"/);
 assert.match(h.panel.innerHTML,/data-jira-choice="jiraIssueType"/);
 h.ctx.event={target:{dataset:{jiraChoice:'jiraIssueType'},value:'7',checked:true}};
 h.run('handleSopJiraChange(event)');
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(h.draft.jiraIssueType,'sub-bug, 7');
 assert.match(h.calls.at(-1),/issueType=sub-bug%2C\+7/);
 assert.match(h.panel.innerHTML,/value="sub-bug" checked/);
 assert.match(h.panel.innerHTML,/value="7" checked/);
 assert.doesNotMatch(h.panel.innerHTML,/待确认/);
 for(const value of ['sub-bug','7']){
   h.ctx.event={target:{dataset:{jiraChoice:'jiraIssueType'},value,checked:false}};
   h.run('handleSopJiraChange(event)');
   await new Promise(resolve=>setImmediate(resolve));
 }
 assert.equal(h.draft.jiraIssueType,'');
 assert.match(h.panel.innerHTML,/>不限类型<\/summary>/);
 assert.match(h.calls.at(-1),/issueType=&/);
});

test('type choices remain available while linked options are loading',async()=>{
 const h=fixture();h.data.issueTypes.push({id:'7',name:'任务'});
 await h.run('loadSopJiraOptions()');
 let resolve;h.ctx.api=()=>new Promise(r=>{resolve=r});
 h.ctx.event={target:{dataset:{jiraChoice:'jiraIssueType'},value:'7',checked:true}};
 h.run('handleSopJiraChange(event)');
 assert.match(h.panel.innerHTML,/value="sub-bug" checked/);
 assert.match(h.panel.innerHTML,/value="7" checked/);
 assert.match(h.panel.innerHTML,/正在读取/);
 h.ctx.api=async()=>h.data;resolve(h.root);
 await new Promise(r=>setImmediate(r));
 assert.doesNotMatch(h.panel.innerHTML,/正在读取/);
});
