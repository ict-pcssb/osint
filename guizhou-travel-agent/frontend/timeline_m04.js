"use strict";
const q = selector => document.querySelector(selector);
let schema, packet, savedId, saveKey;
let pending = false;

async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

function message(text) { q("#status").textContent = text; }

async function action(fn) {
  if (pending) return;
  pending = true;
  try { await fn(); } catch (error) { message(error.message); }
  finally { pending = false; }
}

function plot(points, next, nextLeg) {
  const canvas = q("#map");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0,0,canvas.width,canvas.height);
  canvas.dataset.ids = points.map(point=>point.id).join(",");
  canvas.dataset.next = next || "";
  const ranges = key => [Math.min(...points.map(p=>p[key])), Math.max(...points.map(p=>p[key]))];
  const [x1,x2] = ranges("longitude"), [y1,y2] = ranges("latitude");
  function locate(point) {
    return [26 + (canvas.width-52)*(point.longitude-x1)/(x2-x1 || 1),
            canvas.height-25 - (canvas.height-50)*(point.latitude-y1)/(y2-y1 || 1)];
  }
  if (next && nextLeg) {
    const endpoints = [nextLeg.from_id,nextLeg.to_id].map(id=>points.find(point=>point.id===id));
    if (endpoints.every(Boolean)) {
      ctx.strokeStyle="#205a48";
      ctx.setLineDash([5,4]); ctx.beginPath();
      ctx.moveTo(...locate(endpoints[0])); ctx.lineTo(...locate(endpoints[1])); ctx.stroke(); ctx.setLineDash([]);
    }
  }
  points.forEach((point,i)=>{
    const [x,y] = locate(point);
    ctx.fillStyle="#205a48";
    ctx.beginPath(); ctx.arc(x,y,11,0,2*Math.PI); ctx.fill();
    ctx.fillStyle="#fff"; ctx.font="13px sans-serif"; ctx.textAlign="center";
    ctx.fillText(String(i+1),x,y+4);
  });
  q("#map-legend").replaceChildren(...points.map(p=>element("li",p.label)));
}

function render(response, saved=null) {
  const previousStop = q("#edit-stop").value;
  packet=response; savedId=saved;
  const area=q("#timeline"), result=response?.result;
  area.replaceChildren();
  area.dataset.draft=result?.draft_id || "";
  area.dataset.saved=saved || "";
  const visible = result?.kind==="itinerary";
  q("#map-panel").hidden=!visible;
  q("#edit-form").hidden=!visible || Boolean(saved);
  q("#save-panel").hidden=!visible;
  q("#retry-save").hidden=!saved;
  q("#reopen").hidden=!saved;
  q("#confirm-save").checked=false;
  q("#confirm-save").disabled=Boolean(saved) || !visible || result.feasibility==="conflict";
  q("#save").disabled=true;
  if (!response) return;
  area.append(renderResult(response,schema));
  if (!visible) return;
  const labels = new Map(result.map_points.map(p=>[p.id,p.label]));
  const wrap = element("section",undefined,"result");
  wrap.append(element("h2","路段、费用与返程"));
  for (const day of result.days) {
    wrap.append(element("h3",day.date));
    wrap.append(sequence(day.stops.map(s => {
      const leg=s.incoming_leg;
      const reason={empty:"未找到有效路段",timeout:"路段查询超时",invalid_response:"路段单位或响应无效",endpoint_mismatch:"返回路段接错站或入口"};
      const distance=leg.distance_m===null ? (reason[leg.reason]||"路段待查") : leg.distance_m+" 米 · "+minutes(leg.travel_min);
      return labels.get(leg.from_id)+" → "+labels.get(leg.to_id)+" · "+distance+" · 费用 "+currency(s.cost.total_fen);
    })));
  }
  wrap.append(rows([["锁定返程",time(result.locked_items[0].departure_at)],
                   ["预计到贵阳北站",result.return_leg.arrival_at ? time(result.return_leg.arrival_at) : "返程路段时刻待查"],
                   ["预留进站（估算）",minutes(result.return_leg.buffer_min)],
                   ["费用范围","住宿、餐饮、市内交通、门票"],
                   ["已知至少",currency(result.budget.known_lower_fen)],
                   ["费用合计",currency(result.budget.total_fen,"CNY","总费用待查")]]));
  area.append(wrap);
  const stops=result.days.flatMap(day=>day.stops);
  const next=result.days[0].next_stop_id;
  plot(result.map_points,next,stops.find(stop=>stop.stop_id===next)?.incoming_leg);
  q("#edit-stop").replaceChildren(...stops.map(s=>{
    const option=element("option",s.label); option.value=s.stop_id;
    option.disabled=s.arrival_local===null; return option;
  }));
  if (stops.some(stop => stop.stop_id === previousStop && stop.arrival_local !== null)) {
    q("#edit-stop").value = previousStop;
  }
  setEditTimes();
}

function setEditTimes() {
  const stop=packet?.result.days?.flatMap(day=>day.stops).find(s=>s.stop_id===q("#edit-stop").value);
  q("#edit-arrive").value=stop?.arrival_local?.slice(11,16) || "";
  q("#edit-leave").value=stop?.departure_local?.slice(11,16) || "";
}
q("#edit-stop").addEventListener("change",setEditTimes);
q("#confirm-save").addEventListener("change",event=>{
  q("#save").disabled=!event.target.checked;
});
q("#plan-form").addEventListener("submit",event=>{
  event.preventDefault();
  action(async ()=>{
    schema ||= await api("/schema");
    const get=id=>q("#"+id).value;
    const day_starts=["d1","d2"].map(day=>({entrance_id:"hotel-demo-entry",start:get(day+"-start"),end:get(day+"-end")}));
    const form={destination:"贵阳",adults:2,days:2,budget_scope:"group",budget_fen:Math.round(Number(get("budget"))*100),
      day_starts, end_date:get("end"), return:{entrance_id:"guiyang-north-entry",departure_at:get("end")+"T"+get("return")+":00+08:00",confirmed:q("#confirm-return").checked}};
    if (get("start")) form.start_date=get("start");
    render(await api("/api/preview",form));
    saveKey=null;
    message(packet.result.kind==="itinerary" ? "草稿已生成。" : "还需要一项信息。");
  });
});
q("#edit-form").addEventListener("submit",event=>{
  event.preventDefault();
  action(async ()=>{
    const edited=await api("/api/edit",{draft_id:packet.result.draft_id,stop_id:q("#edit-stop").value,
                                      arrival:q("#edit-arrive").value,departure:q("#edit-leave").value});
    render(edited);
    saveKey=null;
    message(edited.result.feasibility==="conflict" ? "发现冲突，请查看待查事项。" : "草稿已重新校验。");
  });
});
q("#cancel").addEventListener("click",()=>action(async ()=>{
  await api("/api/cancel",{draft_id:packet.result.draft_id}); render(null); saveKey=null; message("已取消草稿。");
}));
function save(repeat) {
  action(async ()=>{
    saveKey ||= crypto.randomUUID();
    const data=await api("/api/save",{draft_id:packet.result.draft_id,key:saveKey,confirmed:true});
    render(data.timeline,data.trip_id);
    message(repeat ? "已复用同一次保存。" : "已保存待核实的行程草稿。");
  });
}
q("#save").addEventListener("click",()=>save(false));
q("#retry-save").addEventListener("click",()=>save(true));
q("#reopen").addEventListener("click",()=>action(async ()=>{
  const data=await api("/api/trip",{trip_id:savedId});
  render(data.timeline,data.trip_id); message("已读取保存的行程。");
}));

