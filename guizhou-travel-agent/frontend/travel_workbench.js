"use strict";
const $=selector=>document.querySelector(selector);
const tripPanel=$("#trip-panel"), chatLog=$("#chat-log"), recheckArea=$("#workbench-recheck");
let settings, saved, draft, draftKey, recheckGeneration=0;
const labels11={ack:"确认",weather:"天气",verification:"核验",transfer:"接驳",cost_breakdown:"费用",candidates:"候选",
  itinerary:"规划",clarification:"澄清",pickup_choices:"打车选点",bundle:"组合",patch_preview:"局部修改",recheck_report:"复查",export_result:"路书",
  bad:"未知类型",bad_fields:"缺字段"};

function status11(text) { $("#status").textContent=text; }
function button11(label,action,className) {
  const item=element("button",label,className);
  item.type="button";
  item.addEventListener("click",()=>perform(item,action));
  return item;
}
async function perform(control,action) {
  if(control.disabled) return;
  control.disabled=true;
  try { await action(); } catch(error) { status11(error.message); }
  finally { control.disabled=false; }
}
async function call11(path,body) {
  const response=await fetch(path,body===undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});
  const value=await response.json();
  if(!response.ok){
    const error=new Error(value.error || "操作暂时失败");
    error.status=response.status;
    error.code=value.code;
    throw error;
  }
  if(body===undefined) return value;
  const trace=$("#workbench-trace");
  trace.replaceChildren(...value.trace.map(row=>element("li",[row.event,row.intent,row.skill,row.tool,row.provider,row.status,row.reason].filter(Boolean).join(" · "))));
  return value.value;
}
const tripPath=(id=saved.trip_id)=>"/api/trips/"+encodeURIComponent(id);
const timeValue=value=>value?.slice(11,16) || "待查";

function exportLinks(result) {
  if(!/^[a-f0-9]{32}$/.test(result.artifact_id || "")) return [];
  const url="/api/exports/"+result.artifact_id;
  const html=result.content_type==="text/html; charset=utf-8";
  const view=element("a",html?"打开 HTML 预览 / 浏览器打印":"打开 PDF");
  const download=element("a",html?"下载 HTML":"下载 PDF");
  view.href=url; view.target="_blank"; view.rel="noopener noreferrer";
  download.href=url+"?download=1";
  const wrap=element("div",undefined,"workbench-actions");
  wrap.append(view,download);
  return [wrap];
}

function resultCard(packet,options={}) {
  return renderResult(packet,settings.schema,{
    saved:options.saved,lockedLabels:saved?.locked_labels,
    pick:async (context_id,choice_id)=>{
      try {
        const packet=await call11("/api/choice",{context_id,choice_id});
        addMessage("已选择候车区",packet);
      } catch(error) { status11(error.message); }
    },
    export:exportLinks,
  });
}
function addMessage(message,packet) {
  const entry=element("section",undefined,"chat-entry");
  entry.append(element("p",message,"chat-user"));
  const card=resultCard(packet);
  entry.append(card);
  if(!card.dataset.error && packet.result.kind==="patch_preview") addPatch(card,packet.result);
  chatLog.append(entry);
  chatLog.scrollTop=chatLog.scrollHeight;
  return card;
}

function addPatch(card,result) {
  const {trip_id,proposal_id,base_revision}=result;
  const path=tripPath(trip_id)+"/patch/";
  const fields={proposal_id,base_revision};
  const request={...fields,confirmed:true,idempotency_key:crypto.randomUUID()};
  const actions=element("div",undefined,"workbench-actions");
  const keep=button11("取消修改，保留原行程",async()=>{
    await call11(path+"discard",fields);
    status11("已保留原行程。");
    actions.remove();
  },"workbench-discard");
  const lookup=button11("查询保存状态",async()=>{
    const result=await call11(path+"operation-status",{idempotency_key:request.idempotency_key});
    if(result.save_status==="saved") await finish();
    else {status11("本次尚未查到保存回执。可用同一次确认重试。"); actions.replaceChildren(confirm,lookup);}
  });
  async function finish() {
    await openTrip(trip_id,false);
    status11("已保存此次修改。");
    actions.remove();
  }
  const confirm=button11("确认并保存改动",async()=>{
    keep.disabled=true;
    try {
      await call11(path+"commit",request);
      await finish();
    } catch(error) {
      if(error.status===409 || error.status===410){
        actions.remove(); await openTrip(trip_id,false); status11(error.message);
      } else if(error.status && error.status<500) {
        status11(error.message); keep.disabled=false;
      } else {
        actions.replaceChildren(lookup); status11("保存状态待确认。请先查询本次操作。");
      }
    }
  },"workbench-confirm primary");
  if(result.rule_status!=="conflict") actions.append(confirm);
  actions.append(keep);
  card.append(actions);
}

