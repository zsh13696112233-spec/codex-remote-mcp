/* 配置中心的开发人通知配置及投递状态。 */
const blockedApi = "/api/dingtalk/blocked-notifications";
const blockedLabels = {not_enabled:"未启用",watching:"等待运行结束",pending:"待发送",sending:"正在发送",delivered:"已投递",failed:"失败",unknown:"结果未确认",unavailable:"无法通知",not_applicable:"无需通知"};
async function loadBlockedConfig() {
  const panel=document.querySelector("#blockedConfigPanel");
  try {
    const config=await api(blockedApi+"/config");
    if(!panel?.isConnected)return;
    const people=state.dingtalkTargets.filter(t=>t.targetType==="PERSON"&&t.available);
    panel.innerHTML=`<h2>Jira 开发人通知</h2><p>任务定义指定开发人通知群。全自动成功完成或业务阻断后，按 Jira 开发人显示名精确匹配通讯录中的可用人员，不受人员启用开关影响；同名固定取一人。专用卡片没有继续按钮。</p>
      <form data-blocked-config><label>已发布的专用卡片模板 ID<input name="templateId" maxlength="256" value="${esc(config.templateId)}"></label><button type="submit">保存模板</button></form>
      <form data-blocked-test><h3>测试专用卡片与 @</h3><label>通知群<select name="groupId" required>${blockedGroupOptions()}</select></label><label>通知人员<select name="personId" required><option value="">请选择人员</option>${people.map(p=>`<option value="${esc(p.id)}">${esc(p.displayName)}</option>`).join("")}</select></label><label>测试文案<textarea name="text" required maxlength="2000"></textarea></label><p>测试会真实发送一张卡片，不修改 Jira 或启动任务。</p><button type="submit">发送测试卡片</button><button type="button" data-blocked-new-test>开始另一条测试</button><p data-blocked-test-result role="status"></p></form>`;
    const old=sessionStorage.getItem("blocked-test-id");
    if(old) panel.querySelector("[data-blocked-test-result]").innerHTML=`上次测试：<button type="button" data-blocked-status="test-${esc(old)}">查看投递结果</button>`;
  } catch(error) { if(panel?.isConnected)panel.innerHTML=`<p>${esc(error.message)}</p><button type="button" data-blocked-config-refresh>重新加载</button>`; }
}
function blockedDialog(title) {
  const dialog=document.createElement("dialog");
  dialog.innerHTML=`<button type="button" class="dialog-close" data-dialog-close aria-label="关闭弹窗" title="关闭">×</button><h2>${esc(title)}</h2><div data-blocked-body>正在加载…</div><footer><button type="button" data-blocked-close>关闭</button></footer>`;
  document.body.append(dialog);
  dialog.addEventListener("close",()=>dialog.remove());
  dialog.querySelector("[data-blocked-close]").onclick=()=>dialog.close();
  dialog.showModal();return dialog;
}
async function blockedStatus(id,dialog=blockedDialog("开发人通知详情")) {
  try {const result=await api(blockedApi+"/"+encodeURIComponent(id));
    if(!dialog.isConnected)return;
    dialog.querySelector("[data-blocked-body]").innerHTML=`<p>${esc(blockedLabels[result.state]||"状态未知")}</p><p>${esc(result.reason||"")}</p><button type="button" data-blocked-refresh>刷新状态</button>`;
  }catch(error){if(dialog.isConnected)dialog.querySelector("[data-blocked-body]").innerHTML=`<p>${esc(error.message)}</p><button type="button" data-blocked-refresh>重新加载</button>`;}
  const refresh=dialog.querySelector("[data-blocked-refresh]");if(refresh)refresh.onclick=()=>blockedStatus(id,dialog);
}
document.addEventListener("click",async event=>{
  const statusButton=event.target.closest("[data-blocked-status]");if(statusButton){event.preventDefault();blockedStatus(statusButton.dataset.blockedStatus);return;}
  if(event.target.closest("[data-blocked-config-refresh]")){loadBlockedConfig();return;}
  if(event.target.closest("[data-blocked-new-test]")){sessionStorage.removeItem("blocked-test-id");const f=event.target.closest("form");f.reset();f.querySelector("[data-blocked-test-result]").textContent="下一次发送将创建新测试。";return;}
});
document.addEventListener("submit",async event=>{
  const f=event.target;if(!f.matches("[data-blocked-config],[data-blocked-test]"))return;
  event.preventDefault();if(f.dataset.saving)return;f.dataset.saving="true";const button=f.querySelector('[type="submit"]');button.disabled=true;
  try {
    if(f.matches("[data-blocked-config]")) {await api(blockedApi+"/config",{method:"PUT",body:JSON.stringify({templateId:f.templateId.value.trim()})});toast("开发人通知模板已保存");}
    else {
      let id=sessionStorage.getItem("blocked-test-id");
      if(!id){const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const h=[...bytes].map(b=>b.toString(16).padStart(2,"0")).join("");id=`${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;sessionStorage.setItem("blocked-test-id",id);}
      await api(blockedApi+"/test",{method:"POST",body:JSON.stringify({requestId:id,groupId:f.groupId.value,personId:f.personId.value,text:f.text.value})});
      f.querySelector("[data-blocked-test-result]").innerHTML=`测试已登记；重复提交沿用同一测试。<button type="button" data-blocked-status="test-${esc(id)}">查看投递结果</button>`;
    }
  }catch(error){const out=f.querySelector("[data-error],[data-blocked-test-result]");if(out)out.textContent=error.message;else toast(error.message);}
  finally{delete f.dataset.saving;button.disabled=false;}
});
