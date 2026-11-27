(async () => {
  const root = document.body.dataset;
  const checks=[];
  const q = x => document.querySelector(x);
  const texts = x => q(x)?.textContent || "";
  const pause = () => new Promise(resolve => setTimeout(resolve,45));
  function check(ok,desc) { if (!ok) throw new Error(desc); checks.push(desc); }
  async function wait(fn,desc) {
    for(let i=0;i<120;i++){if(fn()) return; await pause();}
    throw new Error(desc+" · "+texts("#status"));
  }
  async function shot(name,selector) {
    if(selector) q(selector)?.scrollIntoView({block:"start"});
    root.capture=name; await wait(()=>root.captured===name,"截图 "+name);
  }
  async function select(kind, result=kind) {
    const count=q("#chat-log").children.length;
    const selector='#chat-log > :last-child [data-kind="'+result+'"]';
    q('[data-example="'+kind+'"]').click();
    await wait(()=>q("#chat-log").children.length>count && q(selector),kind+" 专属卡");
    return selector;
  }

  try {
    await wait(()=>q('[data-example="ack"]'),"加载页面");
    check(Boolean(q("#plan-start") && q("#chat-input")),"同页有首页规划和聊天");
    await shot("home");
    for(const kind of ["ack","weather","verification","transfer","cost_breakdown","candidates","clarification","pickup_choices"]){
      const selector=await select(kind);
      check(!q(selector+" .timeline-detail"),kind+" 独立渲染");
      if(kind==="weather") check(!q(selector+" button"),"天气没有行程操作按钮");
      const captured=["ack","weather","verification","transfer","cost_breakdown","candidates"].includes(kind);
      if(captured && window.innerWidth>600) await shot(kind,selector);
    }
    const bundle=await select("bundle");
    check(q(bundle+' [data-kind="weather_unavailable"]') && q(bundle+' [data-kind="transfer"]'),
      "组合中天气失败，接驳仍显示");
    if(window.innerWidth>600) await shot("bundle",bundle);
    const kept=q("#chat-log").children.length;
    q('[data-example="bad"]').click();
    await wait(()=>q("#chat-log > :last-child [data-error]"),"未知类型受控报错");
    check(q("#chat-log").children.length===kept+1 && q("#chat-log [data-kind='bundle'] [data-kind='transfer']"),
      "坏数据没有清空前一条答复");
    q('[data-example="bad_fields"]').click();
    await wait(()=>q("#chat-log > :last-child [data-error]") && q("#chat-log").children.length===kept+2,"缺字段受控报错");

    q("#plan-lock").click();
    await select("itinerary");
    q("#plan-submit").click();
    await wait(()=>q("#trip-panel [data-kind='itinerary'] .timeline-detail"),"首页生成实际草稿");
    check(texts("#trip-panel").includes("全团预算") && texts("#trip-panel").includes("待查"),"全团预算与未知标志");
    q("#trip-panel [data-stop]").click();
    const stop=q("#trip-panel [data-stop]").dataset.stop;
    check(q("#trip-panel canvas").dataset.selectedStop===stop && texts("#selected-stop").includes("WGS84"),"地图与列表共用 stop_id 和入口坐标");
    q("#trip-panel canvas").dispatchEvent(new Event("contextlost",{cancelable:true}));
    check(texts("#map-status").includes("保留") && q("#trip-panel [data-stop]"),"地图降级保留列表");
    if(window.innerWidth>600) await shot("itinerary","#trip-panel");

    q("#open-sample").click();
    await wait(()=>q("#trip-panel").dataset.revision==="8" && q('[data-example="patch_preview"]'),"打开已保存 r8");
    await wait(()=>q("#workbench-recheck").textContent.includes("复查"),"复查区");
    const patch=await select("patch_preview");
    check(q(patch+" .workbench-confirm") && q(patch+" .workbench-discard"),"预览有明确确认和取消");
    if(window.innerWidth>600) await shot("patch_preview",patch);
    const discard=q(patch+" .workbench-discard");
    if(window.innerWidth<=600){discard.focus();await key("Space");}else discard.click();
    await wait(()=>texts("#status").includes("保留原行程"),"取消保存");
    check(q("#trip-panel").dataset.revision==="8","取消不改版本 · 当前 "+q("#trip-panel").dataset.revision+" · "+texts("#status"));
    const retry=await select("patch_preview");
    const confirm=q(retry+" .workbench-confirm");
    if(window.innerWidth<=600){confirm.focus();await key("Space");}else confirm.click();
    await wait(()=>q("#trip-panel").dataset.revision==="9","显式确认后保存 r9");
    check(texts("#trip-panel").includes("室内展馆"),"确认后仅通过 M9 保存下一版本");
    await select("recheck_report");
    await select("export_result");
    check(q('#chat-log a[href^="/api/exports/"]'),"对话导出显示鉴权下载入口");
    q("#export-pdf").click();
    await wait(()=>q("#export-panel a"),"输出 PDF 故障回退");
    check(texts("#export-panel").includes("PDF 生成失败") && texts("#export-panel").includes("HTML"),"PDF 故障继续提供同版本 HTML");
    if(window.innerWidth>600) await shot("fallback","#export-panel");
    if(window.innerWidth<=600){
      document.documentElement.style.zoom="2";
      await pause();
      const area=q("#trip-panel");
      check(document.documentElement.scrollWidth<=document.documentElement.clientWidth+1,"窄屏 200% 页面无横向裁切");
      const summary=area.querySelector("details summary");
      summary.focus();
      await key("Space");
      await wait(()=>summary.parentElement.open,"键盘展开证据或下一站");
      check(document.activeElement===summary,"键盘焦点保留");
      await shot("zoom","#trip-panel");
    }
    root.verdict="PASS";
  } catch(error){ root.failure=error.message; root.verdict="FAIL"; }
  finally {root.checks=JSON.stringify(checks);}
  async function key(name) {
    delete root.keyDone;
    root.testKey=name;
    await wait(()=>root.keyDone===name,"Chrome 键盘事件");
  }
})();
