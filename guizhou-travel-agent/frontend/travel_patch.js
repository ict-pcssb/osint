const patchForm = document.querySelector("#patch-form");
const patchPreview = document.querySelector("#patch-preview");
const tripTimeline = document.querySelector("#traveller-timeline");
const patchStatus = document.querySelector("#patch-status");
const previewButton = patchForm.querySelector("button");
let replySchema, trip;

async function patchRequest(path, body) {
  const response = await fetch(path, {
    ...(body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    }),
    signal: AbortSignal.timeout(35000),
  });
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(value.error || "行程服务暂时无法处理");
    error.code = value.code;
    error.status = response.status;
    throw error;
  }
  return value;
}

function patchButton(id, label, onClick) {
  const button = element("button", label);
  button.id = id;
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function showTimeline(value) {
  trip = value;
  const caption = element("h2", "已保存行程 r" + trip.revision);
  const lock = element("p", trip.return_lock ? "返程已锁定 · " + [
    trip.return_lock.train_number, time(trip.return_lock.departure_at),
  ].filter(Boolean).join(" · ") : "返程待核实");
  lock.id = "locked-return";
  const next = trip.timeline.result.days.at(-1).next_stop_id;
  const nextStop = trip.timeline.result.days.at(-1).stops.find(stop => stop.stop_id === next);
  tripTimeline.replaceChildren(caption, lock, renderResult(trip.timeline, replySchema, {saved:true}),
    paragraph(nextStop ? "下一站 · " + nextStop.label : "下一站待重新核实"));
  tripTimeline.dataset.revision = trip.revision;
  previewButton.disabled = false;
}

function showPreview(packet) {
  const result = packet.result;
  const card = renderResult(packet, replySchema, {lockedLabels:trip.locked_labels});
  patchPreview.replaceChildren(card);
  if (result.kind !== "patch_preview" || card.dataset.error) {
    previewButton.disabled = false;
    return;
  }
  const path = "/api/travel/trips/" + encodeURIComponent(trip.trip_id) + "/patch/";
  const proposal = {proposal_id: result.proposal_id, base_revision: result.base_revision};
  // One operation key for this preview, including retries after an ambiguous response.
  const body = {...proposal, confirmed: true, idempotency_key: crypto.randomUUID()};
  const actions = element("div", undefined, "patch-actions");
  const discard = patchButton("patch-discard", "保留原行程", discardProposal);
  const confirm = patchButton("patch-confirm", "确认修改并保存", confirmProposal);
  const check = patchButton("patch-operation", "查询保存状态", checkOperation);
  const canConfirm = result.rule_status !== "conflict";
  if (canConfirm) actions.append(confirm);
  actions.append(discard);
  patchPreview.append(actions);

  function failure(error) {
    patchStatus.textContent = error.message;
    patchPreview.replaceChildren();
    previewButton.disabled = false;
    if (error.code === "version_conflict") {
      patchRequest("/api/trip").then(showTimeline).catch(error => { patchStatus.textContent = error.message; });
    }
  }

  async function saved() {
    showTimeline(await patchRequest("/api/trip"));
    patchPreview.replaceChildren();
    patchStatus.textContent = "已保存此次修改。";
  }

  async function discardProposal() {
    discard.disabled = confirm.disabled = true;
    try {
      await patchRequest(path + "discard", proposal);
      patchPreview.replaceChildren();
      patchStatus.textContent = "已保留原行程。";
      previewButton.disabled = false;
    } catch (error) {
      failure(error);
    }
  }

  function pending(allowRetry = false) {
    patchStatus.textContent = "保存状态待确认。请先查询本次操作。";
    actions.replaceChildren(check);
    check.disabled = false;
    if (allowRetry) {
      confirm.disabled = false;
      confirm.textContent = "重试本次确认";
      actions.append(confirm);
    }
  }

  async function confirmProposal() {
    confirm.disabled = discard.disabled = check.disabled = true;
    let receipt;
    try {
      receipt = await patchRequest(path + "commit", body);
    } catch (error) {
      if (error.status >= 400 && error.status < 500 || error.code === "commit_disabled") failure(error);
      else pending();
      return;
    }
    try {
      if (receipt.save_status === "saved") await saved();
      else pending();
    } catch {
      pending();
    }
  }

  async function checkOperation() {
    check.disabled = confirm.disabled = true;
    try {
      const receipt = await patchRequest(path + "operation-status", {idempotency_key: body.idempotency_key});
      if (receipt.save_status === "saved") await saved();
      else pending(true);
    } catch {
      pending();
    }
  }
}

patchForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (!trip || previewButton.disabled) return;
  previewButton.disabled = true;
  patchStatus.textContent = "正在准备差异预览。";
  try {
    const packet = await patchRequest("/api/travel/trips/" + encodeURIComponent(trip.trip_id) + "/patch/preview", {
      base_revision: trip.revision,
      instruction: new FormData(patchForm).get("instruction"),
    });
    patchStatus.textContent = "";
    showPreview(packet);
  } catch (error) {
    patchStatus.textContent = error.message;
    if (error.code === "version_conflict") {
      try { showTimeline(await patchRequest("/api/trip")); } catch { /* Keep the visible itinerary. */ }
    }
    previewButton.disabled = false;
  }
});

const patchReady = Promise.all([patchRequest("/schema"), patchRequest("/api/trip")]).then(([schema, value]) => {
  replySchema = schema;
  showTimeline(value);
}).catch(error => { patchStatus.textContent = error.message; });

