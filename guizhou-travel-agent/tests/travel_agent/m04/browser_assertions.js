(async () => {
  const checks = [], rules = [];
  async function capture(name) {
    document.body.dataset.m06Capture = name;
    await until(() => document.body.dataset.m06Captured === name);
    delete document.body.dataset.m06Capture;
  }
  async function until(condition) {
    for (let i=0; i<100; i++) {
      if (condition()) return;
      await new Promise(resolve => setTimeout(resolve,50));
    }
    throw new Error("UI did not reach the expected state");
  }
  const q = selector => document.querySelector(selector);
  const count = async () => (await api("/api/test-counts")).join(",");
  const run = selector => q(selector).requestSubmit();
  const previewed = () => q("#timeline [data-kind=itinerary]") && !q("#timeline").dataset.saved;
  try {
    q("#confirm-return").checked = true;
    run("#plan-form");
    await until(previewed);
    checks.push(q("#timeline").textContent.includes("模拟数据") && document.querySelectorAll("#timeline .day").length===2);
    checks.push(q("#map").dataset.ids.includes("guiyang-north-entry") && q("#map").dataset.next==="");
    checks.push(await count()==="0,0,0");
    checks.push(q("#timeline").textContent.includes("18:30") && q("#timeline").textContent.includes("680"));
    rules.push(packet.result.validation_report.feasibility === "needs_verification" &&
      q("#timeline").textContent.includes("待核实行程草稿") && packet.result.validation_report.data_mode === "fixture");
    await capture("needs_verification");
    const originalDraft = q("#timeline").dataset.draft;
    q("#edit-arrive").value="10:05";
    q("#edit-leave").value="10:55";
    run("#edit-form");
    await until(() => q("#timeline").dataset.draft!==originalDraft);
    checks.push(q("#timeline").textContent.includes("10:05") && await count()==="0,0,0");
    q("#cancel").click();
    await until(() => !q("#timeline [data-kind=itinerary]"));
    checks.push(await count()==="0,0,0");
    run("#plan-form");
    await until(previewed);
    checks.push(q("#timeline").textContent.includes("10:00"));
    checks.push(q("#save").disabled);
    const original = q("#timeline").dataset.draft;
    q("#edit-stop").value="d2-park";
    q("#edit-arrive").value="18:00";
    q("#edit-leave").value="18:25";
    run("#edit-form");
    await until(() => q("#timeline").dataset.draft!==original);
    const blocked = q("#timeline").dataset.draft;
    rules.push(packet.result.validation_report.rule_results.some(row=>row.reason_code==="LOCKED_RETURN_TIME_CONFLICT") &&
      packet.result.validation_report.affected_stop_ids.join(",")==="d2-park");
    rules.push(q("#timeline").textContent.includes("行程有冲突") && q("#timeline").textContent.includes("18:30") &&
      q("#save").disabled && q("#confirm-save").disabled && q("#edit-stop").value==="d2-park");
    let rejected = false;
    try { await api("/api/save",{draft_id:blocked,key:"conflict-browser",confirmed:true}); }
    catch (error) { rejected = error.message.includes("冲突"); }
    rules.push(rejected && await count()==="0,0,0");
    await capture("conflict");
    q("#cancel").click();
    await until(() => !q("#timeline [data-kind=itinerary]"));
    run("#plan-form");
    await until(previewed);
    q("#confirm-save").click();
    q("#save").click();
    await until(() => Boolean(q("#timeline").dataset.saved));
    const saved = q("#timeline").dataset.saved;
    checks.push(await count()==="1,1,1" && q("#timeline").textContent.includes("18:30"));
    q("#retry-save").click();
    await until(() => q("#status").textContent.includes("复用"));
    checks.push(saved===q("#timeline").dataset.saved && await count()==="1,1,1");
    q("#reopen").click();
    await until(() => q("#status").textContent.includes("已读取"));
    checks.push(q("#timeline").dataset.saved===saved && !q("#edit-form").checkVisibility() && await count()==="1,1,1");
  } catch (error) {
    document.body.dataset.m04Error=error.message;
  }
  document.body.dataset.m04Browser = checks.length===11 && checks.every(Boolean) ? "PASS" : "FAIL";
  document.body.dataset.m04Checks = checks.join(",");
  document.body.dataset.m06Checks = rules.join(",");
  document.body.dataset.m06Status = rules.length===4 && rules.every(Boolean) ? "PASS" : "FAIL";
})();

