const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function app() {
  const source = fs.readFileSync(path.join(__dirname, '../../main/resources/static/app.js'), 'utf8');
  const context = vm.createContext({URLSearchParams, location: {search: ''},
    document: {querySelector: () => ({addEventListener() {}, classList: {add() {}, remove() {}}, style: {}, focus() {}})},
    window: {addEventListener() {}}, setInterval() {}, setTimeout() {}});
  // 初始化 UI 与本用例无关；只加载函数定义及状态。
  const entry = source.indexOf('\n$("#lookupForm").onsubmit');
  assert.ok(entry > 0);
  vm.runInContext(source.slice(0, entry), context);
  return context;
}

test('Jira appears after a result poll and survives unchanged polls and terminal reloads', async () => {
  const context = app();
  const elements = new Map();
  context.document.querySelector = selector => {
    if (!elements.has(selector)) elements.set(selector, {});
    return elements.get(selector);
  };
  vm.runInContext(`
    renderDuration = renderSteps = renderAdvanceCountdown = renderComposer = () => {};
    events = async () => {};
    scheduleRefresh = () => {};
    state.workflowId = 'test';
    api = async () => nextSnapshot;
  `, context);
  context.nextSnapshot = {status: 'running', nodes: [], revision: '1'};
  await vm.runInContext('refresh()', context);
  assert.equal(elements.get('#jira').hidden, true);

  context.nextSnapshot = {status: 'running', nodes: [], revision: '2', jiraContext: {issueKey: 'TEST-123'}};
  await vm.runInContext('refresh()', context);
  assert.equal(elements.get('#jira').hidden, false);
  assert.equal(elements.get('#jiraKey').textContent, 'TEST-123');

  context.nextSnapshot = {unchanged: true, revision: '2'};
  await vm.runInContext('refresh()', context);
  assert.equal(elements.get('#jiraKey').textContent, 'TEST-123');
  for (const status of ['completed', 'failed', 'cancelled']) {
    vm.runInContext('state.snapshot = null; state.revision = null', context);
    context.nextSnapshot = {status, nodes: [], revision: '3', jiraContext: {issueKey: 'TEST-123'}};
    await vm.runInContext('refresh()', context);
    assert.equal(elements.get('#jira').hidden, false);
    assert.equal(elements.get('#jiraKey').textContent, 'TEST-123');
  }
  context.nextSnapshot = {status: 'completed', nodes: [], revision: '4', jiraContext: null};
  await vm.runInContext('refresh()', context);
  assert.equal(elements.get('#jira').hidden, true);
  assert.equal(elements.get('#jiraKey').textContent, '');
});

test('consultation progress stays quiet and final answer remains visible', () => {
  const context = app();
  vm.runInContext(`
    consume({type:'chat.user.accepted',payload:{messageId:'q',text:'核查第二步'}});
    consume({type:'chat.assistant.progress',payload:{messageId:'q',text:'正在查看记录'}});
    consume({type:'chat.assistant.progress',payload:{messageId:'q',text:'正在核查实现'}});
  `, context);
  assert.equal(vm.runInContext('state.messages.length', context), 1);
  assert.equal(vm.runInContext('state.messages[0].status', context), 'processing');
  vm.runInContext(`consume({type:'chat.assistant.completed',payload:{messageId:'q',assistantMessageId:'a',text:'核查结果'}})`, context);
  assert.equal(vm.runInContext('state.messages.length', context), 2);
  assert.equal(vm.runInContext('state.messages[1].text', context), '核查结果');
});

test('failed consultation clears in-progress row', () => {
  const context = app();
  vm.runInContext(`
    consume({type:'chat.user.accepted',payload:{messageId:'q',text:'核查'}});
    consume({type:'chat.assistant.progress',payload:{messageId:'q',text:'正在核查'}});
    consume({type:'chat.message.failed',payload:{messageId:'q',error:'暂时无法核查'}});
  `, context);
  assert.equal(vm.runInContext('state.messages.some(m => m.id === "consult-progress-q")', context), false);
  assert.equal(vm.runInContext('state.messages[0].status', context), 'failed');
});