function timelineExtra(result) {
  const wrap=element("section",undefined,"timeline-detail");
  const pointById=new Map(result.map_points.map(p=>[p.id,p]));
  const stops=result.days.flatMap(day=>day.stops.map((stop,i)=>({
    stop,day,nextLeg:day.stops[i+1]?.incoming_leg || (day===result.days.at(-1) ? result.return_leg : null),
  })));
  const stopById=new Map(stops.map(row=>[row.stop.stop_id,row.stop]));
  const canvas=element("canvas");
  canvas.width=560; canvas.height=245;
  canvas.setAttribute("aria-label","精确入口坐标示意图。可用下面的入口按钮选择。");
  canvas.setAttribute("role","img");
  const mapStatus=paragraph("入口坐标示意。连线用于区分站点，实际耗时以路段数据为准。");
  mapStatus.id="map-status";
  const pins=element("div",undefined,"map-hit-list");
  const selected=element("article",undefined,"result");
  selected.id="selected-stop";
  const days=element("div",undefined,"timeline-stops");
  const points=result.map_points;
  const markers=new Map();
  const bounds=key=>[Math.min(...points.map(p=>p[key])),Math.max(...points.map(p=>p[key]))];
  const [west,east]=bounds("longitude"),[south,north]=bounds("latitude");
  for(const p of points) markers.set(p.id,[24+512*(p.longitude-west)/(east-west||1),221-197*(p.latitude-south)/(north-south||1)]);
  let mapDown=false;

  function failMap() {
    mapDown=true; canvas.hidden=true;
    mapStatus.textContent="地图暂不可用，保留站点列表、入口坐标和外部地图入口。";
  }
  canvas.addEventListener("contextlost",event=>{event.preventDefault();failMap();});
  canvas.addEventListener("click",event=>{
    const rect=canvas.getBoundingClientRect();
    const xy=[(event.clientX-rect.left)*canvas.width/rect.width,(event.clientY-rect.top)*canvas.height/rect.height];
    const rank=stops.map(row=>{
      const pos=markers.get(row.stop.entrance_id);
      return {row,dist:pos ? Math.hypot(xy[0]-pos[0],xy[1]-pos[1]) : Infinity};
    }).sort((a,b)=>a.dist-b.dist);
    if(rank[0]?.dist<31) selectStop(rank[0].row);
  });
  function draw(row) {
    if(mapDown) return;
    const ctx=canvas.getContext("2d");
    if(!ctx || !markers.size) return failMap();
    ctx.clearRect(0,0,560,245);
    const {stop,nextLeg}=row;
    for(const leg of [stop.incoming_leg,nextLeg].filter(Boolean)){
      const a=markers.get(leg.from_id),b=markers.get(leg.to_id);
      if(!a||!b) continue;
      ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);
      ctx.strokeStyle="#488472";ctx.lineWidth=2;ctx.setLineDash([5,5]);ctx.stroke();
    }
    ctx.setLineDash([]);
    points.forEach((p,i)=>{
      const [x,y]=markers.get(p.id),isSelected=p.id===stop.entrance_id;
      ctx.beginPath();ctx.arc(x,y,isSelected?14:9,0,2*Math.PI);ctx.fillStyle=isSelected?"#144f3d":"#73958a";ctx.fill();
      ctx.fillStyle="#fff";ctx.font="12px sans-serif";ctx.textAlign="center";ctx.fillText(String(i+1),x,y+4);
    });
  }
  function legText(leg) {
    if(!leg) return "待查";
    return (pointById.get(leg.from_id)?.label||leg.from_id)+" → "+(pointById.get(leg.to_id)?.label||leg.to_id)+" · "+minutes(leg.travel_min);
  }
  function selectStop(row) {
    const {stop,day,nextLeg}=row;
    canvas.dataset.selectedStop=stop.stop_id;
    wrap.querySelectorAll("[data-stop],[data-map-stop]").forEach(control=>{
      control.setAttribute("aria-pressed",String((control.dataset.stop||control.dataset.mapStop)===stop.stop_id));
    });
    draw(row);
    const point=pointById.get(stop.entrance_id);
    const next=stopById.get(stop.next_stop_id);
    selected.replaceChildren(element("h3",stop.label),rows([["日期",day.date],["入口 ID",stop.entrance_id],
      ["位置",point ? "WGS84 "+point.latitude.toFixed(6)+", "+point.longitude.toFixed(6) : "坐标待查"],
      ["本站时间",timeValue(stop.arrival_local)+"–"+timeValue(stop.departure_local)],
      ["上一段",legText(stop.incoming_leg)],["下一段",legText(nextLeg)],
      ["状态",stop.plan_role==="primary" ? "计划站" : "备选站"],["本站费用",currency(stop.cost.total_fen,stop.cost.currency)]]));
    if(point){
      const url="https://www.openstreetmap.org/?mlat="+point.latitude+"&mlon="+point.longitude+"#map=17/"+point.latitude+"/"+point.longitude;
      selected.append(safeLink(url,"在 OpenStreetMap 查看入口"));
    }
    const nextDetails=element("details");
    nextDetails.append(element("summary","展开下一站"),paragraph(next?.label || "下一站待核实"));
    if(next){
      const move=stops.find(entry=>entry.stop.stop_id===next.stop_id);
      nextDetails.append(button11("定位下一站",async()=>selectStop(move)));
    }
    selected.append(nextDetails);
    if(stop.arrival_local && !stop.completed && !stop.locked && !stop.required){
      const form=element("form",undefined,"workbench-actions");
      for(const [key,title,value] of [["arrival","到达",stop.arrival_local],["departure","离开",stop.departure_local]]){
        const label=element("label",title+" ");
        const input=element("input");input.name=key;input.type="time";input.required=true;input.value=timeValue(value);
        label.append(input);form.append(label);
      }
      const submit=element("button",saved?"预览改时":"更新草稿");submit.type="submit";form.append(submit);
      form.addEventListener("submit",ev=>{
        ev.preventDefault();
        perform(submit,async()=>{
          const times=Object.fromEntries(new FormData(form)), edit={stop_id:stop.stop_id,...times};
          if(saved){
            const packet=await call11(tripPath()+"/patch/retime",{...edit,date:day.date,base_revision:saved.revision});
            addMessage("调整 "+stop.label+" 的时间",packet);
            status11("请在聊天中查看改时结果。");
          }else {
            showTrip(await call11("/api/draft/edit",{draft_id:draft.result.draft_id,...edit}));
          }
        });
      });
      selected.append(form);
    }
  }
  for(const day of result.days){
    days.append(element("h3",day.date));
    const entries=stops.filter(row=>row.day===day);
    days.append(sequence(entries.map(row=>{
      const {stop}=row;
      const control=button11(stop.label+" · "+timeValue(stop.arrival_local)+"–"+timeValue(stop.departure_local),async()=>selectStop(row));
      control.dataset.stop=stop.stop_id;
      return control;
    }),true));
  }
  for(const row of stops.filter(x=>pointById.has(x.stop.entrance_id))){
    const key=points.indexOf(pointById.get(row.stop.entrance_id))+1;
    const pin=button11(key+" · "+row.stop.label,async()=>selectStop(row));
    pin.dataset.mapStop=row.stop.stop_id;
    pins.append(pin);
  }
  wrap.append(element("h3","查看入口与路段"),mapStatus,canvas,pins,days,selected);
  const ret=result.return_leg;
  if(ret) wrap.append(rows([["锁定返程", result.locked_items[0]?.departure_at],["预计到站",timeValue(ret.arrival_at)],["预留进站",minutes(ret.buffer_min)],
    ["全团费用合计",currency(result.budget.total_fen,"CNY","合计待查")]]));
  if(stops.length) selectStop(stops.find(row=>row.stop.stop_id===result.days[0].next_stop_id)||stops[0]);
  return wrap;
}

