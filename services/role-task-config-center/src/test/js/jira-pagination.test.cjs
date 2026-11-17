const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function fixture(url='http://localhost/') {
  const input={value:'1',tagName:'INPUT'},select={tagName:'SELECT'},host={innerHTML:'',querySelector:()=>input,querySelectorAll:()=>[input,select]};
  const location={href:url};
  const ctx=vm.createContext({URL,location,history:{replaceState(a,b,u){location.href=String(u)}},$:()=>host});
  vm.runInContext(fs.readFileSync('src/main/resources/static/jira-pagination.js','utf8'),ctx);
  return {host,input,ctx,run:s=>vm.runInContext(s,ctx),click(page){host.onclick({target:{closest:()=>({disabled:false,hasAttribute:()=>false,dataset:{page:String(page)}})}})}};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('pager sends selected page and size, persists URL and isolates other lists',async()=>{
 const h=fixture();let requested;
 h.ctx.reload=async()=>{requested=h.run("jiraPageParams('pool')");return true};
 h.run("mountJiraPager('pool','#pager',{total:61,page:1,pageSize:20},reload)");
 h.click(3);await settle();assert.equal(requested.page,3);assert.match(h.ctx.location.href,/jiraPage_pool=3/);
 assert.equal(h.run("jiraPage('history').page"),1);
 h.host.onchange({target:{tagName:'SELECT',value:'50'}});await settle();
 assert.equal(requested.pageSize,50);assert.equal(requested.page,1);
});
test('failed request restores page and repeated clicks do not issue duplicate requests',async()=>{
 const h=fixture();let resolve,calls=0;
 h.ctx.reload=()=>{calls++;return new Promise(r=>resolve=r)};
 h.run("mountJiraPager('pool','#pager',{total:80,page:1,pageSize:20},reload)");
 h.click(2);h.click(3);assert.equal(calls,1);resolve(false);await settle();
 assert.equal(h.run("jiraPage('pool').page"),1);assert.match(h.host.innerHTML,/第 1 \/ 4 页/);
});
test('local lists clamp after deletion and recover valid URL page sizes',()=>{
 const h=fixture('http://localhost/?jiraPage_groups=9&jiraSize_groups=50');
 h.ctx.rows=Array.from({length:55},(_,i)=>i);
 const rows=h.run("jiraPageSlice('groups',rows)");
 assert.equal(rows.length,5);assert.equal(rows[0],50);assert.equal(h.run("jiraPage('groups').page"),2);
 h.ctx.rows=[];h.run("jiraPageSlice('groups',rows)");assert.equal(h.run("jiraPage('groups').page"),1);
});
test('out-of-range jump is rejected and unchanged refresh keeps the input draft',()=>{
 const h=fixture();let calls=0;h.ctx.reload=()=>{calls++;return true};
 h.run("mountJiraPager('pool','#pager',{total:21,page:1,pageSize:20},reload)");
 h.input.value='9';h.host.onkeydown({key:'Enter',target:h.input,preventDefault(){}});assert.equal(calls,0);
 h.run("mountJiraPager('pool','#pager',{total:21,page:1,pageSize:20},reload)");assert.equal(h.input.value,'9');
});
test('a filter reset supersedes pending navigation without restoring the previous scope page',async()=>{
 const h=fixture();let resolve;
 h.ctx.reload=()=>new Promise(r=>resolve=r);
 h.run("mountJiraPager('pool','#pager',{total:80,page:2,pageSize:20},reload)");h.click(3);
 h.run("resetJiraPage('pool')");resolve(false);await settle();
 assert.equal(h.run("jiraPage('pool').page"),1);
});

test('new lists default to ten rows',()=>{
 const h=fixture();assert.equal(h.run("jiraPage('pool').pageSize"),10);
 h.ctx.rows=Array.from({length:25},(_,i)=>i);assert.equal(h.run("jiraPageSlice('groups',rows)").length,10);
});