test('waiting replies preserve executor identity after replay', () => {
  const context = app();
  vm.runInContext(`
    consume({type:'chat.user.accepted',payload:{messageId:'q',text:'采用这个方案'}});
    consume({type:'chat.assistant.completed',payload:{messageId:'q',assistantMessageId:'a',
      stepNumber:2,summaryUpdated:true,text:'已更新交接总结'}});
  `, context);
  assert.equal(vm.runInContext('state.messages[1].stepNumber', context), 2);
});

test('discussion disables continue even when gate is held', () => {
  const context = app();
  const button = {dataset:{advanceGate:'gate',advanceState:'held',advanceAction:'confirm'},disabled:false};
  context.document.querySelectorAll = () => [button];
  vm.runInContext('state.snapshot={discussionBusy:true};renderAdvanceCountdown()', context);
  assert.equal(button.disabled, true);
  vm.runInContext('state.snapshot.discussionBusy=false;renderAdvanceCountdown()', context);
  assert.equal(button.disabled, false);
});


test('lifecycle messages replace supervisor chatter and are idempotent', () => {
  const context = app();
  vm.runInContext(`
    state.snapshot = {status:'completed',advanceMode:'automatic',response:'审核未通过，已退回返工',nodes:[{id:'b',displayName:'审核',dependsOn:['a']},
      {id:'a',displayName:'分析',response:'界面尚未验证',dependsOn:[],attemptCount:0}]};
    consume({sequence:1,type:'workflow.created'});
    consume({sequence:2,type:'node.started',nodeId:'a'});
    consume({sequence:3,type:'appserver.item/agentMessage/delta',source:'supervisor',payload:{message:{method:'item/agentMessage/delta',params:{delta:'仍在等待'}}}});
    consume({sequence:4,type:'node.completed',nodeId:'a'});
    consume({sequence:5,type:'node.started',nodeId:'b'});
    consume({sequence:5,type:'node.started',nodeId:'b'});
    consume({sequence:6,type:'workflow.completed'});
  `, context);
  assert.equal(vm.runInContext('state.messages.length', context), 3);
  assert.match(vm.runInContext('state.messages[1].text', context), /界面尚未验证/);
  assert.match(vm.runInContext('state.messages[1].text', context), /审核/);
  assert.match(vm.runInContext('state.messages[2].text', context), /本轮流程已结束/);
  assert.match(vm.runInContext('state.messages[2].text', context), /审核未通过/);
});


test('failed terminal message retains confirmed business conclusion without raw errors', () => {
  const context = app();
  vm.runInContext(`
    state.snapshot = {status:'running',response:'旧的过程说明',nodes:[]};
    consume({sequence:1,type:'workflow.failed',payload:{
      termination:{outcome:'blocked'},response:'因缺少验收环境而结束，待补齐环境后重新执行。',
      error:'internal traceback and connection details'}});
  `, context);
  const message = vm.runInContext('state.messages[0].text', context);
  assert.match(message, /因阻断结束/);
  assert.match(message, /缺少验收环境/);
  assert.doesNotMatch(message, /旧的过程说明|internal traceback/);
});

test('cancelled terminal message does not reuse a preceding result', () => {
  const context = app();
  vm.runInContext(`
    state.snapshot = {status:'cancelled',response:'旧结果',nodes:[]};
    consume({sequence:1,type:'workflow.cancelled',payload:{response:'旧结果'}});
  `, context);
  assert.doesNotMatch(vm.runInContext('state.messages[0].text', context), /旧结果/);
});


