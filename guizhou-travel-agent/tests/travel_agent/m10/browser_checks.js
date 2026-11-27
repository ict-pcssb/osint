(async () => {
  const root = document.body.dataset;
  const checks = [];
  const q = selector => document.querySelector(selector);
  const has = (selector, text) => q(selector)?.textContent.includes(text);
  const sleep = () => new Promise(resolve => setTimeout(resolve, 65));
  function assert(condition, desc) {
    if (!condition) throw new Error(desc);
    checks.push(desc);
  }
  async function wait(check, desc) {
    for (let i=0; i<120; i++) {
      if (check()) return;
      await sleep();
    }
    throw new Error(desc + " · " + q("#recheck-status").textContent);
  }
  async function shot(name) {
    root.capture = name;
    await wait(() => root.captured===name, "截图 "+name);
  }
  async function debug(path) {
    return (await fetch("/test/"+path, path==="advance" ? {method:"POST"} : {})).json();
  }

  try {
    await wait(() => q("#traveller-timeline").dataset.revision==="8", "原行程应先显示");
    assert(q("#traveller-timeline .day") && !q("#traveller-timeline [data-error]") &&
      [...document.querySelectorAll("#traveller-timeline .stop")].some(node => node.textContent.includes("观山湖")) &&
      has("#locked-return","18:30"), "原时间轴、站点和锁定返程在核查前可见");
    assert(has("#recheck-status","核查中"), "供应商未返回时原行程可读");
    await shot("opened");
    await wait(() => q("#recheck-decline"), "显示恢复开放的差异");
    assert(has("#recheck-results","恢复开放") && has("#recheck-results","2026-11-29") &&
      has("#recheck-results","2026-11-25"), "变化同时保留原采集时间和适用日期");
    const first = await debug("state");
    assert(first.revision===8 && first.calls===1, "首次打开仅一个供应商调用");
    await shot("changes");

    q("#recheck-decline").click();
    await wait(() => has("#recheck-results","已拒绝"), "显示拒绝记录");
    q("#reload-trip").click();
    await wait(() => has("#recheck-results","已拒绝"), "重开时保留拒绝记录");
    assert(!q("#recheck-decline") && has("#traveller-timeline","观山湖") &&
      JSON.stringify(await debug("state"))===JSON.stringify(first), "拒绝后重开不重复请求，站点、返程和版本不变");
    await shot("declined");

    q("#recheck-manual").click();
    await wait(() => has("#recheck-status","核查中"), "手动创建第二次核查");
    const edited = await debug("advance");
    assert(edited.revision===9, "另一个标签页实际通过 M9 保存到 r9");
    await wait(() => has("#recheck-results","重新核验"), "旧结果提示重新核验");
    assert(has("#recheck-results","r8") && has("#recheck-results","r9") && !q("#recheck-accept") &&
      has("#recheck-results","冲突"), "旧任务关联 r8，保留冲突事实并停用接受");
    assert((await debug("state")).revision===9, "旧核查完成没有覆盖 r9");
    await shot("stale");
    root.verdict = "PASS";
  } catch (error) {
    root.failure = error.message;
    root.verdict = "FAIL";
  } finally {
    root.checks = JSON.stringify(checks);
  }
})();
