(async()=>{
  const ds=document.body.dataset, checks=[];
  const q=s=>document.querySelector(s), text=s=>q(s)?.textContent||"";
  const pause=()=>new Promise(resolve=>setTimeout(resolve,70));
  function check(value,n){if(!value) throw new Error(n);checks.push(n);}
  async function wait(fn,n){
    for(let i=0;i<155;i++){if(fn())return;await pause();}
    throw new Error(n+" · "+text("#status"));
  }
  async function shot(name){q("#trip-panel").scrollIntoView();ds.capture=name;await wait(()=>ds.captured===name,name);}
  async function audit(){
    const response=await fetch("/test/release-state");
    if(!response.ok)throw new Error("缺少隔离数据库核对入口");
    return response.json();
  }
  async function patch(){
    const count=q("#chat-log").children.length;
    q('[data-example="patch_preview"]').click();
    await wait(()=>q("#chat-log").children.length>count&&q(".chat-entry:last-child .workbench-confirm"),"修改预览");
  }
  try{
    await wait(()=>q('[data-example="weather"]'),"统一入口");
    q('[data-example="weather"]').click();
    await wait(()=>q('#chat-log [data-kind="weather"]'),"天气独立卡");
    check(!q('#chat-log [data-kind="weather"] button'),"天气没有行程操作");
    q("#plan-lock").click();
    q("#plan-submit").click();
    await wait(()=>q("#trip-panel .timeline-detail"),"生成两天");
    check(text("#trip-panel").includes("全团预算")&&text("#trip-panel").includes("¥800.00"),"全团预算 800 元");
    [...q("#trip-panel").querySelectorAll("button")].find(b=>b.textContent==="确认保存这份行程").click();
    await wait(()=>q("#trip-panel").dataset.revision==="1","保存 r1");
    const baseline=await audit();
    const initial=baseline.trips.findLast(x=>x.revision===1);
    check(Boolean(initial),"正式保存的首版已进入 SQLite");
    await shot("saved");
    await patch();
    check(JSON.stringify(await audit())===JSON.stringify(baseline),"预览没有新增业务记录");
    q(".chat-entry:last-child .workbench-discard").click();
    await wait(()=>text("#status").includes("已保留原行程"),"拒绝一次");
    check(JSON.stringify(await audit())===JSON.stringify(baseline),"拒绝后完整行程原样");
    await shot("declined");
    await patch();
    q(".chat-entry:last-child .workbench-confirm").click();
    await wait(()=>q("#trip-panel").dataset.revision==="2","确认后 r2");
    const edited=(await audit()).trips.findLast(x=>x.revision===2);
    check(Boolean(edited) && edited.first_day===initial.first_day && edited.locks===initial.locks &&
      edited.second_day!==initial.second_day,"改动仅落在第二天，第一天和锁定返程完全不变");
    q("#reopen-trip").click();
    await wait(()=>text("#workbench-recheck").includes("冲突"),"重开显示复查冲突");
    check((await audit()).trips.findLast(x=>x.revision===2).second_day===edited.second_day &&
      q("#trip-panel").dataset.revision==="2","复查不能修改已保存行程");
    await shot("reopened");
    q("#export-html").click();
    await wait(()=>q("#export-panel a"),"同版本 HTML 导出");
    const content=await (await fetch(q("#export-panel a").href)).text();
    check(content.includes("行程版本 r2")&&content.includes("室内展馆")&&content.includes("待核实"),"路书固定在 r2");
    ds.verdict="PASS";
  }catch(error){ds.failure=error.message;ds.verdict="FAIL";}
  finally{ds.checks=JSON.stringify(checks);}
})();
