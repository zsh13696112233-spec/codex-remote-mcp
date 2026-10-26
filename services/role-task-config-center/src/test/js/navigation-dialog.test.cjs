const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/main/resources/static/app.js','utf8');
function fixture(){
  class Element {constructor(){this.children=new Map();this.hidden=true;this.textContent='';this.classes=new Set();this.classList={remove(){},add(){},toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)}}querySelector(s){if(!this.children.has(s))this.children.set(s,new Element());return this.children.get(s)}focus(){this.focused=true}}
  const nodes=new Map(),panels=[new Element(),new Element()];
  const $=s=>{if(!nodes.has(s))nodes.set(s,new Element());return nodes.get(s)};
  const context=vm.createContext({$,HTMLElement:Element,document:{activeElement:new Element(),querySelector:$,querySelectorAll:s=>s==='aside,main'?panels:[],body:{contains:()=>true}},state:{page:'roles'},location:{href:'http://localhost/'},history:{replaceState(){}},URL,Event,isSopDirty:()=>false,confirmDiscard:()=>true,discardSopChanges(){},resetSkillSelection(){},skillView:{},mcpView:{version:0},render:async()=>{},toast(){}});
  vm.runInContext(source.slice(source.indexOf('let syncWaitFocus='),source.indexOf('function showGatewayStatus')),context);
  vm.runInContext(source.slice(source.indexOf('let navigationPending='),source.indexOf('document.querySelectorAll("nav button").forEach')),context);
  const button={dataset:{page:'tasks'},textContent:'任务定义',classList:{add(){}}};context.button=button;
  return {context,nodes,panels,$,run:s=>vm.runInContext(s,context)};
}
test('navigation blocks repeat clicks until render finishes and restores interaction',async()=>{
  const h=fixture();let resolve,calls=0;h.context.render=()=>{calls++;return new Promise(r=>resolve=r)};
  const pending=h.run('navigateToPage(button)');assert.equal(h.$('#syncWait').hidden,false);assert.equal(h.panels.every(p=>p.inert),true);
  assert.equal(h.$('#syncWait').querySelector('strong').textContent,'');assert.equal(h.$('#syncWait').querySelector('p').textContent,'');assert.equal(h.$('#syncWait').classes.has('spinner-only'),true);assert.equal(h.$('#syncWait').ariaLabel,'正在加载页面');
  await h.run('navigateToPage(button,true)');assert.equal(calls,1);
  resolve();await pending;assert.equal(h.$('#syncWait').hidden,true);assert.equal(h.panels.every(p=>!p.inert),true);assert.equal(h.run('navigationPending'),false);
});
test('failure releases overlay and supports retry on the same page',async()=>{
  const h=fixture();h.context.render=async()=>{throw Error('unavailable')};await h.run('navigateToPage(button)');
  assert.equal(h.$('#syncWait').hidden,true);assert.match(h.$('#content').innerHTML,/重新加载/);
  let called=0;h.context.render=async()=>{called++};await h.$('#content').querySelector('[data-navigation-retry]').onclick();assert.equal(called,1);assert.equal(h.run('navigationPending'),false);
});
test('cancelled dirty SOP navigation never starts loading or changes page',async()=>{
  const h=fixture();h.context.state.page='sops';h.context.isSopDirty=()=>true;h.context.confirmDiscard=()=>false;
  let calls=0;h.context.render=async()=>{calls++};await h.run('navigateToPage(button)');assert.equal(calls,0);assert.equal(h.context.state.page,'sops');assert.equal(h.$('#syncWait').hidden,true);
});
test('directory sync retains its own text after navigation uses the shared overlay',async()=>{
  const h=fixture();await h.run('navigateToPage(button)');h.run('setSyncWait(true)');assert.equal(h.$('#syncWait').querySelector('strong').textContent,'正在同步公司人员');assert.equal(h.$('#syncWait').classes.has('spinner-only'),false);h.run('setSyncWait(false)');
});
test('unified close respects the dialog cancellation guard',()=>{
  const handler=source.split('\n').find(line=>line.startsWith('document.addEventListener("click",e=>{const button=e.target.closest("[data-dialog-close]")'));
  let click;vm.runInNewContext(handler,{document:{addEventListener:(type,fn)=>click=fn},Event});
  const dialog=new EventTarget();let closed=0;dialog.close=()=>closed++;const button={closest:()=>dialog};
  const block=e=>e.preventDefault();dialog.addEventListener('cancel',block);click({target:{closest:()=>button}});assert.equal(closed,0);
  dialog.removeEventListener('cancel',block);click({target:{closest:()=>button}});assert.equal(closed,1);
});
