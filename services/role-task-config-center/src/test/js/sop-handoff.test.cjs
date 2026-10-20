const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../main/resources/static/app.js'),'utf8');

test('new and reloaded SOP payloads omit retired handoff selection',()=>{
  const context=vm.createContext({concreteGroup:()=> 'group',suggestedAgents:()=>[],state:{sop:{}}});
  for(const name of ['blankSop','sopPayload']){
    const line=source.split('\n').find(line=>line.startsWith(`function ${name}(`));
    vm.runInContext(line,context);
  }
  const draft=vm.runInContext('blankSop()',context);
  assert.equal(Object.hasOwn(draft,'handoffMode'),false);
  draft.name='共用目录流程';
  draft.handoffMode='cumulative_files';
  context.state.sop.draft=draft;
  const payload=vm.runInContext('sopPayload()',context);
  assert.equal(payload.name,'共用目录流程');
  assert.equal(Object.hasOwn(payload,'handoffMode'),false);
});
