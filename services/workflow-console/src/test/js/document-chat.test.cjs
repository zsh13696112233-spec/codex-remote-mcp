const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const crypto=require('node:crypto').webcrypto;
function fixture(data){
 const elements={},calls=[],documentListeners={};
 function element(){return {scrollHeight:1000,clientHeight:200,scrollTop:0,disabled:false,hidden:true,value:'',textContent:'',children:[],listeners:{},addEventListener(k,f){this.listeners[k]=f},replaceChildren(){this.children=[];this.scrollTop=0},append(x){this.children.push(x)},focus(){},click(){}}}
 for(const id of ['discuss','discussion','chat-status','chat-text','chat-send','chat-messages','refresh','resources'])elements[id]=element();
 const context={window:{},Event:class {},CustomEvent:class {},history:{replaceState(){}},URLSearchParams,AbortSignal,crypto,location:{search:'?workflowId=run&documentId=doc'},document:{body:{classList:{toggle(){}}},dispatchEvent(){},hidden:false,getElementById:id=>elements[id],createElement:element,addEventListener(k,fn){documentListeners[k]=fn}},setInterval(){},fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>data}}};
 vm.runInNewContext(fs.readFileSync(require('node:path').resolve(__dirname,'../../main/resources/static/document-chat.js'),'utf8'),context);
 return {elements,calls,context,documentListeners};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('new document opens discussion and renders replies as text',async()=>{
 const f=fixture({available:true,reason:'',busy:false,messages:[{sequence:1,messageId:'m',text:'修改',reply:'<img src=x>'}]});await settle();
 assert.equal(f.elements.discuss.disabled,false);
 f.elements.discuss.listeners.click();await settle();
 assert.equal(f.elements.discussion.hidden,false);
 assert.match(f.elements['chat-messages'].children[1].children[1].textContent,/<img src=x>/);
 assert.match(f.calls[0].url,/\/api\/workflows\/run\/documents\/doc\/messages/);
});
test('old or active document cannot enter discussion',async()=>{
 const f=fixture({available:false,reason:'任务结束后才可继续沟通。',busy:false,messages:[]});await settle();
 assert.equal(f.elements.discuss.disabled,true);
 assert.equal(f.elements['chat-send'].disabled,true);
 assert.match(f.elements.discuss.title,/结束后/);
});
test('uncertain HTTP submission reuses message ID without duplicate parallel sends',async()=>{
 const f=fixture({available:true,reason:'',busy:false,messages:[]});await settle();
 const sent=[];
 f.context.fetch=async(url,options)=>{if(options.method==='POST'){sent.push(JSON.parse(options.body));throw Error('network')}return {ok:true,json:async()=>({available:true,busy:false,messages:[]})}};
 f.elements['chat-text'].value='修改并上传';
 await f.elements['chat-send'].listeners.click();
 await f.elements['chat-send'].listeners.click();
 assert.equal(sent.length,2);assert.equal(sent[0].messageId,sent[1].messageId);assert.equal(sent[0].text,'修改并上传');
});

test('empty conversation has a clear prompt and busy state prevents sending',async()=>{
 const f=fixture({available:true,busy:true,messages:[]});await settle();
 assert.equal(f.elements['chat-messages'].children[0].className,'chat-empty');
 assert.equal(f.elements['chat-send'].disabled,true);
 assert.match(f.elements['chat-status'].textContent,/正在处理/);
});
test('pending and failed replies have separate visual states',async()=>{
 const f=fixture({available:true,busy:false,messages:[{sequence:1,text:'问题',reply:'',error:''},{sequence:2,text:'修改',reply:'',error:'处理失败'}]});await settle();
 const rows=f.elements['chat-messages'].children;
 assert.equal(rows[0].className,'chat-entry user');
 assert.equal(rows[1].className,'chat-entry pending');
 assert.equal(rows[3].className,'chat-entry error');
 assert.equal(rows[3].children[1].textContent,'处理失败');
});
test('unchanged polling preserves message elements',async()=>{
 const f=fixture({available:true,busy:false,messages:[{sequence:1,text:'问题',reply:'答复'}]});await settle();
 f.elements.discuss.listeners.click();await settle();
 const first=f.elements['chat-messages'].children[0];
 await f.documentListeners.visibilitychange();
 assert.equal(f.elements['chat-messages'].children[0],first);
});


test('workspace polling does not move a reader back to the bottom',async()=>{
 const data={available:true,busy:false,messages:[{sequence:1,messageId:'m',text:'问题',reply:'答复'}]};
 const f=fixture(data);await settle();f.elements.discuss.listeners.click();await settle();
 const list=f.elements['chat-messages'];list.scrollTop=120;
 const first=list.children[0];
 f.documentListeners['workspace-loaded']({detail:{files:[],versions:[]}});await settle();
 assert.equal(list.scrollTop,120);assert.equal(list.children[0],first);
 // A changed attachment causes rebuilding; clearing DOM must not lose scroll position.
 f.documentListeners['workspace-loaded']({detail:{files:[{id:'file',name:'资料.txt'}],versions:[]}});await settle();
 assert.equal(list.scrollTop,120);
 data.messages.push({sequence:2,messageId:'m2',text:'后续问题',reply:'新答复'});
 await f.documentListeners.visibilitychange();
 assert.equal(list.scrollTop,120);
});

test('first visible load and new replies at the bottom follow latest messages',async()=>{
 const data={available:true,busy:false,messages:[{sequence:1,text:'问题',reply:''}]};
 const f=fixture(data);await settle();const list=f.elements['chat-messages'];
 f.elements.discuss.listeners.click();await settle();assert.equal(list.scrollTop,list.scrollHeight);
 list.scrollTop=800;data.messages[0].reply='答复';
 await f.documentListeners.visibilitychange();assert.equal(list.scrollTop,list.scrollHeight);
});
