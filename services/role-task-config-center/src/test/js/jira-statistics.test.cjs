const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const code=fs.readFileSync('src/main/resources/static/jira-statistics.js','utf8');
function fixture(){
 const nodes=Object.fromEntries(['#jiraStatistics','#jiraStatsBody','#jiraStatsNotice'].map(k=>[k,{innerHTML:'',textContent:''}]));
 const ctx=vm.createContext({URL,URLSearchParams,location:{href:'http://localhost/?page=jira'},history:{replaceState(){}},setInterval(){},state:{page:'jira'},jiraView:'statistics',groupState:{selected:'',items:[]},$:k=>nodes[k],groupName:id=>id,time:v=>v,esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;')});
 vm.runInContext(code,ctx);return {ctx,nodes,run:s=>vm.runInContext(s,ctx)};
}
const result={summary:{total:2,full:1,limited:1,noSubmission:0,passed:2,blocked:0,other:0},groups:[],developers:[{developer:'<甲>',total:3,full:1,limited:1,noSubmission:0,passed:2,blocked:1}],generatedAt:'now'};
test('passes, empty denominator and escaped developer names',async()=>{
 const h=fixture();h.ctx.api=async()=>result;await h.run('loadJiraStatistics()');
 assert.match(h.nodes['#jiraStatsBody'].innerHTML,/100.0%/);assert.match(h.nodes['#jiraStatsBody'].innerHTML,/&lt;甲>/);assert.equal(h.run('statsPercent(0,0)'),'—');
});
test('late group response cannot overwrite new scope',async()=>{
 const h=fixture();let resolve;h.ctx.api=()=>new Promise(r=>resolve=r);const pending=h.run('loadJiraStatistics()');h.ctx.groupState.selected='next';resolve(result);await pending;assert.equal(h.nodes['#jiraStatsBody'].innerHTML,'');
});
test('refresh failure retains previous results',async()=>{
 const h=fixture();h.ctx.api=async()=>result;await h.run('loadJiraStatistics()');const before=h.nodes['#jiraStatsBody'].innerHTML;
 h.ctx.api=async()=>{throw Error('offline')};await h.run('loadJiraStatistics()');assert.equal(h.nodes['#jiraStatsBody'].innerHTML,before);assert.match(h.nodes['#jiraStatsNotice'].textContent,/保留/);
});
test('hidden statistics do not request data',async()=>{const h=fixture();h.ctx.jiraView='pool';h.ctx.api=async()=>assert.fail('unexpected request');await h.run('loadJiraStatistics()')});

test('developer total includes non-passing issues and opens unfiltered details',async()=>{
 const h=fixture();h.ctx.api=async()=>result;await h.run('loadJiraStatistics()');
 const html=h.nodes['#jiraStatsBody'].innerHTML;
 assert.match(html,/<th>开发人<\/th><th>Jira 总数<\/th>/);
 assert.match(html,/data-stat-category="" data-stat-group="" data-stat-developer="&lt;甲>">3<\/button>/);
});

test('blocked-only developers remain visible with a filtered detail button',async()=>{
 const h=fixture();h.ctx.api=async()=>({...result,developers:[{developer:'乙',total:2,full:0,limited:0,noSubmission:0,passed:0,blocked:2}]});
 await h.run('loadJiraStatistics()');
 const html=h.nodes['#jiraStatsBody'].innerHTML;
 assert.match(html,/<th>通过总数<\/th><th>阻断<\/th>/);
 assert.match(html,/data-stat-category="blocked" data-stat-group="" data-stat-developer="乙">2<\/button>/);
});

test('manual completion detail is green and remains a separate category',async()=>{
 const h=fixture();
 h.nodes['#jiraStatsDetails']={open:true};
 for(const k of ['#jiraStatsItems','#jiraStatsCategory','#jiraStatsDetailRefresh','#jiraStatsMore'])h.nodes[k]={innerHTML:''};
 h.ctx.api=async()=>({items:[{issueKey:'TEST-1',title:'已有修复',groupIds:[],developer:'甲',status:'completed_no_submission',finishedAt:'today'}],nextCursor:null,generatedAt:'now'});
 await h.run("showJiraStatItems({category:'completed_no_submission'})");
 assert.match(h.nodes['#jiraStatsItems'].innerHTML,/jira-status success">正常结束（无提交）/);
 assert.match(h.nodes['#jiraStatsItems'].innerHTML,/value="completed_no_submission" selected/);
});

test('manual completion contributes to total and developer passes with its own column',async()=>{
 const h=fixture();
 const counts={total:4,full:1,limited:1,noSubmission:1,passed:3,blocked:0,other:1};
 h.ctx.api=async()=>({summary:counts,groups:[{groupId:'group',...counts}],developers:[{developer:'甲',...counts}],generatedAt:'now'});
 await h.run('loadJiraStatistics()');
 const html=h.nodes['#jiraStatsBody'].innerHTML;
 assert.match(html,/75.0%/);
 assert.match(html,/data-stat-category="passed" data-stat-group="" data-stat-developer="">3<\/button>/);
 assert.match(html,/data-stat-category="passed" data-stat-group="" data-stat-developer="甲">3<\/button>/);
 assert.match(html,/data-stat-category="completed_no_submission" data-stat-group="group" data-stat-developer="">1<\/button>/);
 assert.match(html,/data-stat-category="completed_no_submission" data-stat-group="" data-stat-developer="甲">1<\/button>/);
});

test('review rework detail is orange with an independent filter',async()=>{
 const h=fixture();
 h.nodes['#jiraStatsDetails']={open:true};
 for(const k of ['#jiraStatsItems','#jiraStatsCategory','#jiraStatsDetailRefresh','#jiraStatsMore'])h.nodes[k]={innerHTML:''};
 h.ctx.api=async()=>({items:[{issueKey:'TEST-1',title:'回归失败',groupIds:[],developer:'甲',status:'rework_required',finishedAt:'today'}],nextCursor:null,generatedAt:'now'});
 await h.run("showJiraStatItems({category:'rework_required'})");
 assert.match(h.nodes['#jiraStatsItems'].innerHTML,/jira-status waiting">审核不通过（待返工）/);
 assert.match(h.nodes['#jiraStatsItems'].innerHTML,/value="rework_required" selected/);
});