function showTrip(packet,owned=null) {
  saved=owned;draft=owned?null:packet;
  tripPanel.dataset.revision=owned?.revision || "";
  tripPanel.replaceChildren();
  $("#trip-actions").hidden=!owned;
  if(!packet) return;
  if(owned) tripPanel.append(element("h2","已保存行程 r"+owned.revision));
  const card=resultCard(packet,{saved:Boolean(owned)});
  tripPanel.append(card);
  if(packet.result.kind!=="itinerary" || card.dataset.error) return;
  card.append(timelineExtra(packet.result));
  if(!owned){
    const controls=element("div",undefined,"workbench-actions");
    const save=button11("确认保存这份行程",async()=>{
      draftKey ||= crypto.randomUUID();
      const version=await call11("/api/draft/save",{draft_id:packet.result.draft_id,key:draftKey,confirmed:true});
      showTrip(version.timeline,version);status11("行程已保存。");
    },"primary");
    save.disabled=packet.result.feasibility==="conflict";
    controls.append(save,button11("取消草稿",async()=>{
      await call11("/api/draft/cancel",{draft_id:packet.result.draft_id});showTrip(null);status11("已取消草稿。");
    }));
    card.append(controls);
  }
}

async function openTrip(id,automatic) {
  recheckGeneration++;
  recheckArea.replaceChildren();
  const version=await call11(tripPath(id));
  showTrip(version.timeline,version);
  if(automatic && version.auto_recheck) refresh("open").catch(error=>status11(error.message));
}
async function refresh(trigger,existingRun) {
  const generation=++recheckGeneration;
  const endpoint=tripPath()+"/recheck";
  recheckArea.replaceChildren(paragraph("原行程可查看，正在复查…"));
  let report=existingRun ? await call11(endpoint+"/"+existingRun) :
    await call11(endpoint,{base_revision:saved.revision,trigger});
  while(report.check_state==="running" && generation===recheckGeneration){
    await new Promise(resolve=>setTimeout(resolve,250));
    report=await call11(endpoint+"/"+report.run_id);
  }
  if(generation!==recheckGeneration) return;
  const stale=report.current_revision!==report.base_revision;
  const section=element("article",undefined,"result");
  section.append(element("h2","行程复查"),paragraph(
    ({unchanged:"沿用现有证据",changed:"发现变化",partial:"部分事实待查",unavailable:"目前无法确认"})[report.check_state] || "待查"));
  if(stale) section.append(paragraph("原复查基于 r"+report.base_revision+"，请按当前版本重新核验。"));
  if(report.declined) section.append(paragraph("已拒绝该建议。原行程保留。"));
  for(const row of report.station_changes){
    const names={opening:"开放",weather:"天气",booking:"预约",last_service:"末班",quote:"报价"};
    const value=o=>typeof o?.value==="object" && o?.value!==null ? (o.value.amount_fen!==undefined ?
      currency(o.value.amount_fen,o.value.currency) : o.value.low_c+"–"+o.value.high_c+" °C") : ({open:"开放",closed:"停运或关闭",required:"需预约",not_required:"无需预约"}[o?.value]||o?.value||"待核实");
    section.append(element("h3",row.stop_label+" · "+names[row.kind]),
      rows([["适用日期",row.target_date],["原事实",value(row.previous)],["原采集",row.previous.collected_at],
        ["最新",value(row.latest)],["新采集",row.latest?.collected_at],["本次核查",row.checked_at],
        ["状态",row.status==="conflict"?"冲突":row.status==="pending"?"待查":row.status==="changed"?"有变化":"沿用"]]));
  }
  if(!stale && report.requires_user_choice && report.suggestion_date){
    const route=endpoint+"/"+report.run_id;
    const actions=element("div",undefined,"workbench-actions");
    actions.append(button11("查看替换预览",async()=>{
      addMessage("按复查建议查看预览",await call11(route+"/preview",{}));actions.remove();
    }),button11("保留原安排",async()=>{
      await call11(route+"/decline",{});await refresh("open",report.run_id);
    }));
    section.append(actions);
  }
  recheckArea.replaceChildren(section);
}
async function ask(mode,message) {
  status11("正在处理…");
  const packet=await call11("/api/turn",{mode,message,trip_id:saved?.trip_id || null,plan:planFields()});
  const card=addMessage(message,packet);
  if(!card.dataset.error && packet.result.kind==="itinerary") {
    recheckGeneration++; recheckArea.replaceChildren(); draftKey=null; showTrip(packet);
  }
  if(card.dataset.error) status11("本次结果未通过校验，已有内容保留。");
  else {
    status11("");
    if(saved && packet.result.kind==="recheck_report" && packet.result.run_id) refresh("chat",packet.result.run_id).catch(error=>status11(error.message));
  }
}
function planFields() {
  return {start_date:$("#plan-start").value,budget:Number($("#plan-budget").value),
    return_time:$("#plan-return").value,confirmed:$("#plan-lock").checked};
}
$("#plan-form").addEventListener("submit",event=>{
  event.preventDefault();
  perform($("#plan-submit"),async()=>{
    recheckGeneration++;recheckArea.replaceChildren();draftKey=null;
    const packet=await call11("/api/plan",planFields());
    showTrip(packet);status11("已生成演示时间轴。");
  });
});
$("#chat-form").addEventListener("submit",event=>{
  event.preventDefault();
  perform($("#send"),async()=>{
    const message=$("#chat-input").value.trim();
    const preset=Object.entries(settings.examples).find(([,example])=>example===message);
    await ask(preset && !settings.model ? preset[0] : "model",message);
    $("#chat-input").value="";
  });
});
$("#open-sample").addEventListener("click",()=>perform($("#open-sample"),()=>openTrip(settings.sample_trip,true)));
$("#reopen-trip").addEventListener("click",()=>perform($("#reopen-trip"),()=>openTrip(saved.trip_id,true)));
$("#refresh-trip").addEventListener("click",()=>perform($("#refresh-trip"),()=>refresh("manual")));
for(const fmt of ["html","pdf"]){
  const control=$("#export-"+fmt);
  control.addEventListener("click",()=>perform(control,async()=>{
    status11("正在生成路书…");
    const packet=await call11(tripPath()+"/export",{revision:saved.revision,format:fmt});
    $("#export-panel").replaceChildren(resultCard(packet));
    status11("");
  }));
}
call11("/api/setup").then(config=>{
  settings=config;
  $("#plan-start").value=config.form.start_date;
  $("#plan-budget").value=config.form.budget_fen/100;
  $("#plan-lock").checked=false;
  $("#export-pdf").hidden=!config.pdf && !config.testing;
  const modelName=config.model_provider==="radeon" ? "Radeon" : "百炼";
  $("#mode-note").textContent=config.live ?
    "真实查询模式。演示行程仍标为模拟。"+(config.model?modelName+" 已连接。可输入公开的旅行问题。":"当前无模型 Key，可点击示例发起真实查询。") :
    "固定数据模式。可试天气、接驳、改行程和路书。请输入原样示例，或点按钮。";
  const items={...config.examples};
  if(config.testing){items.bad="测试未知类型";items.bad_fields="测试缺字段";}
  for(const [kind,message] of Object.entries(items)){
    const control=button11(labels11[kind],()=>ask(kind,message));
    control.dataset.example=kind;
    $("#chat-prompts").append(control);
  }
}).catch(error=>status11(error.message));
