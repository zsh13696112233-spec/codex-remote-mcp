const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../../main/resources/static');
const source=fs.readFileSync(path.join(root,'app.js'),'utf8');
function fixture(){
  const nodes=new Map(),calls=[];
  const $=selector=>{if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',value:'',open:false,showModal(){this.open=true},addEventListener(type,fn){this[type]=fn}});return nodes.get(selector)};
  let response={items:[{workflowId:'run-1',status:'completed',monitorUrl:'http://localhost:8090/?workflowId=run-1'}],total:41,page:0,size:20};
  const context=vm.createContext({$,esc:x=>String(x??''),time:x=>x,toast(){},window:{open(){throw Error('Use native link')}},api:async url=>{calls.push(url);if(response instanceof Error)throw response;return response}});
  vm.runInContext(source.slice(source.indexOf('const runHistory ='),source.indexOf('document.querySelectorAll("nav button")')),context);
  return {nodes,calls,run:code=>vm.runInContext(code,context),respond:value=>response=value,$};
}
test('monitor link preserves dialog and pagination is based on total',async()=>{
  const f=fixture();await f.run('showRuns("task","测试")');
  const html=f.$('#runList').innerHTML;
  assert.match(html,/target="_blank" rel="noopener"/);
  assert.match(html,/共 41 条 · 第 1 \/ 3 页/);
  assert.match(html,/max="3"/);
  assert.match(html,/data-history-page="1" >下一页/);
  const index=fs.readFileSync(path.join(root,'index.html'),'utf8');
  const dialog=index.match(/<dialog id="runsDialog"[\s\S]*?<\/dialog>/)[0];
  assert.doesNotMatch(dialog,/<form/);
  assert.equal(f.$('#runsDialog').open,true);
});
test('filter resets page and refresh retains selection and page',async()=>{
  const f=fixture();await f.run('showRuns("task","测试",2)');
  await f.$('#runHistoryStatus').onchange({target:{value:'failed'}});
  assert.match(f.calls.at(-1),/status=failed&page=0/);
  f.respond({items:[],total:42,page:2,size:20});
  await f.run('showRuns("task","测试",2,"failed")');
  await f.$('#runHistoryRefresh').onclick();
  assert.match(f.calls.at(-1),/status=failed&page=2/);
});
test('empty and error states remain usable',async()=>{
  const f=fixture();f.respond({items:[],total:0,page:0,size:20});
  await f.run('showRuns("task","测试",0,"failed")');
  assert.match(f.$('#runList').innerHTML,/暂无符合该状态/);
  assert.match(f.$('#runList').innerHTML,/data-history-page="1" disabled/);
  f.respond(Error('暂时无法加载'));await f.$('#runHistoryRefresh').onclick();
  assert.match(f.$('#runList').innerHTML,/暂时无法加载.*重新加载/);
});
test('late responses cannot overwrite a newer filter',async()=>{
  const f=fixture();let resolve;
  f.respond(new Promise(r=>resolve=r));
  const old=f.run('showRuns("task","测试")');
  f.respond({items:[],total:0,page:0,size:20});
  await f.run('showRuns("task","测试",0,"failed")');
  resolve({items:[],total:90,page:0,size:20});await old;
  assert.match(f.$('#runList').innerHTML,/共 0 条/);
});
test('jump submits selected page and rejects invalid input',async()=>{
  const f=fixture();await f.run('showRuns("task","测试",0,"running")');
  const button={disabled:false,dataset:{},hasAttribute:name=>name==='data-history-jump'};
  const input=f.$('#runHistoryPage');input.value='3';input.reportValidity=()=>true;
  await f.$('#runList').onclick({target:{closest:()=>button}});
  assert.match(f.calls.at(-1),/status=running&page=2/);
  assert.equal(button.disabled,false);
  input.reportValidity=()=>false;const count=f.calls.length;
  await f.$('#runList').onclick({target:{closest:()=>button}});
  assert.equal(f.calls.length,count);
});
