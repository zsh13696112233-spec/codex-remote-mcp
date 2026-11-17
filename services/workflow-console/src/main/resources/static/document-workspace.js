'use strict';
(() => {
 const q=new URLSearchParams(location.search), w=q.get('workflowId'), d=q.get('documentId');
 if(!w||!d)return;
 const base=`/api/workflows/${encodeURIComponent(w)}/documents/${encodeURIComponent(d)}/workspace`;
 const el=id=>document.getElementById(id);let files=[],versions=[],initialized=false,readingVersion=null;
 window.documentAttachments=[];
 async function request(query='',options){const r=await fetch(base+query,options);if(!r.ok){let data=await r.json();throw Error(data.error||'资料请求失败');}return r;}
 function button(text,fn){const b=document.createElement('button');b.textContent=text;b.onclick=fn;return b;}
 function selected(){el('selected-files').replaceChildren(...window.documentAttachments.map(id=>button((files.find(f=>f.id===id)?.name||'附件')+' ×',()=>{window.documentAttachments=window.documentAttachments.filter(x=>x!==id);selected();})));}
 function show(node){el('preview-body').replaceChildren(node);el('preview-body').scrollTop=0;if(!el('preview').open)el('preview').showModal();}
 window.openDocumentVersion=async v=>{try{readingVersion=v;const data=await (await request('?action=version&version='+v)).json();const article=document.createElement('article');const h=document.createElement('h2');h.textContent=data.name+' · 第 '+v+' 版';article.append(h);const body=document.createElement('div');if(data.format==='markdown')body.innerHTML=DOMPurify.sanitize(marked.parse(data.content),{ALLOWED_TAGS:['p','br','hr','h1','h2','h3','h4','ul','ol','li','strong','em','del','blockquote','pre','code','table','thead','tbody','tr','th','td','a'],ALLOWED_ATTR:['href','title']});else body.textContent=data.content;article.append(body);show(article);}catch(e){el('chat-status').textContent=e.message;}};
 async function preview(f){try{if(f.media_type==='text/plain'){const pre=document.createElement('pre');pre.textContent=await (await request('?action=file&id='+f.id)).text();show(pre);}else{const img=document.createElement('img');img.src=base+'?action=file&id='+f.id;img.alt=f.name;show(img);}}catch(e){el('chat-status').textContent=e.message;}}

 function fileCard(f){
  const card=document.createElement('div');card.className='file-card';
  const open=()=>{readingVersion=null;preview(f);};
  const heading=button('',open);heading.className='file-heading';heading.title=f.name;
  if(f.media_type.startsWith('image/')){
   const image=document.createElement('img');image.src=base+'?action=file&id='+f.id;
   image.alt='';image.className='attachment-thumb';heading.append(image);
  }else{
   const icon=document.createElement('span');icon.className='file-type';icon.textContent='TXT';heading.append(icon);
  }
  const name=document.createElement('span');name.className='file-name';name.textContent=f.name;heading.append(name);card.append(heading);
  if(!f.messageIds?.length){const state=document.createElement('span');state.className='file-state';state.textContent='待发送';card.append(state);}
  const actions=document.createElement('div');actions.className='file-actions';
  actions.append(button('引用',()=>{if(!window.documentAttachments.includes(f.id)&&window.documentAttachments.length<5)window.documentAttachments.push(f.id);selected();}));
  const download=document.createElement('a');download.href=base+'?action=file&id='+f.id;download.download=f.name;download.textContent='下载';actions.append(download);
  if(f.messageIds?.length){actions.append(button('定位',()=>el('message-'+f.messageIds.at(-1))?.scrollIntoView({block:'center',behavior:'smooth'})));}
  else actions.append(button('移除',async()=>{try{await request('?action=file&id='+f.id,{method:'DELETE'});window.documentAttachments=window.documentAttachments.filter(id=>id!==f.id);await load();}catch(e){el('chat-status').textContent=e.message;}}));
  card.append(actions);return card;
 }
 async function load(){try{const data=await (await request()).json();files=data.files;versions=data.versions;if(el('preview').open&&readingVersion&&versions.at(-1)?.version>readingVersion){let notice=el('new-version');if(!notice){notice=button('有新版本，点击查看',()=>{notice.remove();window.openDocumentVersion(versions.at(-1).version);});notice.id='new-version';el('preview-body').prepend(notice);}}el('versions').replaceChildren(...versions.map((v,i)=>button((i===versions.length-1?'最新文档 · ':'')+'第 '+v.version+' 版',()=>window.openDocumentVersion(v.version))));el('files').replaceChildren(...files.map(fileCard));if(data.jira){el('jira-info').hidden=false;el('jira-info').textContent=[data.jira.issueKey,data.jira.title,data.jira.assignee||'负责人未获取',data.jira.status?.name||'状态未获取','已有记录 · 非实时'].join(' · ');el('jira-info').title='负责人采集时间：'+(data.jira.collectedAt||'未获取')+'；状态采集时间：'+(data.jira.status?.checkedAt||'未获取');if(data.jira.url){const link=document.createElement('a');link.href=data.jira.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=' 打开 Jira';el('jira-info').append(link);}}selected();document.dispatchEvent(new CustomEvent('workspace-loaded',{detail:{files,versions}}));}catch(e){el('chat-status').textContent=e.message;}}
 async function upload(list){for(const f of list){try{const value=await (await request('?action=upload&name='+encodeURIComponent(f.name),{method:'POST',body:f})).json();if(window.documentAttachments.length<5&&!window.documentAttachments.includes(value.id))window.documentAttachments.push(value.id);}catch(e){el('chat-status').textContent=e.message;}}await load();el('upload').value='';}
 el('upload').onchange=()=>upload(el('upload').files);
 el('chat-text').addEventListener('paste',e=>{if(e.clipboardData.files.length){e.preventDefault();upload(e.clipboardData.files);}});
 el('discussion').ondragover=e=>e.preventDefault();el('discussion').ondrop=e=>{e.preventDefault();upload(e.dataTransfer.files);};
 el('chat-text').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!el('chat-send').disabled)el('chat-send').click();}});
 el('toggle-resources').onclick=()=>{el('resources').hidden=!el('resources').hidden;};
 el('close-preview').onclick=()=>el('preview').close();
 document.addEventListener('message-attachments',e=>{for(const id of e.detail.row.attachmentIds||[]){const f=files.find(x=>x.id===id);if(f)e.detail.item.append(button(f.name,()=>preview(f)));}});
 document.addEventListener('attachments-sent',selected);
 document.addEventListener('chat-open',async()=>{if(!el('discussion').hidden&&!initialized){try{await request('',{method:'POST'});initialized=true;await load();}catch(e){el('chat-status').textContent=e.message;}}});
 load().then(()=>{if(q.get('chat')==='1'){el('discuss').disabled=false;el('discuss').click();}});
 setInterval(()=>{if(!document.hidden&&!el('discussion').hidden)load();},10000);
})();
