"use strict";

const labels = {
  ack: "收到安排", weather: "天气与出行建议", weather_unavailable: "天气待核实",
  verification: "开放与预约核验", transfer: "站到入口的接驳", cost_breakdown: "分项费用",
  candidates: "候选比较", itinerary: "按天行程草稿", patch_preview: "局部修改预览",
  clarification: "还需要确认一项", bundle: "本轮查询", pickup_choices: "选择上下车点",
  recheck_report: "行程复查", export_result: "导出结果", unavailable: "信息待核实",
};
const modes = {walk: "步行", shuttle: "接驳车", bus: "公交", car: "驾车", taxi: "出租车", rail: "铁路", flight: "航班", metro: "地铁"};
const known = {known: "已知", estimate: "估算", unknown: "待查", unconfirmed: "待核实", confirmed: "已确认", contradicted: "与说法不符", conflicted: "来源有冲突", eligible: "符合", ineligible: "不符合"};
const candidateNames = {hotel: "住宿", dining: "餐饮", train: "铁路", flight: "航班", taxi: "打车"};
const failureReasons = {timeout: "查询超时", unbound: "该查询暂未开放", empty: "本次未找到有效信息",
  invalid_response: "返回信息有误", auth_failed: "供应商认证失败", rate_limited: "供应商请求受限",
  quota_exhausted: "查询额度用完", provider_error: "供应商暂不可用", out_of_window: "超过预报窗口"};

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined && value !== null) node.textContent = String(value);
  if (className) node.className = className;
  return node;
}

function rows(list) {
  const dl = element("dl");
  for (const [name, value] of list) {
    if (value !== null && value !== undefined) dl.append(element("dt", name), element("dd", value));
  }
  return dl;
}

function sequence(items, ordered = false) {
  const list = element(ordered ? "ol" : "ul");
  for (const item of items) {
    const li = element("li");
    li.append(item instanceof Node ? item : document.createTextNode(item));
    list.append(li);
  }
  return list;
}

function safeLink(raw, name) {
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" || url.username || url.password ||
        !url.hostname.includes(".") || url.hostname === "localhost" ||
        /\.(?:local|internal|localhost)$/.test(url.hostname) || /^\d+\.\d+\.\d+\.\d+$/.test(url.hostname)) {
      return element("span", "链接待核实");
    }
    const link = element("a", name);
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch {
    return element("span", "链接待核实");
  }
}

function currency(amount, code = "CNY", ifMissing = "费用待查") {
  if (amount === null) return ifMissing;
  return new Intl.NumberFormat("zh-CN", {style: "currency", currency: code}).format(amount / 100);
}
const minutes = value => value === null ? "待查" : value + " 分钟";
const time = value => value ? value.replace("T", " ") : "更新时间待查";
const paragraph = value => element("p", value);
const dayTime = value => value?.slice(11, 16) || "待查";

