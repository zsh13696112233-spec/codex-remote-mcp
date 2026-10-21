const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function app() {
  const elements = new Map();
  const element = () => ({value: '', children: [], style: {}, dataset: {},
    append(...children) { children.forEach(child => child.parentNode = this); this.children.push(...children); },
    replaceChildren(...children) { this.children = []; this.append(...children); },
    get lastElementChild() { return this.children.at(-1); },
    remove() { this.parentNode.children = this.parentNode.children.filter(child => child !== this); }});
  const timers = new Map();
  let nextTimer = 0;
  const context = vm.createContext({URLSearchParams, location: {search: '?workflowId=test'},
    document: {querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, element());
      return elements.get(selector);
    }, querySelectorAll: () => [], createElement: element},
    setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval() { return 100; }, clearInterval() {}});
  const source = fs.readFileSync(path.join(__dirname, '../../main/resources/static/app.js'), 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('\n$("#lookupForm").onsubmit')), context);
  vm.runInContext('toast = () => {};', context);
  return {context, elements, timers, run: code => vm.runInContext(code, context)};
}

test('404 clears loading UI and stops all automatic refresh attempts until explicit reload', async () => {
  const {context, elements, timers, run} = app();
  let requests = 0;
  context.fetch = async () => { requests++; return {ok: false, status: 404, text: async () => '{"error":"internal error"}'}; };
  await run('refresh()');
  assert.equal(requests, 1);
  assert.equal(timers.size, 0);
  assert.equal(elements.get('#name').textContent, '任务信息无法加载');
  assert.equal(elements.get('#steps').children.length, 1);
  assert.match(elements.get('#refreshReason').textContent, /找不到该任务.*已停止自动查询/);
  assert.equal(elements.get('.live-badge').hidden, true);
  assert.equal(elements.get('#chatInput').disabled, true);
  await run('refresh()'); // visibility/focus callbacks also use refresh
  await run('setExecutionDetails(true)');
  assert.equal(requests, 1);
  context.fetch = async () => {
    requests++;
    return {ok: true, text: async () => JSON.stringify({status: 'running', nodes: [], revision: '1'})};
  };
  await run('events = async () => {}; start();');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests, 2);
  assert.equal(elements.get('#refreshNotice').hidden, true);
  assert.equal(elements.get('.live-badge').hidden, false);
  assert.equal(elements.get('#chatInput').disabled, false);
  assert.equal(timers.size, 1);
});

test('transient failures keep automatic retry and recover without erasing loaded records', async () => {
  const {context, elements, timers, run} = app();
  run('state.snapshot = {name:"已有任务", status:"running", nodes:[]}; state.messages = [];');
  const steps = elements.get('#steps');
  for (const status of [502, 408, 429]) {
    context.fetch = async () => ({ok: false, status, text: async () => '{}'});
    await run('refresh()');
    assert.equal(run('state.refreshRetrying'), true);
    assert.equal(run('state.snapshot.name'), '已有任务');
    assert.equal(elements.get('#steps'), steps);
    assert.equal(timers.size, 1);
  }
  context.fetch = async () => { throw new TypeError('network unavailable'); };
  await run('refresh()');
  assert.equal(run('state.refreshRetrying'), true);
  assert.equal(timers.size, 1);
});
