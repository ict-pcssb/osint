(async () => {
  const checks = [];
  const pause = () => new Promise(resolve => setTimeout(resolve, 70));
  function assert(condition, label) {
    if (!condition) throw new Error(label);
    checks.push(label);
  }
  async function wait(predicate, label) {
    for (let tick = 0; tick < 90; tick++) {
      if (predicate()) return;
      await pause();
    }
    throw new Error(label + " · " + document.querySelector("#patch-status").textContent);
  }
  async function capture(name) {
    document.querySelector(name === "preview" ? "#patch-preview" : "#traveller-timeline").scrollIntoView();
    document.body.dataset.capture = name;
    await wait(() => document.body.dataset.captured === name, "截图 " + name);
  }
  const query = selector => document.querySelector(selector);
  const revision = value => query("#traveller-timeline").dataset.revision === value;
  const has = (selector, string) => query(selector).textContent.includes(string);
  const counts = async expected =>
    JSON.stringify(await (await fetch("/test/counts")).json()) === JSON.stringify(expected);
  const unchanged = async () => revision("7") && has("#traveller-timeline", "观山湖") && await counts([1, 7, 1]);

  try {
    await wait(() => revision("7"), "原始行程 r7");
    const firstDay = query("#traveller-timeline .day").textContent;
    const locked = query("#locked-return").textContent;
    assert(locked.includes("18:30") && locked.includes("D-FIXTURE"), "原返程车次和出发时间在页面可见");
    query("#patch-form button").click();
    await wait(() => query("#patch-confirm"), "局部差异预览");
    assert(has("#patch-preview", "观山湖") && has("#patch-preview", "室内展馆") &&
      has("#patch-preview", "已锁定返程票") && has("#patch-preview", "已锁定酒店"), "展示修改前后和明确保留的安排");
    assert(/\+[^\d]*60\.00/.test(query("#patch-preview").textContent) &&
      has("#patch-preview", "合计待查") && has("#patch-preview", "第2天车费待查") &&
      has("#patch-preview", "下一站待重新核实"), "已知增加60元、车费和合计待查、下一站重算");
    assert(await unchanged(), "预览阶段真实 SQLite 计数保持 1/7/1");
    assert(document.documentElement.scrollWidth <= window.innerWidth, "视口无横向溢出");
    await capture("preview");
    query("#patch-discard").click();
    await wait(() => has("#patch-status", "已保留原行程"), "拒绝预览成功");
    assert(await unchanged(), "拒绝后原行程和 SQLite 计数均不变");
    await capture("declined");

    query("#patch-form button").click();
    await wait(() => query("#patch-confirm"), "重新预览");
    const endpoint = "/api/travel/trips/" + encodeURIComponent(trip.trip_id) + "/patch/";
    const secondTab = await patchRequest(endpoint + "preview", {base_revision:7,
      instruction:query("#patch-message").value});
    query("#patch-confirm").click();
    await wait(() => query("#patch-operation"), "服务端已保存但响应断开");
    assert(revision("7") && await counts([1, 8, 2]), "收到确认前页面保留原行程，服务器只保存一版");
    query("#patch-operation").click();
    await wait(() => revision("8"), "查询本次操作后展示 r8");
    const stops = [...document.querySelectorAll("#traveller-timeline .stop")].map(stop => stop.textContent);
    assert(has("#patch-status", "已保存此次修改"), "查到该操作的保存回执");
    assert(stops.some(stop => stop.includes("室内展馆")) &&
      !stops.some(stop => stop.includes("观山湖")) && has("#traveller-timeline", "下一站待重新核实"),
      "按原操作键确认 r8 后第二天才替换");
    assert(query("#traveller-timeline .day").textContent === firstDay &&
      query("#locked-return").textContent === locked && await counts([1, 8, 2]), "第一天及返程原样保留，操作回查不重复写入");
    const stale = await fetch(endpoint + "commit", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({proposal_id:secondTab.result.proposal_id,base_revision:7,
        idempotency_key:crypto.randomUUID(),confirmed:true})});
    const rejected = await stale.json();
    assert(stale.status === 409 && rejected.save_status === "conflict" && await counts([1, 8, 2]),
      "另一个 r7 提案的真实 HTTP 请求返回 409 且没有覆盖 r8");
    await capture("committed");
    document.body.dataset.verdict = "PASS";
  } catch (error) {
    document.body.dataset.failure = error.message;
    document.body.dataset.verdict = "FAIL";
  } finally {
    document.body.dataset.checks = JSON.stringify(checks);
  }
})();

