import {sum,monthGroups,eventTotal} from '/model.mjs';
const $=s=>document.querySelector(s);
let plan,etag,csrf,dirty=false,saving=false;
const today=()=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Tokyo'}).format(new Date());
const money=n=>n==null?'未確認':new Intl.NumberFormat('ja-JP',{style:'currency',currency:'JPY',maximumFractionDigits:0}).format(n);
const signed=n=>n==null?'未確認':(n>=0?'＋':'−')+money(Math.abs(n));
const uid=()=>crypto.randomUUID();
function el(tag,props={},children=[]){const node=document.createElement(tag);for(const [k,v]of Object.entries(props)){if(k==='text')node.textContent=v;else if(k==='class')node.className=v;else if(k.startsWith('on'))node.addEventListener(k.slice(2),v);else if(v!==undefined)node.setAttribute(k,v);}for(const child of children)node.append(child);return node;}
function button(text,fn,cls=''){return el('button',{type:'button',text,class:cls,onclick:fn});}
function input(label,value,type='text',oninput){const i=el('input',{type});i.value=value??'';if(type==='number'){i.min='0';i.step='1';i.inputMode='numeric';i.placeholder='未確認';}if(oninput)i.addEventListener('input',()=>oninput(i.value));const wrapper=el('label',{text:label},[i]);return{wrapper,input:i};}
function select(label,value,options,onchange){const s=el('select');for(const [v,text]of options)s.append(el('option',{value:v,text}));s.value=value;s.addEventListener('change',()=>onchange(s.value));return el('label',{text:label},[s]);}
function amount(value){if(value==='')return null;const n=Number(value);if(!Number.isSafeInteger(n)||n<0||n>1e12)throw Error('金額は0以上の整数の円で入力してください');return n;}
function state(text){$('#save-state').textContent=text;}
function error(text){$('#error').hidden=!text;$('#error').textContent=text;}
function changed(){dirty=true;$('#save').disabled=false;state('未保存の変更があります');error('');}
function setSummary(label,value){return el('div',{},[el('small',{text:label}),el('strong',{text:money(value),class:value<0?'negative':''})]);}
function render(){
 if(!plan)return;
 $('#opening').value=plan.opening_balance??'';
 const timeline=$('#timeline');timeline.replaceChildren();
 const ordered=[...plan.events].sort((a,b)=>(a.date??'9999').localeCompare(b.date??'9999'));
 let previous=plan.start_date,cash=plan.opening_balance,worst=0,totalIncome=0,totalExpense=0,hasUnknown=false;
 for(const event of ordered){
  const groups=event.date?monthGroups(plan,previous,event.date):[];
  let income=0,expense=0;
  for(const group of groups){
   const box=el('section',{class:'month','aria-label':group.label+'の収支'},[el('div',{class:'month-heading'},[el('b',{text:group.label}),el('small',{text:group.partial?'イベント日までの月額を日割り':'毎月の収支'})])]);
   for(const row of group.rows){
    if(row.amount==null){hasUnknown=true;if(row.direction==='income')income=null;else expense=null;}
    else if(row.direction==='income'&&income!==null)income+=row.amount;
    else if(row.direction==='expense'&&expense!==null)expense+=row.amount;
    const label=el('span',{class:'flow-label',text:row.rule.label},[el('small',{text:row.override?'この月だけ変更':'毎月の設定'})]);
    const value=row.amount==null?null:(row.direction==='income'?row.amount:-row.amount);
    box.append(el('div',{class:'flow-row'},[label,el('span',{class:'flow-end'},[el('strong',{class:'flow-amount',text:signed(value)}),button('この月を編集',()=>editOverride(row.rule,group.month))])]));
   }
   timeline.append(box);
  }
  if(event.date&&event.date>=previous)previous=event.date;
  const cost=eventTotal(event);
  cash=cash==null||income==null||expense==null||cost==null||!event.date?null:cash+income-expense-cost;
  if(cash!==null)worst=Math.max(worst,-cash);
  if(income===null)totalIncome=null;else if(totalIncome!==null)totalIncome+=income;
  if(expense===null)totalExpense=null;else if(totalExpense!==null)totalExpense+=expense;
  hasUnknown ||=cost==null||!event.date;
  const list=el('ul',{class:'item-list'});
  for(const item of event.items){
   const desc=el('span',{text:item.label},[el('span',{class:'pill',text:item.amount==null?'未確認':item.status==='confirmed'?'確認済み':'概算'})]);
   if(item.note)desc.append(el('small',{class:'item-note',text:item.note}));
   list.append(el('li',{},[desc,el('strong',{text:money(item.amount)})]));
  }
  if(!event.items.length)list.append(el('li',{class:'empty',text:event.zero_cost?'イベント費用なし。毎月の収支とは別のチェックポイントです。':'費用の内訳は未登録です。合計は未確認として扱います。'}));
  const card=el('div',{class:'event-card'},[
   el('div',{class:'event-top'},[el('div',{},[el('p',{class:'event-date',text:event.date??'日程未定'}),el('h2',{text:event.title})]),el('div',{class:'event-total'},[el('small',{text:'イベントの合計'}),document.createTextNode(money(cost))])]),list,
   el('div',{class:'event-bottom'},[el('span',{class:'balance'},[document.createTextNode('支払い後の残り '),el('b',{class:cash<0?'negative':'',text:money(cash)})]),button('内訳を編集',()=>editEvent(event))])]);
  timeline.append(el('section',{class:'event','aria-label':event.title},[card]));
 }
 $('#summary').replaceChildren(setSummary('これからの収入',totalIncome),setSummary('毎月の予定支出',totalExpense),setSummary('追加で必要な資金',hasUnknown||plan.opening_balance==null?null:worst));
 renderRules();
}
function renderRules(){const list=$('#rule-list');list.replaceChildren();for(const rule of plan.monthly_rules){list.append(el('article',{class:'rule'},[el('div',{class:'event-top'},[el('div',{},[el('h3',{text:rule.label}),el('small',{text:`${rule.direction==='income'?'収入':'支出'} / ${rule.start_month} 〜 ${rule.end_month}`})]),el('strong',{text:money(rule.amount)+' / 月'})]),el('p',{class:'small',text:rule.note||'メモなし'}),el('div',{class:'actions'},[button('毎月の設定を編集',()=>editRule(rule)),button('削除',()=>{plan.monthly_rules=plan.monthly_rules.filter(r=>r.id!==rule.id);plan.overrides=plan.overrides.filter(o=>o.rule_id!==rule.id);changed();render();})])]));}}
function openDialog(title,description){$('#dialog-title').textContent=title;$('#dialog-description').textContent=description;$('#editor-fields').replaceChildren();$('#dialog-error').hidden=true;$('#editor-form').onsubmit=null;$('#editor').showModal();}
function dialogError(err){$('#dialog-error').textContent=err.message;$('#dialog-error').hidden=false;}
function editEvent(event){
 const draft=structuredClone(event);const baseline=eventTotal(event);const stamp=today();
 openDialog('内訳を編集：'+event.title,'項目ごとに円で入力します。既存の総額は未分類残額として残し、追加した内訳へ配分します。未分類残額を削除すれば、入力した明細だけの合計になります。');
 const fields=$('#editor-fields');const heading=el('div',{class:'form-grid'});
 heading.append(input('イベント名',draft.title,'text',v=>draft.title=v).wrapper,input('予定日（未定なら空欄）',draft.date,'date',v=>draft.date=v||null).wrapper);fields.append(heading);const zero=el('input',{type:'checkbox'});zero.checked=draft.zero_cost===true;zero.addEventListener('change',()=>{draft.zero_cost=zero.checked;recalc();});fields.append(el('label',{class:'zero-cost'},[zero,document.createTextNode('明細がない場合、イベント費用なしの地点として扱う')]));
 const rows=el('div');fields.append(rows);
 const total=el('div',{class:'dialog-total','aria-live':'polite'});
 function recalc(){
  const rest=draft.items.find(i=>i.unallocated);
  if(rest&&baseline!==null){rest.amount=Math.max(0,baseline-draft.items.filter(i=>!i.unallocated).reduce((n,i)=>n+(i.amount??0),0));}
  const value=eventTotal(draft);
  total.replaceChildren(el('strong',{text:'合計 '+money(value)}));
  if(baseline!==null&&value!==null&&value!==baseline)total.append(el('p',{class:'small',text:'編集前との差額 '+signed(value-baseline)}));
  const unallocated=rows.querySelector('[data-unallocated-amount]');if(unallocated&&rest)unallocated.value=rest.amount??'';
 }
 function draw(){
  rows.replaceChildren();
  for(const item of draft.items){
   const card=el('div',{class:'edit-item'+(item.unallocated?' unallocated':'')});
   const grid=el('div',{class:'fields'});
   const name=input('項目名',item.unallocated?'未分類残額（内訳未確認）':item.label,'text',v=>item.label=v);
   const price=input('金額（円・空欄は未確認）',item.amount,'number',v=>{try{item.amount=amount(v);if(item.amount===null)item.status='unknown';else if(item.status==='unknown')item.status='estimate';const status=card.querySelector('select');if(status)status.value=item.status;recalc();}catch(err){dialogError(err);}});
   if(item.unallocated){name.input.readOnly=true;price.input.readOnly=true;price.input.setAttribute('data-unallocated-amount','');}
   grid.append(name.wrapper,price.wrapper,select('確認状態',item.status,[['unknown','未確認'],['estimate','概算'],['confirmed','確認済み']],v=>item.status=v));
   card.append(grid,el('div',{class:'note-fields'},[input('メモ',item.note??'','text',v=>item.note=v).wrapper,input('出典',item.source??'','text',v=>item.source=v).wrapper]),button(item.unallocated?'未分類残額を削除':'この項目を削除',()=>{draft.items=draft.items.filter(i=>i.id!==item.id);draw();},'remove'));
   rows.append(card);
  }
  recalc();
 }
 fields.append(button('＋ 内訳を追加',()=>{draft.items.push({id:uid(),label:'',amount:null,status:'unknown',note:'',source:'本人による画面入力',updated_at:stamp});draw();}),total);draw();
 $('#editor-form').onsubmit=e=>{e.preventDefault();try{
  if(!draft.title.trim())throw Error('イベント名を入力してください');
  for(const item of draft.items){if(!item.label.trim())throw Error('項目名を入力してください');if(item.amount===null&&item.status==='confirmed')throw Error('未確認の金額を確認済みにできません');item.updated_at=stamp;}
  draft.updated_at=stamp;plan.events[plan.events.findIndex(x=>x.id===event.id)]=draft;changed();render();$('#editor').close();
 }catch(err){dialogError(err);}};
}
function editRule(original){
 const rule=structuredClone(original??{id:uid(),label:'',direction:'expense',amount:null,start_month:plan.start_date.slice(0,7),end_month:'2027-11',status:'unknown',note:'',source:'本人による画面入力',updated_at:today()});
 openDialog(original?'毎月の設定を編集':'毎月の項目を追加','変更は指定した期間の各月に反映します。「この月だけ変更」の設定は優先して維持されます。');
 const fields=$('#editor-fields');const grid=el('div',{class:'form-grid'});
 grid.append(input('項目名',rule.label,'text',v=>rule.label=v).wrapper,select('収入／支出',rule.direction,[['income','収入'],['expense','支出']],v=>rule.direction=v),input('月額（円・空欄は未確認）',rule.amount,'number',v=>{try{rule.amount=amount(v);}catch(err){dialogError(err);}}).wrapper,input('開始月',rule.start_month,'month',v=>rule.start_month=v).wrapper,input('終了月',rule.end_month,'month',v=>rule.end_month=v).wrapper,select('確認状態',rule.status,[['unknown','未確認'],['estimate','概算'],['confirmed','確認済み']],v=>rule.status=v),input('メモ',rule.note,'text',v=>rule.note=v).wrapper,input('出典',rule.source,'text',v=>rule.source=v).wrapper);
 fields.append(grid);
 $('#editor-form').onsubmit=e=>{e.preventDefault();try{
  if(!rule.label.trim()||!rule.start_month||!rule.end_month)throw Error('項目名と適用期間を入力してください');
  if(rule.start_month>rule.end_month)throw Error('終了月は開始月以降にしてください');
  if(rule.amount===null&&rule.status==='confirmed')throw Error('未確認の金額を確認済みにできません');
  rule.updated_at=today();const index=plan.monthly_rules.findIndex(x=>x.id===rule.id);if(index<0)plan.monthly_rules.push(rule);else plan.monthly_rules[index]=rule;changed();render();$('#editor').close();
 }catch(err){dialogError(err);}};
}
function editOverride(rule,month){
 const current=plan.overrides.find(x=>x.rule_id===rule.id&&x.month===month);
 const draft=structuredClone(current??{id:uid(),rule_id:rule.id,month,amount:rule.amount,note:'',source:'本人による画面入力',updated_at:today()});
 openDialog(month+'だけ変更：'+rule.label,'ここで入力するのは、この月全体の月額です。イベントの途中で区切られる場合、表示上は日割りになります。毎月の設定は変わりません。');
 const fields=$('#editor-fields');fields.append(input('この月の金額（円・空欄は未確認）',draft.amount,'number',v=>{try{draft.amount=amount(v);}catch(err){dialogError(err);}}).wrapper,input('変更理由・メモ',draft.note,'text',v=>draft.note=v).wrapper);
 if(current)fields.append(button('月別変更を解除して毎月の設定に戻す',()=>{plan.overrides=plan.overrides.filter(x=>x.id!==current.id);changed();render();$('#editor').close();}));
 $('#editor-form').onsubmit=e=>{e.preventDefault();draft.updated_at=today();const index=plan.overrides.findIndex(x=>x.rule_id===rule.id&&x.month===month);if(index<0)plan.overrides.push(draft);else plan.overrides[index]=draft;changed();render();$('#editor').close();};
}
async function load(){try{const response=await fetch('/api/plan');const data=await response.json();if(!response.ok)throw Error(data.error);plan=data.plan;etag=data.etag;csrf=data.csrf;dirty=false;$('#save').disabled=true;error('');state('DBから読み込み済み・更新番号 '+plan.revision);render();}catch(err){error(err.message);state('読み込みに失敗しました');}}
async function save(){if(!dirty||saving)return;saving=true;$('#save').disabled=true;state('DBへ保存中…');try{
 const response=await fetch('/api/plan',{method:'PUT',headers:{'Content-Type':'application/json','If-Match':etag,'X-CSRF-Token':csrf},body:JSON.stringify(plan)});
 const data=await response.json();if(!response.ok)throw Error(data.error);
 plan=data.plan;etag=data.etag;dirty=false;state('ローカルGitに保存済み '+data.commit+'（GitHub未同期）');render();
 }catch(err){error(err.message);state('未保存：入力内容はこの画面に残っています');}finally{saving=false;$('#save').disabled=!dirty;}}
$('#save').addEventListener('click',save);
$('#reload').addEventListener('click',()=>{if(dirty&&!confirm('未保存の変更を破棄してDBを読み直しますか？'))return;load();});
$('#opening').addEventListener('change',e=>{try{plan.opening_balance=amount(e.target.value);changed();render();}catch(err){error(err.message);}});
$('#add-rule').addEventListener('click',()=>editRule());
for(const id of ['close-dialog','cancel-dialog'])$('#'+id).addEventListener('click',()=>$('#editor').close());
$('#timeline-tab').addEventListener('click',()=>{for(const name of ['timeline','rules']){$('#'+name).hidden=name!=='timeline';$('#'+name+'-tab').setAttribute('aria-pressed',String(name==='timeline'));}});
$('#rules-tab').addEventListener('click',()=>{for(const name of ['timeline','rules']){$('#'+name).hidden=name!=='rules';$('#'+name+'-tab').setAttribute('aria-pressed',String(name==='rules'));}});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
load();
