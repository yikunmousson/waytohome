/* Browser-local records only. No remote prediction service. */
(function(root){
'use strict';
const IDS=['leguang','xuguang','jinggangao','erguang','unknown'];
function time(value){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw Error('请输入完整日期与时间');
  const n=Date.parse(value+'+08:00');
  if(!Number.isFinite(n)||new Date(n+8*3600000).toISOString().slice(0,16)!==value) throw Error('日期或时间无效');
  return n;
}
function validate(r){
  if(!r||typeof r!=='object'||Array.isArray(r)) throw Error('记录格式错误');
  const text=(key,max,required=true)=>{const v=r[key];if(typeof v!=='string'||v.length>max||(required&&!v.trim()))throw Error('字段无效：'+key);return v.trim();};
  const v={id:text('id',100),direction:r.direction,corridor:r.corridor,depart:text('depart',16),queried:text('queried',16),duration:r.duration,source:text('source',100),roads:text('roads',500),note:text('note',1000,false),tolls:r.tolls,updated_at:text('updated_at',40)};
  if(!['outbound','return'].includes(v.direction)||!IDS.includes(v.corridor))throw Error('方向或走廊无效');
  time(v.depart);time(v.queried);
  if(typeof v.duration!=='number'||!Number.isFinite(v.duration)||v.duration<=0||v.duration>10080)throw Error('耗时应在 0 到 10080 分钟之间');
  if(v.tolls!==null&&(typeof v.tolls!=='number'||!Number.isFinite(v.tolls)||v.tolls<0))throw Error('费用无效');
  if(!Number.isFinite(Date.parse(v.updated_at)))throw Error('修改时间无效');
  return v;
}
function parse(s){const obj=JSON.parse(s);if(obj.version!==1||!Array.isArray(obj.records)||obj.records.length>5000)throw Error('不支持的文件版本或记录数量');const rows=obj.records.map(validate);if(new Set(rows.map(r=>r.id)).size!==rows.length)throw Error('文件内有重复记录 ID');return rows;}
function status(r,now,ttl=24){if(r.corridor==='unknown')return '待核验';if(time(r.queried)>now)return '查询时间在未来';if(time(r.queried)>time(r.depart))return '非出发前预测';if(time(r.depart)<=now)return '出发时间已过';if(now-time(r.queried)>ttl*3600000)return '需重新查询';return '有效';}
function compare(rows,{direction,now=Date.now(),ttl=24,start='',end='',arrival='',rest=0}){
 const current=rows.filter(r=>r.direction===direction), latest=new Map();
 current.filter(r=>status(r,now,ttl)==='有效').forEach(r=>{const k=r.corridor+'|'+r.depart,old=latest.get(k);if(!old||time(r.queried)>time(old.queried)||(r.queried===old.queried&&r.updated_at>old.updated_at))latest.set(k,r);});
 return [...latest.values()].filter(r=>(!start||time(r.depart)>=time(start))&&(!end||time(r.depart)<=time(end))&&(!arrival||time(r.depart)+(r.duration+rest)*60000<=time(arrival))).sort((a,b)=>a.duration-b.duration||a.depart.localeCompare(b.depart));
}
function merge(existing,incoming,replace){const m=new Map(existing.map(r=>[r.id,r]));incoming.forEach(r=>{if(!m.has(r.id)||replace)m.set(r.id,r);});if(m.size>5000)throw Error('最多保存 5000 条记录');return [...m.values()];}
const api={time,validate,parse,status,compare,merge};root.ManualModel=api;if(typeof module!=='undefined')module.exports=api;
})(globalThis);
