const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {queued:'排队中',running:'协作分析中',waiting_approval:'等待人工审批',completed:'已完成',completed_with_warning:'已执行 / 总结未完成',needs_attention:'需要人工处理',failed:'执行失败',canceled:'已取消',open:'待处理',resolved:'已解决',escalated:'已转人工'};
const actions = {restore_access:'恢复已授权的账号访问',retry_export:'重新排队导出任务',escalate:'转人工支持处理'};
const roles = {supervisor:'协调员',account:'账号权限专家',platform:'平台作业专家',reviewer:'方案审核员',human:'人工决策',system:'系统',executor:'业务执行器'};
const kinds = {queued:'任务已入队',agent_started:'开始分析',agent_finished:'完成本轮决策',agent_failed:'模型调用失败',agent_interrupted:'模型调用中断',evidence_read:'读取业务证据',finding:'提交调查结论',proposal:'生成处理方案',proposal_read:'读取待审方案',review:'提交审核意见',review_repair_requested:'提醒补交审核意见',review_handoff_blocked:'审核未落库，交接已停止',approval_requested:'请求人工审批',human_decision:'记录人工决策',executed:'执行业务操作',completed:'流程完成',completed_with_warning:'业务已执行，总结未完成',needs_attention:'需要人工介入',failed:'执行失败',recovered:'从检查点恢复',canceled:'任务已取消',workflow_repair:'纠正流程交接',retry_requested:'请求恢复任务',ticket_deleted:'工单移入回收站',ticket_restored:'工单已恢复'};
const active = status => ['queued','running','waiting_approval'].includes(status);
let tickets=[], runs=[], selected=null, current=null, stream=null, generation=0, trash=false, pendingDelete=null;
async function api(path, body, method) {
 const options = body===undefined && !method ? {} : {method:method||'POST',headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})};
 const response=await fetch('/api'+path,options);const data=await response.json();
 if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));return data;
}
function toast(message){$('toast').textContent=message;$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,4500);}
async function refresh(){
 const view=trash;const [allTickets,allRuns,viewTickets,viewRuns]=await Promise.all([api('/tickets'),api('/runs'),view?api('/tickets?deleted=true'):Promise.resolve(null),view?api('/runs?deleted=true'):Promise.resolve(null)]);
 if(view!==trash)return;
 tickets=view?viewTickets:allTickets;runs=view?viewRuns:allRuns;
 $('ticket-count').textContent=allTickets.length;$('pending-count').textContent=allRuns.filter(r=>r.status==='waiting_approval').length;$('done-count').textContent=allRuns.filter(r=>['completed','completed_with_warning'].includes(r.status)).length;
 $('inbox-tab').setAttribute('aria-pressed',String(!trash));$('trash-tab').setAttribute('aria-pressed',String(trash));
 $('tickets').innerHTML=tickets.map(t=>`<article role="button" tabindex="0" aria-label="${esc(t.id+' '+t.title)}" class="ticket ${selected===t.id?'active':''}" data-ticket="${esc(t.id)}"><small>${esc(t.id)} · ${esc(t.priority)} · ${trash?'回收站':labels[t.status]}</small><strong>${esc(t.title)}</strong><p>${esc(t.description)}</p></article>`).join('')||`<p class="muted" style="padding:16px">${trash?'回收站为空':'暂无工单，可新建演示工单。'}</p>`;
 $('history').innerHTML=runs.slice(0,6).map(r=>`<button data-run="${r.id}">${esc(r.ticket_id)} · ${labels[r.status]}</button>`).join('')||'<p class="muted">暂无任务</p>';
 if(selected)showTicket(selected);
}
function showTicket(id){
 selected=id;const t=tickets.find(t=>t.id===id);if(!t)return;
 const deleted=t.deleted_at!=null;const busy=runs.some(r=>r.ticket_id===id&&active(r.status));
 $('empty').hidden=true;$('ticket-detail').hidden=false;$('ticket-id').textContent=t.id;$('ticket-title').textContent=t.title;$('ticket-description').textContent=t.description;$('ticket-status').textContent=deleted?'回收站':labels[t.status];
 $('delete-ticket').hidden=deleted;$('delete-ticket').disabled=busy;$('restore-ticket').hidden=!deleted;
 $('delete-hint').textContent=deleted?'恢复后可继续处理，历史记录已保留。':busy?'有活动任务，完成或取消后可删除。':'';
 $('start').disabled=deleted||busy;$('feedback').disabled=deleted;
 $('retry').hidden=deleted||!current||current.error?.startsWith('STALE_SNAPSHOT:')||!['failed','needs_attention'].includes(current.status);
 document.querySelectorAll('[data-ticket]').forEach(el=>el.classList.toggle('active',el.dataset.ticket===id));
}
function clearRun(){
 generation++;if(stream)stream.close();stream=null;current=null;
 $('timeline').innerHTML='<p class="muted">开始分析后，调查证据和审批记录将在这里实时显示。</p>';
 $('approval').hidden=true;$('answer').hidden=true;$('trace-controls').hidden=true;$('role-stats').innerHTML='';$('role-filter').value='all';$('download-report').removeAttribute('href');
 $('usage').textContent='';$('run-state').textContent='';$('cancel').hidden=true;$('retry').hidden=true;
}
function filterEvents(){document.querySelectorAll('#timeline [data-actor]').forEach(el=>el.hidden=$('role-filter').value!=='all'&&el.dataset.actor!==$('role-filter').value);}
function renderRun(run){
 current=run;$('run-state').textContent=labels[run.status];$('cancel').hidden=!['queued','waiting_approval'].includes(run.status);$('usage').textContent=`${run.model_calls} 次模型决策 · ${run.mode==='scripted'?'模拟模式 / 无API用量':run.tokens+' Token'}`;$('approval').hidden=!run.approval;
 if(run.approval){const p=run.approval.proposal;$('proposal').innerHTML=`<strong>${esc(actions[p.action])}</strong><p>${esc(p.reason)}</p><p>独立审核：${esc(p.review?.reason)}</p><details><summary>查看证据与方案编号</summary><pre>${esc(JSON.stringify(p,null,2))}</pre></details>`;}
 $('approve').disabled=false;$('reject').disabled=false;$('answer').hidden=active(run.status)||!(run.answer||run.error);$('answer').textContent=[run.answer,run.error].filter(Boolean).join('\n');$('answer').classList.toggle('error',!!run.error);
 $('trace-controls').hidden=false;$('download-report').href='/api/runs/'+encodeURIComponent(run.id)+'/report';
 $('role-stats').innerHTML=Object.entries(run.trace?.roles||{}).map(([role,s])=>`<div class="role-card"><strong>${esc(roles[role]||role)}</strong><span>${s.calls} 次调用 · ${s.tokens} Token · ${(s.latency_ms/1000).toFixed(1)} 秒</span><span>${s.failures} 次失败 · ${s.interrupted} 次中断</span><span>${esc(s.models.join(' / ')||'历史记录未保存模型名称')}</span></div>`).join('');
 showTicket(run.ticket_id);
}
async function selectRun(id){
 clearRun();const seq=generation;const run=await api('/runs/'+id);if(seq!==generation)return;
 renderRun(run);$('timeline').innerHTML='';const connection=new EventSource('/api/runs/'+id+'/events');stream=connection;
 connection.onmessage=event=>{
  if(seq!==generation)return;
  const e=JSON.parse(event.data);const el=document.createElement('div');el.className='event';el.dataset.actor=e.actor;
  const detail=e.data.summary||e.data.reason||e.data.answer||e.data.error||e.data.message||(e.data.tools?.length?'调用 '+e.data.tools.join('、'):'');
  el.innerHTML=`<span class="node"></span><div class="content"><small>${new Date(e.created*1000).toLocaleTimeString()}</small><b>${esc(roles[e.actor]||e.actor)} · ${esc(kinds[e.kind]||e.kind)}</b>${detail?'<p>'+esc(detail)+'</p>':''}<details><summary>详细记录</summary><pre>${esc(JSON.stringify(e.data,null,2))}</pre></details></div>`;
  $('timeline').append(el);filterEvents();
  if(['agent_finished','agent_failed','agent_interrupted','approval_requested','completed','completed_with_warning','failed','needs_attention','human_decision','executed','canceled'].includes(e.kind)){
   api('/runs/'+id).then(async latest=>{if(seq!==generation)return;renderRun(latest);await refresh();}).catch(e=>toast(e.message));
  }
 };
 connection.addEventListener('done',()=>connection.close());
}
async function switchView(deleted){trash=deleted;clearRun();selected=null;$('ticket-detail').hidden=true;$('empty').hidden=false;await refresh();if(tickets.length)showTicket(tickets[0].id);}
async function chooseTicket(event){const el=event.target.closest('[data-ticket]');if(!el)return;clearRun();showTicket(el.dataset.ticket);const latest=runs.find(r=>r.ticket_id===selected);if(latest)await selectRun(latest.id);}
$('tickets').onclick=event=>chooseTicket(event).catch(e=>toast(e.message));
$('tickets').onkeydown=event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();chooseTicket(event).catch(e=>toast(e.message));}};
$('history').onclick=event=>{const el=event.target.closest('[data-run]');if(el)selectRun(el.dataset.run).catch(e=>toast(e.message));};
$('start').onclick=async()=>{if(!selected)return;$('start').disabled=true;try{const run=await api('/runs',{ticket_id:selected,feedback:$('feedback').value});await refresh();await selectRun(run.id);}catch(e){toast(e.message);showTicket(selected);}};
async function decide(decision){if(!current?.approval)return;const id=current.id;$('approve').disabled=true;$('reject').disabled=true;try{await api('/runs/'+id+'/decision',{approval_id:current.approval.id,decision,feedback:$('review-feedback').value});$('review-feedback').value='';renderRun(await api('/runs/'+id));await refresh();}catch(e){toast(e.message);$('approve').disabled=false;$('reject').disabled=false;}}
$('approve').onclick=()=>decide('approve');$('reject').onclick=()=>decide('reject');
$('cancel').onclick=async()=>{try{const id=current.id;await api('/runs/'+id+'/cancel',{});await refresh();await selectRun(id);}catch(e){toast(e.message);}};
$('retry').onclick=async()=>{try{const id=current.id;await api('/runs/'+id+'/retry',{});await refresh();await selectRun(id);}catch(e){toast(e.message);}};
$('new-ticket').onclick=()=>$('new-dialog').showModal();$('close-dialog').onclick=()=>$('new-dialog').close();
$('create-ticket').onclick=async()=>{try{const t=await api('/tickets',{template_id:$('template').value,description:$('description').value});$('new-dialog').close();$('description').value='';await switchView(false);showTicket(t.id);}catch(e){toast(e.message);}};
$('inbox-tab').onclick=()=>switchView(false).catch(e=>toast(e.message));$('trash-tab').onclick=()=>switchView(true).catch(e=>toast(e.message));
$('delete-ticket').onclick=()=>{pendingDelete=selected;$('delete-target').textContent=selected+' · '+$('ticket-title').textContent;$('delete-dialog').showModal();};
$('cancel-delete').onclick=()=>$('delete-dialog').close();
$('confirm-delete').onclick=async()=>{const id=pendingDelete;$('confirm-delete').disabled=true;try{await api('/tickets/'+encodeURIComponent(id),undefined,'DELETE');$('delete-dialog').close();await switchView(false);toast('工单已移入回收站，可随时恢复。');}catch(e){toast(e.message);}finally{$('confirm-delete').disabled=false;}};
$('restore-ticket').onclick=async()=>{try{const id=selected;await api('/tickets/'+encodeURIComponent(id)+'/restore',{});await switchView(false);showTicket(id);toast('工单已恢复。');}catch(e){toast(e.message);}};
$('role-filter').onchange=filterEvents;
(async()=>{try{const health=await api('/health');$('mode').textContent=health.mode==='real'?'真实模型 · '+health.model:'确定性演示 · 无模型API调用';await refresh();if(tickets.length)showTicket(tickets[0].id);}catch(e){toast(e.message);}})();