test('manual confirmation remains available after elapsed time and keeps final label', () => {
  const context = app();
  const button = {dataset:{advanceGate:'gate',advanceState:'held',advanceAction:'confirm',
    advanceExpires:'2000-01-01T00:00:00Z'},disabled:false};
  context.document.querySelectorAll = () => [button];
  for (const [kind, label] of [['advance', '进入下一步'], ['finish', '确认结束']]) {
    context.waitKind = kind;
    vm.runInContext('state.snapshot={pendingAdvance:{kind:waitKind,state:"held"}};renderAdvanceCountdown()', context);
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, label);
    vm.runInContext('state.snapshot.discussionBusy=true;renderAdvanceCountdown()', context);
    assert.equal(button.disabled, true);
    vm.runInContext('state.snapshot.discussionBusy=false;state.advanceAction={gateId:"gate",type:"confirm"};renderAdvanceCountdown()', context);
    assert.equal(button.disabled, true);
    vm.runInContext('state.advanceAction=null', context);
  }
});


test('busy manual waits never display automatic timeout copy', () => {
  for (const kind of ['advance', 'finish', 'blocked']) {
    const context = app();
    const elements = [];
    function element() {
      const value = {children:[], dataset:{}, append(...items) { this.children.push(...items); },
        insertBefore(item) { this.children.push(item); }};
      elements.push(value);
      return value;
    }
    const list = element();
    context.document.createElement = element;
    context.document.querySelector = () => list;
    context.waitKind = kind;
    vm.runInContext(`
      state.snapshot={pendingAdvance:{gateId:'gate',completedNodeId:'a',kind:waitKind,state:'busy',
        expiresAt:'2000-01-01T00:00:00Z'}};
      renderSteps([{id:'a',status:'completed',displayName:'步骤'}]);
    `, context);
    const text = elements.map(item => item.textContent || '').join(' ');
    assert.match(text, /正在处理，请等待处理结果/);
    assert.doesNotMatch(text, /无人回复将在|自动正常结束|自动进入下一步|自动阻断结束/);
  }
});


test('details default off, tool updates deduplicate and late start cannot undo completion', () => {
  const context = app();
  vm.runInContext(`
    const detail = {sequence:1,type:'monitor.execution.detail',nodeId:'a',payload:{key:'tool',title:'执行命令',text:'执行中',completed:false}};
    consume(detail);
  `, context);
  assert.equal(vm.runInContext('state.messages.length', context), 0);
  vm.runInContext(`
    state.showDetails = true;
    consume(detail);
    consume({...detail,sequence:2,payload:{...detail.payload,text:'已完成',completed:true}});
    consume(detail);
  `, context);
  assert.equal(vm.runInContext('state.messages.length', context), 1);
  assert.equal(vm.runInContext('state.messages[0].text', context), '已完成');
});

test('switching view drops stale responses and reloads history without changing workflow state', async () => {
  const context = app();
  const pending = [];
  context.fetch = url => new Promise(resolve => pending.push({url, resolve}));
  vm.runInContext(`renderMessages = () => {}; state.workflowId='one'; state.snapshot={status:'waiting',pendingAdvance:{gateId:'gate'}};`, context);
  const old = vm.runInContext('events()', context);
  const enabled = vm.runInContext('setExecutionDetails(true)', context);
  assert.match(pending[0].url, /view=monitor&/);
  assert.match(pending[1].url, /view=monitor_details&.*tail=true/);
  const respond = (request, body) => request.resolve({ok:true,text:async()=>JSON.stringify(body)});
  respond(pending[1], {events:[{sequence:2,type:'monitor.execution.detail',payload:{key:'k',title:'思考摘要',text:'核查中',completed:true}}],nextCursor:2});
  await enabled;
  respond(pending[0], {events:[{sequence:1,type:'workflow.created'}],nextCursor:99});
  await old;
  assert.equal(vm.runInContext('state.cursor', context), 2);
  assert.equal(vm.runInContext('state.messages.length', context), 1);
  const disabled = vm.runInContext('setExecutionDetails(false)', context);
  respond(pending[2], {events:[{sequence:3,type:'chat.assistant.completed',payload:{assistantMessageId:'answer',text:'正式回答'}}],nextCursor:3});
  await disabled;
  assert.equal(vm.runInContext('state.messages[0].text', context), '正式回答');
  assert.equal(vm.runInContext('state.snapshot.pendingAdvance.gateId', context), 'gate');
});

