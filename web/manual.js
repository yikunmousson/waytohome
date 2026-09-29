(function(){
'use strict';
const M=ManualModel,KEY='waytohome.manual.v1',names={leguang:'广连—许广',xuguang:'许广 G0421',jinggangao:'京港澳 G4',erguang:'二广 G55',unknown:'待核验'};
let records=[],pending=null,dir='outbound',storageOK=true;
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const local=n=>new Date(n+8*3600000).toISOString().slice(0,16),stamp=s=>s.replace('T',' ');
function message(t){$('manual-message').textContent=t;}
try{const raw=localStorage.getItem(KEY);if(raw)records=M.parse(raw);}catch(e){storageOK=false;message('本地记录无法读取，已停止写入以保护原数据。请先检查浏览器存储或备份。');}
function save(next){if(!storageOK)throw Error('本地存储不可用，未保存');localStorage.setItem(KEY,JSON.stringify({version:1,records:next}));records=next;render();}
function reset(){ $('manual-form').reset();$('record-id').value='';$('record-queried').value=local(Date.now());$('record-direction').value=dir;$('save-record').textContent='保存记录'; }
function conditions(){const c={direction:dir,ttl:Number($('filter-ttl').value),rest:Number($('filter-rest').value),start:$('filter-start').value,end:$('filter-end').value,arrival:$('filter-arrival').value};if(!Number.isFinite(c.ttl)||c.ttl<=0||!Number.isFinite(c.rest)||c.rest<0)throw Error('新鲜度须大于零，休息时间不能为负');[c.start,c.end,c.arrival].filter(Boolean).forEach(M.time);if(c.start&&c.end&&M.time(c.start)>M.time(c.end))throw Error('最早出发不能晚于最晚出发');return c;}
function render(){
 let c,valid;try{c=conditions();valid=M.compare(records,c);}catch(e){$('manual-summary').textContent=e.message;$('manual-matrix').innerHTML='';$('manual-records').innerHTML='<p>请修正上方筛选条件后查看记录。</p>';return;}
 const current=records.filter(r=>r.direction===dir), eligible=new Set(valid.map(r=>r.id));
 $('manual-summary').textContent=valid.length?`已录入方案中用时较少：${names[valid[0].corridor]}，${stamp(valid[0].depart)} 出发，行驶 ${valid[0].duration} 分钟。共 ${valid.length} 个满足条件的方案、${new Set(valid.map(r=>r.corridor)).size} 条走廊。`:'暂无满足条件的手动方案。可新增记录；当前自动采样独立可用。';
 const rows=[...current].sort((a,b)=>a.depart.localeCompare(b.depart)||b.queried.localeCompare(a.queried));
 $('manual-records').innerHTML=rows.length?'<table><thead><tr><th>走廊 / 出发</th><th>行驶 / 预计抵达</th><th>来源与依据</th><th>状态 / 操作</th></tr></thead><tbody>'+rows.map(r=>{const state=M.status(r,Date.now(),c.ttl);return `<tr><td>${esc(names[r.corridor])}<br>${esc(stamp(r.depart))}</td><td>${r.duration} 分钟<br>${stamp(local(M.time(r.depart)+(r.duration+c.rest)*60000))}<br>费用 ${r.tolls===null?'未知':r.tolls+' 元'}</td><td>${esc(r.source)} · ${esc(stamp(r.queried))}<br>${esc(r.roads)}<br>${esc(r.note)}</td><td>${state==='有效'?(eligible.has(r.id)?'参与比较':'筛选外 / 较旧版本'):state}<br><button type="button" data-edit="${esc(r.id)}">编辑</button> <button type="button" data-delete="${esc(r.id)}">删除</button></td></tr>`;}).join('')+'</tbody></table>':'<p class="note">本方向尚未录入记录。</p>';
 const times=[...new Set(valid.map(r=>r.depart))].sort();
 $('manual-matrix').innerHTML=times.length?'<table><caption>已录入路线 × 出发时间 · 同列比较同一时刻</caption><thead><tr><th>走廊</th>'+times.map(t=>`<th>${stamp(t)}</th>`).join('')+'</tr></thead><tbody>'+Object.keys(names).filter(k=>k!=='unknown').map(k=>'<tr><th>'+names[k]+'</th>'+times.map(t=>{const r=valid.find(x=>x.corridor===k&&x.depart===t);const min=Math.min(...valid.filter(x=>x.depart===t).map(x=>x.duration));return `<td ${r&&r.duration===min?'class="manual-best"':''}>${r?r.duration+' 分钟':'未录入'}</td>`;}).join('')+'</tr>').join('')+'</tbody></table>':'';
}
$('manual-form').addEventListener('submit',e=>{e.preventDefault();try{const r=M.validate({id:$('record-id').value||crypto.randomUUID(),direction:$('record-direction').value,corridor:$('record-corridor').value,depart:$('record-depart').value,queried:$('record-queried').value,duration:Number($('record-duration').value),source:$('record-source').value,roads:$('record-roads').value,note:$('record-note').value,tolls:$('record-tolls').value===''?null:Number($('record-tolls').value),updated_at:new Date().toISOString()});save(M.merge(records,[r],true));reset();message('已保存到当前浏览器。');}catch(err){message(err.message);}});
$('cancel-record').onclick=reset;
$('manual-records').onclick=e=>{const id=e.target.dataset.edit||e.target.dataset.delete;if(!id)return;const r=records.find(x=>x.id===id);if(!r)return;if(e.target.dataset.delete){if(confirm('删除这条记录？'))try{save(records.filter(x=>x.id!==id));message('已删除');}catch(err){message(err.message);}return;}for(const k of ['id','direction','corridor','depart','queried','duration','source','roads','note','tolls'])$('record-'+k).value=r[k]??'';$('save-record').textContent='保存修改';$('manual-form').scrollIntoView({behavior:'smooth'});};
$('manual-filters').addEventListener('input',render);
$('export-records').onclick=()=>{const blob=new Blob([JSON.stringify({version:1,records},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='waytohome-manual.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('import-records').onchange=async e=>{pending=null;$('import-preview').hidden=true;try{const file=e.target.files[0];if(!file)return;if(file.size>5*1024*1024)throw Error('文件不能超过 5 MB');pending=M.parse(await file.text());const conflicts=pending.filter(r=>records.some(x=>x.id===r.id&&JSON.stringify(x)!==JSON.stringify(r))).length;$('import-summary').textContent=`将导入 ${pending.length} 条记录，其中 ${conflicts} 条同 ID 内容冲突。请选择冲突处理方式后确认。`;$('import-preview').hidden=false;}catch(err){message('导入失败：'+err.message);}finally{e.target.value='';}};
$('confirm-import').onclick=()=>{try{if(!pending)return;save(M.merge(records,pending,$('import-policy').value==='replace'));pending=null;$('import-preview').hidden=true;message('导入完成');}catch(e){message(e.message);}};
$('cancel-import').onclick=()=>{pending=null;$('import-preview').hidden=true;};
window.renderManual=direction=>{dir=direction;$('record-direction').value=dir;render();};
reset();render();setInterval(render,60000);
})();
