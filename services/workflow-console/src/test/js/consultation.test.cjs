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

test('consultation progress is replaced by final answer and survives replay', () => {
  const context = app();
  vm.runInContext(`
    consume({type:'chat.user.accepted',payload:{messageId:'q',text:'核查第二步'}});
    consume({type:'chat.assistant.progress',payload:{messageId:'q',text:'正在查看记录'}});
    consume({type:'chat.assistant.progress',payload:{messageId:'q',text:'正在核查实现'}});
  `, context);
  assert.equal(vm.runInContext('state.messages.length', context), 2);
  assert.equal(vm.runInContext('state.messages[1].text', context), '正在核查实现');
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