test('history replay preserves discussion details and final answer once', () => {
  const context = app();
  vm.runInContext(`
    state.showDetails=true;
    const history=[{sequence:1,type:'chat.assistant.progress',payload:{messageId:'q',text:'核查记录'}},
      {sequence:2,type:'chat.assistant.completed',payload:{messageId:'q',assistantMessageId:'a',text:'正式结果'}}];
    history.forEach(e=>state.historyEvents.set(e.sequence,e));
    rebuildMessages(); rebuildMessages();
  `, context);
  assert.equal(vm.runInContext('state.messages.length', context), 2);
  assert.equal(vm.runInContext('state.messages[0].title', context), '讨论过程');
});


test('switch preserves formal conversation when details fail or recent page contains only tools', async () => {
  const context = app();
  vm.runInContext(`
    renderMessages=()=>{}; toast=()=>{}; state.workflowId='one';
    const conversation=[
      {sequence:1,type:'chat.user.accepted',payload:{messageId:'q',text:'核查'}},
      {sequence:2,type:'chat.user.forwarded',payload:{messageId:'q'}},
      {sequence:3,type:'chat.assistant.completed',payload:{messageId:'q',assistantMessageId:'a',text:'正式答复'}}];
    conversation.forEach(e=>state.historyEvents.set(e.sequence,e)); rebuildMessages();
    api=async()=>{throw Error('offline')};
  `, context);
  await vm.runInContext('setExecutionDetails(true)', context);
  assert.equal(vm.runInContext('state.messages[1].text', context), '正式答复');
  assert.equal(vm.runInContext('state.messages[0].status', context), 'completed');
  vm.runInContext(`api=async()=>({events:[{sequence:4,type:'monitor.execution.detail',payload:{key:'k',title:'工具',text:'完成',completed:true}}],nextCursor:4,oldestCursor:4,hasOlder:true});`, context);
  await vm.runInContext('events()', context);
  assert.equal(vm.runInContext('state.messages.length', context), 3);
  assert.equal(vm.runInContext('state.messages[1].text', context), '正式答复');
  // 切回精简视图重放已有旧事件，不能将已完成提问退回处理中。
  vm.runInContext('api=async()=>({events:[conversation[1]],nextCursor:4,oldestCursor:2,hasOlder:true})', context);
  await vm.runInContext('setExecutionDetails(false)', context);
  assert.equal(vm.runInContext('state.messages.length', context), 2);
  assert.equal(vm.runInContext('state.messages[0].status', context), 'completed');
});

test('switch during older history request ignores old page and keeps current cursors', async () => {
  const context = app();
  const pending=[];
  context.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
  vm.runInContext('renderMessages=()=>{}; state.workflowId="one"; state.oldestCursor=10;',context);
  const old=vm.runInContext('loadOlderMessages()',context);
  const current=vm.runInContext('setExecutionDetails(true)',context);
  pending[1].resolve({ok:true,text:async()=>JSON.stringify({events:[],nextCursor:20,oldestCursor:15,hasOlder:true})});
  await current;
  pending[0].resolve({ok:true,text:async()=>JSON.stringify({events:[{sequence:1,type:'workflow.created'}],nextCursor:10,oldestCursor:1})});
  await old;
  assert.equal(vm.runInContext('state.oldestCursor',context),15);
  assert.equal(vm.runInContext('state.cursor',context),20);
  assert.equal(vm.runInContext('state.historyEvents.size',context),0);
});
