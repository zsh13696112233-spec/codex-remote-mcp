const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname,
  '../services/role-task-config-center/src/main/resources/static/app.js'), 'utf8');

function taskDialog(targets) {
  const form = Object.fromEntries(['id', 'name', 'objective', 'additionalNotes', 'sopId',
    'blockedNotificationGroupId', 'notifyDingTalk', 'enabled'].map(name => [name, {}]));
  form.reset = () => {};
  form.querySelector = () => null;
  let opened = false;
  const context = vm.createContext({
    document: { querySelector: selector => selector === '#taskForm' ? form : {
      showModal: () => { opened = true; }
    } },
    taskRequirements: x => x.objective || '',
    syncTaskDingTalkTargetFields: () => {},
    bindGroupForm: () => {},
    targets,
  });
  // Load the main script's globals and actual dialog entry without the notification script.
  vm.runInContext(source.slice(0, source.indexOf('async function api(')), context);
  vm.runInContext(source.split('\n').find(line => line.startsWith('function openTask(')), context);
  vm.runInContext('state.dingtalkTargets = targets', context);
  return { form, open: value => {
    context.task = value;
    vm.runInContext('openTask(task)', context);
    assert.equal(opened, true);
  } };
}

test('new task opens without the optional notification script or any groups', () => {
  const dialog = taskDialog([]);
  dialog.open(undefined);
  assert.equal(dialog.form.blockedNotificationGroupId.value, '');
  assert.match(dialog.form.blockedNotificationGroupId.innerHTML, /value=""/);
  assert.equal(dialog.form.enabled.checked, true);
});

test('edit retains its unavailable group while new tasks only offer available groups', () => {
  const targets = [
    { id: 'active', targetType: 'GROUP', enabled: true, available: true, displayName: '<Team>' },
    { id: 'disabled', targetType: 'GROUP', enabled: false, available: true, displayName: 'Old' },
    { id: 'person', targetType: 'PERSON', enabled: true, available: true, displayName: 'Person' },
  ];
  const dialog = taskDialog(targets);
  dialog.open({ id: 'task', blockedNotificationGroupId: 'disabled' });
  assert.equal(dialog.form.blockedNotificationGroupId.value, 'disabled');
  assert.match(dialog.form.blockedNotificationGroupId.innerHTML, /value="disabled"/);
  assert.match(dialog.form.blockedNotificationGroupId.innerHTML, /&lt;Team&gt;/);
  dialog.open(undefined);
  assert.doesNotMatch(dialog.form.blockedNotificationGroupId.innerHTML, /value="disabled"|value="person"/);
  assert.match(dialog.form.blockedNotificationGroupId.innerHTML, /value="active"/);
});
