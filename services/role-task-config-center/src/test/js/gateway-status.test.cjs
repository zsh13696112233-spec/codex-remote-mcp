const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/main/resources/static/app.js', 'utf8');
const pages = ['groups', 'roles', 'sops', 'tasks', 'schedules', 'runs', 'jira', 'machines', 'skills', 'mcps', 'dingtalk', 'dingtalk-targets'];

function fixture(page) {
  const calls = [];
  const gateway = {textContent: '正在检查网关…'};
  const context = vm.createContext({
    state: {page, gatewayOnline: false}, document: {hidden: false},
    $: () => gateway,
    api: async path => {calls.push(path); return {ready: true};},
    loadGroups: () => new Promise(() => {}),
    refreshMachineStatuses: async () => {},
    updateAgentRuntimeUi() {}, renderRuntimeStatus() {},
  });
  vm.runInContext(source.slice(source.indexOf('function showGatewayStatus('), source.indexOf('async function loadBase(')), context);
  vm.runInContext(source.slice(source.indexOf('let pageRenderVersion='), source.indexOf('function catalogLabel(')), context);
  vm.runInContext(source.slice(source.indexOf('async function refreshAgentRuntimeStatuses('), source.indexOf('function agentPermissionProfiles(')), context);
  return {context, calls, gateway, run: code => vm.runInContext(code, context)};
}

for (const page of pages) {
  test(`${page}: initial gateway status does not wait for page data`, async () => {
    const h = fixture(page);
    h.run('void render()');
    await new Promise(setImmediate);
    assert.equal(h.calls.filter(path => path === '/api/gateway/ready').length, 1);
    assert.equal(h.gateway.textContent, '● Python 网关正常');
  });
  test(`${page}: polling shows offline and recovers when ready`, async () => {
    const h = fixture(page);
    h.context.api = async () => {throw Error('offline');};
    await h.run('refreshAgentRuntimeStatuses()');
    assert.equal(h.gateway.textContent, '● Python 网关不可用');
    h.context.api = async () => ({ready: true});
    await h.run('refreshAgentRuntimeStatuses()');
    assert.equal(h.gateway.textContent, '● Python 网关正常');
  });
}

test('hidden pages pause gateway polling', async () => {
  const h = fixture('jira');
  h.context.document.hidden = true;
  await h.run('refreshAgentRuntimeStatuses()');
  assert.equal(h.calls.length, 0);
});

test('gateway not ready is displayed as unavailable', async () => {
  const h = fixture('jira');
  h.context.api = async () => ({ready: false});
  await h.run('loadGatewayReady()');
  assert.equal(h.gateway.textContent, '● Python 网关不可用');
});
