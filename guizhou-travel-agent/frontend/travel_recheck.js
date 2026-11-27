const recheckResults = document.querySelector("#recheck-results");
const recheckStatus = document.querySelector("#recheck-status");
const reopen = document.querySelector("#reload-trip");
const manual = document.querySelector("#recheck-manual");
const reasonText = {reused:"沿用原证据",rechecked:"已复查",unbound:"可用查询来源待配置",
  empty:"暂未查到",outside_effective_period:"未找到覆盖出游日的资料",timeout:"查询超时",
  rate_limited:"供应商限流",invalid_response:"返回资料不完整",out_of_window:"超出查询窗口",
  source_conflict:"来源有冲突",budget_exhausted:"本次查询额度已用完",stale:"资料已过期"};
const factNames = {opening:"开放状态",booking:"预约",weather:"天气",last_service:"末班车",quote:"报价"};
let active = 0;

function readable(value) {
  if (value == null) return "待查";
  if (typeof value === "object") return Object.hasOwn(value,"amount_fen") ?
    currency(value.amount_fen,value.currency) : value.low_c+" 至 "+value.high_c+" °C · 降雨 "+value.rain_pct+"%";
  return ({open:"开放",closed:"停运或关闭",required:"需要预约",not_required:"无需预约"})[value] || value;
}

function showRecheck(details, endpoint) {
  const stale = details.current_revision !== details.base_revision;
  const fragment = document.createDocumentFragment();
  const suffix = {changed:"有新变化",partial:"部分事实待查",unavailable:"目前无法确认",unchanged:"无新变更"};
  fragment.append(element("h2","复查结果 · "+suffix[details.check_state]));
  if (stale) fragment.append(paragraph("本次复查基于 r"+details.base_revision+"，当前行程已是 r"+details.current_revision+"。请重新核验后再预览。"));
  if (details.declined) fragment.append(paragraph("已拒绝这项建议，原行程保留。"));
  for (const row of details.station_changes) {
    const card = element("article",undefined,"result "+row.status);
    const before = row.previous;
    const after = row.latest;
    const lead = row.status==="conflict" ? "冲突" : row.kind==="opening" && after?.value==="open"
      && before.value==="closed" ? "恢复开放" : row.status==="pending" ? "待查" : row.status==="changed" ? "有变化" : "沿用";
    card.append(element("h3",row.stop_label+" · "+factNames[row.kind]+" · "+lead),
      rows([["行程日期",row.target_date],["原值",readable(before.value)+(row.data_freshness==="stale" ? "（历史参考）" : "")],
        ["原采集时间",before.collected_at || "未知"],["原证据有效期至",before.valid_until || "待核实"],
        ["原文摘录",before.excerpt || null],["最新值",after ? readable(after.value) : null],
        ["新证据采集时间",after?.collected_at],["新证据适用期",after?.valid_from ?
          after.valid_from+" 至 "+(after.valid_to || "未注明结束") : null],
        ["本次核查时间",row.checked_at],["核查结果",reasonText[row.reason] || "待进一步核实"]]));
    if (before.source_url?.startsWith("https://")) card.append(safeLink(before.source_url,"查看原来源"));
    if (after?.source_url?.startsWith("https://")) card.append(safeLink(after.source_url,"查看新来源"));
    fragment.append(card);
  }
  if (!stale && details.requires_user_choice) {
    const actions=element("div",undefined,"patch-actions");
    if (details.suggestion_date) actions.append(patchButton("recheck-accept","查看替换预览",async () => {
      try {
        const preview = await patchRequest(endpoint+"/"+details.run_id+"/preview",{});
        showPreview(preview);
        if (preview.result.kind==="patch_preview") {
          showRecheck({...details,requires_user_choice:false},endpoint);
          document.querySelector("#patch-preview").scrollIntoView();
        }
      }
      catch (error) { recheckStatus.textContent=error.message; }
    }));
    if (details.suggestion_date) actions.append(patchButton("recheck-decline","保留原行程",async () => {
      try { showRecheck(await patchRequest(endpoint+"/"+details.run_id+"/decline",{}),endpoint); }
      catch (error) { recheckStatus.textContent=error.message; }
    }));
    if (!actions.childElementCount) actions.append(paragraph("请核对冲突后自行选择调整方案。"));
    fragment.append(actions);
  }
  recheckResults.replaceChildren(fragment);
}

async function refreshTrip(trigger) {
  const work = ++active;
  reopen.disabled = manual.disabled = true;
  try {
    await patchReady;
    showTimeline(await patchRequest("/api/trip"));
    recheckResults.replaceChildren();
    if (trigger==="open" && !trip.auto_recheck) {
      recheckStatus.textContent="自动复查已关闭。可以手动复查。";
      return;
    }
    const endpoint="/api/travel/trips/"+encodeURIComponent(trip.trip_id)+"/recheck";
    recheckStatus.textContent="已显示原行程，正在核查中…";
    let report = await patchRequest(endpoint,{base_revision:trip.revision,trigger});
    while (report.check_state==="running" && work===active) {
      await new Promise(resolve => setTimeout(resolve,250));
      report = await patchRequest(endpoint+"/"+report.run_id);
    }
    if (work!==active) return;
    if (trip.revision!==report.current_revision) showTimeline(await patchRequest("/api/trip"));
    showRecheck(report,endpoint);
    recheckStatus.textContent="";
  } catch (error) {
    recheckStatus.textContent=error.message;
  } finally {
    if (work===active) reopen.disabled = manual.disabled = false;
  }
}

reopen.addEventListener("click",()=>refreshTrip("open"));
manual.addEventListener("click",()=>refreshTrip("manual"));
refreshTrip("open");
