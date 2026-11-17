// Shared controls for the Jira center's independent lists.
const jiraPagination = {};
function jiraPage(key) {
  if (!jiraPagination[key]) {
    const q = new URL(location.href).searchParams;
    const page = Number(q.get('jiraPage_' + key)) || 1;
    const size = Number(q.get('jiraSize_' + key)) || 10;
    jiraPagination[key] = {page:Number.isSafeInteger(page) && page > 0 ? page : 1, pageSize:[10,20,50,100].includes(size) ? size : 10, total:0};
  }
  return jiraPagination[key];
}
function jiraPageParams(key) { const p=jiraPage(key); return {page:p.page,pageSize:p.pageSize}; }
function resetJiraPage(key) { const p=jiraPage(key);p.page=1;p.generation=(p.generation||0)+1; }
function jiraPageSlice(key, rows) {
  const p=jiraPage(key);p.total=rows.length;p.page=Math.min(p.page,Math.max(1,Math.ceil(p.total/p.pageSize)));
  return rows.slice((p.page-1)*p.pageSize,p.page*p.pageSize);
}
function saveJiraPages() {
  const u=new URL(location.href);
  for(const [key,p] of Object.entries(jiraPagination)) {u.searchParams.set('jiraPage_'+key,p.page);u.searchParams.set('jiraSize_'+key,p.pageSize)}
  history.replaceState(null,'',u);
}
function mountJiraPager(key, selector, result, reload) {
  const host=$(selector);if(!host)return;
  const p=jiraPage(key);Object.assign(p,{page:result.page??p.page,pageSize:result.pageSize??p.pageSize,total:result.total??0});
  const pages=Math.max(1,Math.ceil(p.total/p.pageSize));
  const html=`<span>共 ${p.total} 条 · 第 ${p.page} / ${pages} 页</span><div class="actions"><label>每页 <select aria-label="每页条数">${[10,20,50,100].map(n=>`<option${n===p.pageSize?' selected':''}>${n}</option>`).join('')}</select> 条</label><button type="button" data-page="${p.page-1}" ${p.page<=1?'disabled':''}>上一页</button><button type="button" data-page="${p.page+1}" ${p.page>=pages?'disabled':''}>下一页</button><label>跳至 <input aria-label="跳转页码" type="number" min="1" max="${pages}" value="${p.page}"> 页</label><button type="button" data-jump>跳转</button></div>`;
  if(host.jiraMarkup!==html){host.innerHTML=html;host.jiraMarkup=html}
  async function change(page,size=p.pageSize) {
    if(p.busy||!Number.isSafeInteger(page)||page<1||page>(size===p.pageSize?pages:1))return;
    const generation=p.generation,old={page:p.page,pageSize:p.pageSize};p.page=page;p.pageSize=size;p.busy=true;
    host.querySelectorAll('button,input,select').forEach(n=>n.disabled=true);
    try { const success=await reload();if(generation===p.generation){if(success===false)Object.assign(p,old);else saveJiraPages()} }
    finally {p.busy=false;if(generation===p.generation&&host===$(selector)){host.jiraMarkup=null;mountJiraPager(key,selector,p,reload)}}
  }
  host.onclick=e=>{const b=e.target.closest('button');if(!b||b.disabled)return;change(b.hasAttribute('data-jump')?Number(host.querySelector('input').value):Number(b.dataset.page))};
  host.onchange=e=>{if(e.target.tagName==='SELECT')change(1,Number(e.target.value))};
  host.onkeydown=e=>{if(e.key==='Enter'&&e.target.tagName==='INPUT'){e.preventDefault();change(Number(e.target.value))}};
}