const components = {
  ack: r => paragraph(r.text),
  weather: r => [
    rows([["目的地", r.place.label + " · " + r.place.coverage], ["目标日", r.target_date],
      ["降雨概率", r.forecast.rain_probability_pct + "%"], ["气温", r.forecast.temperature_c.join(" 至 ") + " °C"],
      ["核查时间", time(r.checked_at)], ["采集时间", r.as_of ? time(r.as_of) : null],
      ["数据更新时间", r.updated_at ? time(r.updated_at) : r.as_of ? null : time(r.updated_at)]]),
    ...(r.forecast.hourly?.length ? [sequence(r.forecast.hourly.map(h => dayTime(h.at)+" · "+h.temperature_c+" °C · 降雨 "+h.rain_probability_pct+"%"))] : []),
    paragraph(r.conclusion), element("p", r.action, "action"),
  ],
  weather_unavailable: r => [rows([["地点", r.place_id], ["日期", r.target_date], ["状态", failureReasons[r.reason] || "天气待查"]]), paragraph(r.action)],
  verification: r => [rows([["核验事项", r.claim], ["日期", r.target_date], ["状态", known[r.status]], ["核查时间", time(r.checked_at)]]),
    paragraph(r.finding), r.booking_entry.state === "verified_url" ? safeLink(r.booking_entry.url, "打开预约入口") : paragraph(r.booking_entry.reason),
    ...(r.official_links || []).map(link => safeLink(link.url, link.kind === "booking" ? "官方预约入口" : "官方信息入口")),
    ...(r.uncertainties || []).map(line => paragraph(line)), paragraph(r.action)],
  transfer: r => [rows([["日期", r.date], ["等车", minutes(r.wait_min)],
    [r.buffers ? "候车内的上车缓冲" : "换乘缓冲", minutes(r.transfer_buffer_min)],
    ...(r.buffers ? [["出站步行", minutes(r.buffers.exit_min)], ["换乘", minutes(r.buffers.transfer_min)],
      ["末段步行", minutes(r.buffers.walk_min)]] : []),
    ["末班", r.last_service.state === "known" ? time(r.last_service.departure_at) : "末班待查"],
    ["预计到达", r.arrival_check.arrival_at ? time(r.arrival_check.arrival_at) : null],
    ["接驳状态", r.feasibility ? ({conflict:"有冲突",needs_verification:"待核实",verified:"已核实"})[r.feasibility] : null],
    ["能否赶上", r.arrival_check.reason]]),
    sequence(r.segments.map(s => s.from_label + " → " + s.to_label + " · " + (modes[s.mode] || s.mode) +
      " · " + minutes(s.travel_min) + " · " + known[s.status]), true),
    ...(r.alternative ? [paragraph("备选 · " + r.alternative.label)] : [])],
  cost_breakdown: r => [
    sequence(r.items.map(item => {
      const type = {ticket: "门票", shuttle: "观光车"}[item.category] || item.category;
      const group = {adults: "成人", children: "儿童", all: "全员", group: "全团"}[item.traveler_group];
      const unit = {person: "人", vehicle: "车", room_night: "房晚", group: "团"}[item.unit];
      return type + " · " + group + " · " + item.quantity + " " + unit + " · " +
        currency(item.subtotal_fen, item.currency) + " · " + (item.missing_reason || known[item.eligibility.status]);
    })),
    rows([["已知至少", currency(r.known_lower_fen, r.currency)], ["合计", currency(r.total_fen, r.currency, "合计待查")]]),
  ],
  candidates: r => [
    rows([["比较范围", candidateNames[r.candidate_kind]],
      ["查询端点", r.basis.origin_label ? r.basis.origin_label+" → "+r.basis.destination_label :
        r.basis.from_label ? r.basis.from_label+" → "+r.basis.to_label : null],
      ["出发时间", r.basis.from_label ? (r.basis.departure_at ? time(r.basis.departure_at) : "即时估价") : null],
      ["入住条件", r.basis.check_out ? r.basis.check_in+" 至 "+r.basis.check_out+" · "+r.basis.adults+" 位成人 · "+r.basis.rooms+" 间房" : null],
      ["用餐时间", r.basis.meal_at ? time(r.basis.meal_at) : null],
      ["报价口径", r.price_meaning],
      ["库存 / 预订", ({unverified: "库存待核实", available: "供应方显示有库存", unavailable: "供应方显示无库存"})[r.inventory_status] + " · 尚未预订"]]),
    sequence(r.options.map(item => {
      const details = {
        hotel: () => [item.label || item.id, item.room_name,
          currency(item.nightly_reference_fen, item.currency || r.currency, "每晚参考待查") + (item.nightly_reference_fen===null ? "" : " / 晚"),
          item.stay_total_fen===null ? "入住总价待查" : "入住总价 "+currency(item.stay_total_fen, item.currency || r.currency),
          "退改：" + (item.cancellation.terms || known[item.cancellation.status] || item.cancellation.status),
          item.inventory_detail==="on_request" ? "库存按需确认" : null,
          "去下一站 " + minutes(item.next_stop_leg.travel_min)].filter(Boolean),
        dining: () => [item.label, item.distance_m === undefined ? null : "直线 "+item.distance_m+" 米",
          item.price_per_person_fen===null ? "人均费用待查" : currency(item.price_per_person_fen, r.currency)+" / 人",
          item.independent_reviews ? "独立到访 "+item.independent_reviews.length+" 条" : null,
          "推广状态 " + (known[item.promotion_status] || item.promotion_status),
          item.uncertainties?.length ? "待核实："+item.uncertainties.join("、") : null].filter(Boolean),
        train: () => [item.train_number, time(item.depart_at), time(item.arrive_at), "门到门 " + minutes(item.door_to_door_min),
          currency(item.reference_fen, r.currency)],
        flight: () => [item.flight_number, time(item.depart_at), time(item.arrive_at), "门到门 " + minutes(item.door_to_door_min),
          currency(item.reference_fen, r.currency)],
        taxi: () => [item.vehicle_class, currency(item.min_fen, r.currency) + " 至 " + currency(item.max_fen, r.currency)],
      }[r.candidate_kind]();
      if (item.connection) {
        details.push("门到门 " + currency(item.total_fen, r.currency, "总价待核实"));
      }
      const prefix = r.provider_status ? "" : (item.id === r.primary_id ? "首选 · " : "备选 · ");
      const visibleId=r.provider_status || item.label || item.unit ? [] : [item.id];
      return prefix + [...visibleId, ...details, item.reason].join(" · ");
    })),
  ],
  itinerary: (r, actions) => [
    rows([["全团预算", currency(r.budget.cap_fen)], ["费用合计", currency(r.budget.total_fen)], ["当前状态", ({verified:"已核验", needs_verification:actions.saved ? "部分事项待核实" : "待核实行程草稿", conflict:"行程有冲突"})[r.feasibility]]]),
    sequence(r.days.map(day => {
      const group = element("div", undefined, "day");
      group.append(element("h3", day.date), sequence(day.stops.map(stop => {
        const line = dayTime(stop.arrival_local) + "–" + dayTime(stop.departure_local) + " · " + (stop.label || stop.poi_id) +
          " · 入口 " + stop.entrance_id + " · " + (stop.plan_role === "primary" ? "计划站" : "备选站") +
          " · " + (modes[stop.incoming_leg.mode] || stop.incoming_leg.mode) + " " + minutes(stop.incoming_leg.travel_min) +
          (stop.incoming_leg.data_mode === "live" ? " · OSM 实测 " + stop.incoming_leg.distance_m + " 米 / " + stop.incoming_leg.duration_s + " 秒" : "") +
          " · " + currency(stop.cost.total_fen, stop.cost.currency) + " · " + (stop.next_stop_id || "下一站待定");
        return element("span", line, "stop");
      })));
      return group;
    }), true), ...(r.pending.length ? [paragraph((r.feasibility === "conflict" ? "冲突及待核实 · " : "待核实 · ") + r.pending.join("、"))] : []),
  ],
  patch_preview: (r, actions) => [
    rows([["范围", r.scope.dates.join("、")], ["原行程版本", r.base_revision],
      [r.route_changes ? "路上时间变化" : "时间影响", r.time_impact.delta_min > 0 ?
        "+" + minutes(r.time_impact.delta_min) : minutes(r.time_impact.delta_min)],
      [r.route_changes ? "预留缓冲后剩余" : "返程缓冲", minutes(r.time_impact.return_buffer_min)],
      ["费用变化", r.cost_impact.delta_fen === null && "after_total_fen" in r.cost_impact ? null :
        currency(r.cost_impact.delta_fen, r.cost_impact.currency, "合计待查") + " · " + known[r.cost_impact.status]],
      ["已知费用变化", r.cost_impact.known_delta_fen == null ? null :
        (r.cost_impact.known_delta_fen > 0 ? "+" : "") + currency(r.cost_impact.known_delta_fen, r.cost_impact.currency)],
      ["全团费用合计", "after_total_fen" in r.cost_impact ?
        currency(r.cost_impact.after_total_fen, r.cost_impact.currency, "合计待查") : null],
      ["另需核实", r.cost_impact.missing_item_ids?.length ?
        r.cost_impact.missing_item_ids.map(id => /^d\d+-ride$/.test(id) ? "第" + id.slice(1, -5) + "天车费待查" : "费用项目待查").join("、") : null],
      ["规则核验", r.rule_codes ? ({conflict:"有冲突，无法保存",needs_verification:"待核实",verified:"通过"})[r.rule_status] : null],
      ["保留原安排", r.unchanged.map(id => actions.lockedLabels?.[id] ||
        ({ "return-ticket": "返程票", "day-1": "第一天" })[id] ||
        (id.startsWith("day:") ? id.slice(4) : "已锁定安排")).join("、")]]),
    sequence(r.changes.map(change => change.before + " → " + change.after)),
    ...(r.route_changes?.length ? [paragraph("受影响路段"), sequence(r.route_changes.map(leg =>
      (leg.stop_id === "locked-return-connection" ? "去返程车站" : "到变更站点") + " · " +
      minutes(leg.before_min) + " → " + minutes(leg.after_min)))] : []),
    ...(r.next_stop_impact || []).filter(item => item.before !== item.after).map(item =>
      paragraph(item.date + " 下一站" + (item.after ? "已更新" : "待重新核实"))),
    ...(r.pending.length ? [paragraph("待核实 · " + r.pending.join("、"))] : []),
  ],
  clarification: r => paragraph(r.question),
  pickup_choices: r => sequence(r.options.map(choice => (choice.role === "pickup" ? "上车" : "下车") + " · " + choice.label)),
  recheck_report: r => r.check_state === "running" ? paragraph("已开始复查。请在行程页查看进度。") :
    sequence(r.findings.map(item => item.before + " → " + (item.after || "待核实"))),
  export_result: r => [paragraph(r.action), ...(r.download_url ? [safeLink(r.download_url, "打开导出文件")] : [])],
  unavailable: r => [paragraph(failureReasons[r.reason] || "这项信息待核实"), paragraph(r.action)],
};

