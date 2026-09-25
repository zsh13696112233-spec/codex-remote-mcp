const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('src/main/resources/static/mcps.js','utf8');
function setup(){
  const listeners={},content={innerHTML:''},inventory={innerHTML:''},nav={innerHTML:''},storage=new Map();
  let sequence=0;
  const context=vm.createContext({state:{page:'mcps'},document:{hidden:false,addEventListener:(k,v)=>listeners[k]=v},
    $:selector=>selector==='.toolbar'?{classList:{add(){}}}:selector==='#content'?content:selector==='#mcpInventory'?inventory:selector==='#mcpMachineNav'?nav:null,
    concreteGroup:()=> 'group-a',esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    setInterval:()=>{},sessionStorage:{removeItem:k=>storage.delete(k)},
    skillPending:(key,payload)=>{if(!storage.has(key))storage.set(key,{payload,requestId:'request-'+(++sequence)});return storage.get(key).requestId},
    api:async path=>path.includes('/machines')?{agents:[]}:path.includes('/inventory')?{items:[]}:path.includes('/mcp-deployments')?{deployments:[]}:{packages:[],enabled:true},toast:()=>{}});
  vm.runInContext(source,context);
  return {context,content,inventory,nav,listeners,storage,run:code=>vm.runInContext(code,context)};
}
test('loads grouped library and an explicit empty state',async()=>{
  const h=setup();await h.run('renderMcps()');
  assert.match(h.content.innerHTML,/MCP \/ CLI 包库/);assert.match(h.content.innerHTML,/暂无安装包/);
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

test('CLI installations show their type and do not claim MCP discovery',()=>{
  const h=setup();
  h.context.rows=[{package_id:'p',agent_id:'a',state:'completed',installation:{kind:'cli',programPath:'C:/apps/gm_cli.exe',skillPath:'C:/skills/SKILL.md'},diagnostics:{kind:'cli',stage:'验证 CLI 帮助命令',reason:'<secret>',checkedAt:'today'}}];
  const html=h.run('mcpTaskRows(rows)');
  assert.match(html,/CLI \+ Skill/);
  assert.match(html,/验证 CLI 帮助命令/);
  assert.doesNotMatch(html,/工具数量|服务：|<secret>/);
  h.context.rows[0].installation.kind='mcp';
  assert.match(h.run('mcpTaskRows(rows)'),/MCP \+ Skill/);
  h.context.rows[0].installation.skillPath=null;
  assert.equal(h.run('mcpKindBadge(rows[0].installation)'),'<span class="badge">MCP</span>');
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

test('machine navigation includes empty executors and lists the latest package attempt',()=>{
  const h=setup();
  h.run(`mcpView.machines=[{agentId:'a',name:'执行机 A',capabilities:['executor'],enabled:true},{agentId:'b',name:'空机器',capabilities:['executor']},{agentId:'s',name:'仅监督',capabilities:['supervisor']}];
    mcpView.items=[{id:'old',agent_id:'a',package_id:'p',state:'completed',created_at:'2026-09-16'}, {id:'new',agent_id:'a',package_id:'p',state:'installing',created_at:'2026-09-17'}, {id:'other',agent_id:'a',package_id:'q',state:'failed',created_at:'2026-09-17'}];drawMcpInventory();`);
  assert.match(h.nav.innerHTML,/2 个安装包/);
  assert.match(h.nav.innerHTML,/空机器/);
  assert.match(h.nav.innerHTML,/0 个安装包/);
  assert.doesNotMatch(h.nav.innerHTML,/仅监督/);
  assert.equal(h.run('mcpCurrentItems("a").length'),2);
  assert.equal(h.run('mcpCurrentItems("a")[0].id'),'new');
  assert.doesNotMatch(h.inventory.innerHTML,/安装成功/);
  assert.match(h.inventory.innerHTML,/正在安装/);
});

test('installation details show escaped package identity with a missing package fallback',()=>{
  const h=setup();h.run(`mcpView.packages=[{id:'p',name:'<包名>'}];`);
  h.context.rows=[{package_id:'p',agent_id:'a',state:'completed'}];
  assert.match(h.run('mcpTaskRows(rows)'),/&lt;包名>/);
  h.context.rows[0].package_id='older-package';
  assert.match(h.run('mcpTaskRows(rows)'),/older-packag/);
});

test('machine selection survives refresh and falls back when group changes',async()=>{
  const h=setup();h.run(`mcpView.machines=[{agentId:'a',name:'A',capabilities:['executor']},{agentId:'b',name:'B',capabilities:['executor']}];drawMcpInventory()`);
  await h.listeners.click({target:{closest:()=>({dataset:{mcp:'machine',id:'b'}})}});
  assert.equal(h.run('mcpView.machineId'),'b');
  assert.match(h.inventory.innerHTML,/暂无平台安装记录/);
  h.run('drawMcpInventory()');assert.equal(h.run('mcpView.machineId'),'b');
  h.run(`mcpView.machines=[{agentId:'c',name:'C',capabilities:['executor']}];drawMcpInventory()`);
  assert.equal(h.run('mcpView.machineId'),'c');
  h.run('mcpView.machines=[];drawMcpInventory()');assert.equal(h.run('mcpView.machineId'),'');
  assert.match(h.inventory.innerHTML,/登记执行机/);
});

test('polling preserves selection and expanded diagnostics while updating status',()=>{
  const h=setup();let writes=0,html='';
  Object.defineProperty(h.inventory,'innerHTML',{get:()=>html,set:v=>{writes++;html=v;}});
  h.run(`mcpView.machines=[{agentId:'a',name:'A',capabilities:['executor']}];mcpView.items=[{id:'t',agent_id:'a',package_id:'p',state:'installing'}];drawMcpInventory();drawMcpInventory()`);
  assert.equal(writes,1);assert.doesNotMatch(html,/ open>/);
  h.listeners.toggle({target:{dataset:{mcpDetails:'t'},open:true}});
  h.run(`mcpView.items[0].state='completed';drawMcpInventory()`);
  assert.match(html,/安装成功/);assert.match(html,/ open>/);
  assert.equal(h.run('mcpView.machineId'),'a');
  h.listeners.toggle({target:{dataset:{mcpDetails:'t'},open:false}});
  h.run('drawMcpInventory()');assert.doesNotMatch(html,/ open>/);
});
