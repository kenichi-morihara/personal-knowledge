export const eventTotal=event=>event.items.length?sum(event.items):(event.zero_cost===true?0:null);
const DAY=86400000;
export const sum=rows=>rows.some(r=>r.amount==null)?null:rows.reduce((v,r)=>v+r.amount,0);
function monthKey(time){return new Date(time).toISOString().slice(0,7);}
export function monthGroups(plan,from,to){
 const groups=[];const start=Date.parse(from),end=Date.parse(to);if(!Number.isFinite(start)||!Number.isFinite(end)||end<start)return groups;
 const d=new Date(start);d.setUTCDate(1);
 for(let guard=0;d.getTime()<=end&&guard<1212;guard++,d.setUTCMonth(d.getUTCMonth()+1)){
  const ms=d.getTime(),me=Date.UTC(d.getUTCFullYear(),d.getUTCMonth()+1,0),days=new Date(me).getUTCDate();
  const left=Math.max(start,ms-DAY),right=Math.min(end,me),length=Math.max(0,Math.round((right-left)/DAY));if(!length)continue;
  const month=monthKey(ms),first=left<ms?1:new Date(left).getUTCDate()+1,last=right>=me?days:new Date(right).getUTCDate();
  const rows=plan.monthly_rules.filter(r=>r.start_month<=month&&month<=r.end_month).map(rule=>{
   const override=plan.overrides.find(o=>o.rule_id===rule.id&&o.month===month);
   const base=override?override.amount:rule.amount;
   return{rule,override,month,amount:base==null?null:Math.round(base*(last)/days)-Math.round(base*(first-1)/days),direction:rule.direction};
  });
  if(rows.length)groups.push({month,label:month.replace('-','年')+'月'+(length===days?'':` ${first}〜${last}日`),partial:length!==days,rows});
 }
 return groups;
}