function matches(value, rule, schema) {
  if (rule.$ref) return matches(value, schema.$defs[rule.$ref.split("/").at(-1)], schema);
  if (rule.oneOf) return rule.oneOf.filter(choice => matches(value, choice, schema)).length === 1;
  if ("const" in rule && value !== rule.const) return false;
  if (rule.enum && !rule.enum.includes(value)) return false;
  if (Array.isArray(rule.type)) return rule.type.some(type => matches(value, {...rule, type}, schema));
  switch (rule.type) {
    case "object":
      return value !== null && typeof value === "object" && !Array.isArray(value)
        && (rule.required || []).every(key => Object.hasOwn(value, key))
        && Object.entries(value).every(([key, entry]) => {
          if (Object.hasOwn(rule.properties || {}, key)) return matches(entry, rule.properties[key], schema);
          const additional = rule.additionalProperties;
          return typeof additional === "object" ? matches(entry, additional, schema) : additional !== false;
        });
    case "array":
      return Array.isArray(value) && value.length >= (rule.minItems ?? 0) && value.length <= (rule.maxItems ?? Infinity)
        && (!rule.uniqueItems || new Set(value).size === value.length) && value.every(item => matches(item, rule.items, schema));
    case "string":
      return typeof value === "string" && value.length >= (rule.minLength ?? 0) && value.length <= (rule.maxLength ?? Infinity);
    case "integer":
    case "number":
      return (rule.type === "integer" ? Number.isInteger(value) : Number.isFinite(value))
        && value >= (rule.minimum ?? -Infinity) && value <= (rule.maximum ?? Infinity);
    case "boolean": return typeof value === "boolean";
    case "null": return value === null;
    default: return true;
  }
}

