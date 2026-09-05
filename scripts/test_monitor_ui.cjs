const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

class Element {
  constructor() { this.children = []; this.dataset = {}; this.scrollHeight = 0; this.scrollTop = 0; this.clientHeight = 100; }
  append(...children) { children.forEach(child => this.insertBefore(child, null)); }
  insertBefore(child, before) {
    child.remove();
    const index = before ? this.children.indexOf(before) : this.children.length;
    this.children.splice(index, 0, child); child.parentNode = this;
  }
  remove() { if (this.parentNode) { const p = this.parentNode; p.children.splice(p.children.indexOf(this), 1); this.parentNode = null; } }
  replaceChildren(...children) { [...this.children].forEach(child => child.remove()); this.append(...children); }
  get lastElementChild() { return this.children.at(-1); }
}

function load() {
  const elements = new Map();
  const timers = [];
  const context = vm.createContext({URLSearchParams, location:{search:'?workflowId=one'},
    crypto:{getRandomValues: array => webcrypto.getRandomValues(array)}, Uint8Array,
    setTimeout:(fn, delay) => {timers.push({fn, delay}); return timers.length;}, clearTimeout:()=>{},
    document:{hidden:false, createElement:()=>new Element(), querySelector:selector=>{
      if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector);
    }}});
  const source = fs.readFileSync('services/workflow-console/src/main/resources/static/app.js', 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('$("#lookupForm").onsubmit')), context);
  return {context, elements, timers, run:code=>vm.runInContext(code, context)};
}

test('HTTP fallback generates distinct RFC 4122 v4 message IDs', () => {
  const app = load();
  const values = app.run('Array.from({length:1000}, newMessageId)');
  assert.equal(new Set(values).size, 1000);
  values.forEach(value => assert.match(value, /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/));
});

test('unchanged steps and messages retain their DOM nodes', () => {
  const app = load();
  app.run('state.messages=[{id:"m1",role:"user",text:"hello"},{id:"m2",role:"assistant",text:"reply"}]; renderMessages()');
  const box = app.elements.get('#messages');
  const [first, second] = box.children;
  app.run('state.messages[1].text="updated reply"; renderMessages()');
  assert.equal(box.children[0], first);
  assert.notEqual(box.children[1], second);
  app.run('state.snapshot={}; renderSteps([{id:"a",status:"pending"},{id:"b",status:"pending"}])');
  const list = app.elements.get('#steps'), firstStep = list.children[0];
  app.run('renderSteps([{id:"a",status:"pending"},{id:"b",status:"completed"}])');
  assert.equal(list.children[0], firstStep);
  assert.equal(list.children.length, 2);
});

test('terminal polling detects another client restarting the workflow', async () => {
  const app = load();
  app.run('render=()=>{}; renderMessages=()=>{}; api=async()=>({workflowId:"one",status:"completed",nodes:[],revision:"1",lastEventSequence:0}); state.eventsLoaded=true');
  await app.run('refresh()');
  assert.equal(app.timers.at(-1).delay, 15000);
  app.run('api=async()=>({workflowId:"one",status:"running",nodes:[],revision:"2",lastEventSequence:0})');
  await app.run('refresh()');
  assert.equal(app.timers.at(-1).delay, 2000);
  assert.equal(app.run('state.snapshot.status'), 'running');
});

test('one event page per poll, no duplicates on retry, older pages preserve live cursor', async () => {
  const app = load(); let calls = [];
  app.context.fetchPage = async path => {calls.push(path); return {events:[{sequence:9,type:'chat.user.accepted',payload:{messageId:'m',text:'hello'}}],nextCursor:20,oldestCursor:9,hasOlder:true};};
  app.run('api=fetchPage; renderMessages=()=>{}; state.snapshot={lastEventSequence:20}');
  await app.run('events()');
  assert.match(calls[0], /tail=true/);
  assert.equal(calls.length, 1);
  await app.run('events()');
  assert.equal(calls.length, 1);
  await app.run('loadOlderMessages()');
  assert.equal(app.run('state.cursor'), 20);
  assert.equal(app.run('state.messages.length'), 1);
});

test('partial result responses retain cached output, while restarted attempts clear it', async () => {
  const app = load();
  app.run('render=()=>{}; renderMessages=()=>{}; state.eventsLoaded=true; api=async()=>({workflowId:"one",status:"running",nodes:[{id:"a",status:"completed",response:"完整结果",artifacts:[],resultRevision:2}],revision:"1",lastEventSequence:0})');
  await app.run('refresh()');
  app.run('api=async()=>({workflowId:"one",status:"running",nodes:[{id:"a",status:"completed",resultUnchanged:true,resultRevision:2}],revision:"2",lastEventSequence:0})');
  await app.run('refresh()');
  assert.equal(app.run('state.snapshot.nodes[0].response'), '完整结果');
  app.run('api=async()=>({workflowId:"one",status:"running",nodes:[{id:"a",status:"pending",response:null,artifacts:[],resultRevision:3}],revision:"3",lastEventSequence:0})');
  await app.run('refresh()');
  assert.equal(app.run('state.snapshot.nodes[0].response'), null);
});
