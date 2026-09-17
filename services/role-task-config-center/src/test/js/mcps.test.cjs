const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('src/main/resources/static/mcps.js','utf8');
function setup(){
  const listeners={},content={innerHTML:''},inventory={innerHTML:''},storage=new Map();
  let sequence=0;
  const context=vm.createContext({state:{page:'mcps'},document:{hidden:false,addEventListener:(k,v)=>listeners[k]=v},
    $:selector=>selector==='#content'?content:selector==='#mcpInventory'?inventory:null,
    concreteGroup:()=> 'group-a',esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    setInterval:()=>{},sessionStorage:{removeItem:k=>storage.delete(k)},
    skillPending:(key,payload)=>{if(!storage.has(key))storage.set(key,{payload,requestId:'request-'+(++sequence)});return storage.get(key).requestId},
    api:async path=>path.includes('/machines')?{agents:[]}:path.includes('/inventory')?{items:[]}:path.includes('/mcp-deployments')?{deployments:[]}:{packages:[],enabled:true},toast:()=>{}});
  vm.runInContext(source,context);
  return {context,content,inventory,listeners,storage,run:code=>vm.runInContext(code,context)};
}
test('loads grouped library and an explicit empty state',async()=>{
  const h=setup();await h.run('renderMcps()');
  assert.match(h.content.innerHTML,/MCP 包库/);assert.match(h.content.innerHTML,/暂无 MCP 包/);
  assert.equal(h.run('mcpScope("/api/mcp-packages")'),'/api/mcp-packages?groupId=group-a');
});
test('disabled machines explain authorization and connection requirements',()=>{
  const h=setup();h.context.machine={agentId:'a',name:'<machine>',port:4500,enabled:true,testStatus:'untested'};
  const html=h.run('mcpMachineOption(machine)');
  assert.match(html,/disabled/);assert.match(html,/未授权 MCP 安装/);assert.match(html,/连接尚未检测通过/);
  assert.match(html,/class="skill-machine"/);assert.doesNotMatch(html,/<machine>/);
  h.context.machine.testStatus='passed';h.context.machine.mcpInstallation={enabled:true};
  assert.doesNotMatch(h.run('mcpMachineOption(machine)'),/disabled/);
  h.context.machine.enabled=false;assert.match(h.run('mcpMachineOption(machine)'),/机器未启用/);
});
test('stale page response never replaces another page',async()=>{
  const h=setup(),finish=[];
  h.context.api=()=>new Promise(resolve=>{finish.push(resolve)});
  const pending=h.run('renderMcps()');h.context.state.page='roles';
  // Replace with resolved requests in a second render; the old render must remain stale.
  h.context.api=async()=>({packages:[],agents:[],items:[],deployments:[],enabled:true});
  h.context.state.page='mcps';await h.run('renderMcps()');
  h.context.state.page='roles';h.content.innerHTML='roles';
  finish.forEach(resolve=>resolve({packages:[],agents:[],items:[],deployments:[],enabled:true}));
  await pending;
  assert.equal(h.content.innerHTML,'roles');
});
test('late installation acknowledgement cannot redraw a different page',async()=>{
  const h=setup();h.context.state.page='roles';h.content.innerHTML='roles';
  await h.run('renderMcps()');assert.equal(h.content.innerHTML,'roles');
});
test('failed submission reuses request ID and clears it only after acknowledgement',async()=>{
  const h=setup(),ids=[];
  h.context.api=async(path,options)=>{ids.push(JSON.parse(options.body).requestId);if(ids.length===1)throw new Error('offline');return {id:'batch'}};
  await assert.rejects(h.run('mcpSubmit("/api/mcp-deployments",{packageId:"p"},"pending")'));
  assert.equal(h.storage.size,1);
  await h.run('mcpSubmit("/api/mcp-deployments",{packageId:"p"},"pending")');
  assert.equal(ids[0],ids[1]);assert.equal(h.storage.size,0);
});
test('task rows escape diagnostics and distinguish review from active work',()=>{
  const h=setup();h.context.rows=[{id:'x',agent_id:'a',state:'review',message:'<script>bad</script>'}];
  const html=h.run('mcpTaskRows(rows)');
  assert.match(html,/待核对/);assert.match(html,/重新检测/);assert.doesNotMatch(html,/<script>|data-mcp="retry"/);
  h.context.rows[0].state='installing';assert.doesNotMatch(h.run('mcpTaskRows(rows)'),/data-mcp="check"/);
});
test('detection details distinguish unknown from zero and escape text',()=>{
  const h=setup();
  assert.match(h.run('mcpDiagnosticRows(null)'),/重新检测/);
  h.context.detail={checkedAt:'2026-09-17',stage:'<stage>',found:true,connection:'已连接',toolCount:0,reason:'<script>'};
  const html=h.run('mcpDiagnosticRows(detail)');
  assert.match(html,/已找到/);assert.match(html,/工具数量：0/);assert.match(html,/2026-09-17/);
  assert.doesNotMatch(html,/<stage>|<script>/);
  h.context.detail.toolCount=null;h.context.detail.found=null;
  assert.match(h.run('mcpDiagnosticRows(detail)'),/工具数量：未知/);
  assert.match(h.run('mcpDiagnosticRows(detail)'),/尚未确认/);
});
test('busy state blocks duplicate task actions',async()=>{
  const h=setup();let calls=0,finish;
  h.context.api=()=>{calls++;return new Promise(resolve=>finish=resolve)};
  const button={dataset:{mcp:'check',id:'task'},disabled:false};
  const event={target:{closest:()=>button}};
  const first=h.listeners.click(event);await h.listeners.click(event);assert.equal(calls,1);
  h.context.api=async path=>path.includes('inventory')?{items:[]}:path.includes('machines')?{agents:[]}:path.includes('deployments')?{deployments:[]}:{packages:[]};
  finish({accepted:true});await first;assert.equal(button.disabled,false);
});