function renderResult(packet, schema, actions={}) {
  if (typeof packet === "string") return element("pre", packet, "legacy");
  if (!matches(packet, schema, schema)) {
    const err = element("p", "当前结果暂无法展示" + (typeof packet?.trace_id === "string" ? " · " + packet.trace_id : ""));
    err.dataset.error = "true";
    return err;
  }

  function component(result) {
    const card = element("article", undefined, "result");
    card.dataset.kind = result.kind;
    card.append(element("h2", result.kind === "itinerary" && actions.saved ? "按天行程" : labels[result.kind]));
    if (result.data_mode === "fixture") card.append(element("p", "模拟数据，仅用于组件测试", "fixture"));
    if (result.kind === "bundle") {
      card.append(component(result.primary), ...result.supplements.map(component));
    } else if (result.kind==="pickup_choices" && actions.pick) {
      card.append(sequence(result.options.map(choice=>{
        const button=element("button",choice.label);
        button.type="button";
        button.addEventListener("click",()=>actions.pick(result.tool_context_id,choice.id));
        return button;
      })));
    } else {
      card.append(...[components[result.kind](result, actions)].flat());
      if (result.kind==="export_result" && actions.export) card.append(...actions.export(result));
    }
    if (result.next_question) card.append(element("p", result.next_question, "action"));
    return card;
  }

  const card = component(packet.result);
  if (packet.result.kind === "ack") return card;
  const evidence = element("details");
  evidence.append(element("summary", packet.evidence.length ? "查看证据" : "暂无证据"));
  for (const item of packet.evidence) {
    evidence.append(paragraph(item.publisher + " · 采集 " + time(item.collected_at) +
      (item.freshness === "expired" ? " · 已过期" : "")), paragraph(item.excerpt));
    if (item.effective_from) evidence.append(paragraph("适用日期 " + item.effective_from +
      (item.effective_to && item.effective_to !== item.effective_from ? " 至 " + item.effective_to : "")));
    if (item.published_at) evidence.append(paragraph("发布时间 " + item.published_at));
    if (item.data_mode !== "fixture") evidence.append(safeLink(item.source_url, "打开来源"));
  }
  card.append(evidence);
  return card;
}

