"use strict";
(() => {
 const q=new URLSearchParams(location.search), w=q.get('workflowId'), d=q.get('documentId');
 const button=document.getElementById('discuss'), panel=document.getElementById('discussion'), status=document.getElementById('chat-status'), input=document.getElementById('chat-text'), send=document.getElementById('chat-send'), list=document.getElementById('chat-messages');
 if(!w||!d)return;
 const url=`/api/workflows/${encodeURIComponent(w)}/documents/${encodeURIComponent(d)}/messages`;
 let pending=null, sending=false, loading=false, sendError='', renderedMessages=null, hasVisibleRender=false;
 function renderMessages(rows){
  const signature=JSON.stringify([rows,window.documentFiles||[],window.documentVersions||[]]);
  if(signature===renderedMessages&&(panel.hidden||hasVisibleRender))return;
  const previousScrollTop=list.scrollTop;
  const firstVisibleRender=!panel.hidden&&!hasVisibleRender;
  const nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<48;
  list.replaceChildren();
  if(!rows.length){
   const empty=document.createElement('div');empty.className='chat-empty';
   const title=document.createElement('strong');title.textContent='还没有沟通记录';empty.append(title);
   const hint=document.createElement('span');hint.textContent='从一个问题或修改建议开始';empty.append(hint);list.append(empty);
  }
  if(window.documentVersions?.length){const first=window.documentVersions[0];const card=document.createElement('button');card.textContent='起始文档 · '+first.name+' · 第 '+first.version+' 版';card.onclick=()=>window.openDocumentVersion(first.version);list.append(card);}
  for(const row of rows){
   for(const message of [{kind:'user',author:'你',text:row.text},{kind:row.reply?'assistant':row.error?'error':'pending',author:'执行者',text:row.reply||row.error||'正在处理，请稍候…'}]){
    const item=document.createElement('div');item.className='chat-entry '+message.kind;
    const author=document.createElement('span');author.className='chat-author';author.textContent=message.author;
    const bubble=document.createElement('div');bubble.className='chat-bubble';bubble.textContent=message.text;
    if(message.kind==='pending'){
     bubble.textContent='正在处理，请稍候';
     const dots=document.createElement('span');dots.className='waiting-dots';dots.ariaHidden='true';
     for(let i=0;i<3;i++){const dot=document.createElement('span');dots.append(dot);}
     bubble.append(dots);
    }
    item.append(author);item.append(bubble);list.append(item);if(message.kind==='user'){item.id='message-'+row.messageId;document.dispatchEvent(new CustomEvent('message-attachments',{detail:{item,row}}));}if(message.kind==='assistant'&&row.version){const b=document.createElement('button');b.textContent='文档已更新 · 第 '+row.version+' 版';b.onclick=()=>window.openDocumentVersion(row.version);item.append(b);}
   }
  }
  if(!panel.hidden&&(nearBottom||firstVisibleRender))list.scrollTop=list.scrollHeight;
  else list.scrollTop=previousScrollTop;
  if(!panel.hidden)hasVisibleRender=true;
  renderedMessages=signature;
 }
 async function load(){
  if(loading||document.hidden)return;loading=true;
  try{
   let after=0, rows=[], data;
   do {const r=await fetch(url+'?after='+after,{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error('暂时无法读取文档沟通。');data=await r.json();rows.push(...data.messages);if(data.messages.length)after=data.messages.at(-1).sequence;}while(data.messages.length===200);
   button.disabled=!data.available;button.title=data.reason||'与原执行者继续沟通';
   status.textContent=sendError||data.reason||(data.busy?'正在处理或等待核对执行结果。':'');send.disabled=sending||!data.available||data.busy;
   status.className=sendError?'warning':'';renderMessages(rows);
   if(pending&&rows.some(row=>row.messageId===pending.messageId)){pending=null;input.value='';window.documentAttachments=[];document.dispatchEvent(new Event('attachments-sent')); }
  }catch(e){status.textContent=e.message;status.className='warning';button.title=e.message;}finally{loading=false;}
 }
 button.addEventListener('click',()=>{panel.hidden=!panel.hidden;document.body.classList.toggle('chat-mode',!panel.hidden);document.getElementById('resources').hidden=panel.hidden;history.replaceState(null,'',location.pathname+location.search.replace(/&chat=1/g,'')+(!panel.hidden?'&chat=1':''));document.dispatchEvent(new Event('chat-open'));if(!panel.hidden){load();input.focus();}});
 send.addEventListener('click',async()=>{
  if(sending||(!input.value.trim()&&!window.documentAttachments?.length))return;
  if(!pending){const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const h=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');pending={messageId:`${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`,text:input.value,attachmentIds:[...(window.documentAttachments||[])]};}
  sending=true;send.disabled=true;send.textContent='发送中…';sendError='';
  try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending),signal:AbortSignal.timeout(15000)});if(!r.ok){if(r.status===400||r.status===409)pending=null;throw Error(r.status===409?'原执行机忙碌或此前执行尚未结束，请稍后重试。':'本次消息未能确认受理，请重试。');}pending=null;input.value='';window.documentAttachments=[];document.dispatchEvent(new Event('attachments-sent')); }
  catch(e){sendError=e.message;status.textContent=e.message;status.className='warning';}
  finally{sending=false;send.disabled=false;send.textContent='发送消息';}
  await load();
 });
 let polls=0;setInterval(()=>{if(!panel.hidden&&!document.hidden&&(send.disabled||++polls%5===0)){load();}},2000);
 document.addEventListener('workspace-loaded',e=>{window.documentFiles=e.detail.files;window.documentVersions=e.detail.versions;if(!panel.hidden)load();});
 document.addEventListener('visibilitychange',load);load();
})();
