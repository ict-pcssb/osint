"use strict";

const demoPrompts = {
  "我周六准备去青岩。": "ack",
  "明天青岩下雨吗？": "weather",
  "水帘洞周六开吗？怎么预约？": "verification",
  "荔波站去小七孔怎么坐？": "transfer",
  "一家三口门票和观光车多少钱？": "cost_breakdown",
  "住哪比较合适？": "candidates",
  "帮我安排贵阳两人两天，全团800元。": "itinerary",
  "第二天改成室内，返程票不变。": "patch_preview",
};
const normalizePrompt = text => text.replace(/[\s。！？?!，,]/g, "");
const demoCases = new Map(Object.entries(demoPrompts).map(([text, kind]) => [normalizePrompt(text), kind]));
const chatInput = document.querySelector("#chat-input");
const chatLog = document.querySelector("#chat-log");
const repliesByKind = new Map(m2Data.core.map(packet => [packet.result.kind, packet]));

function addMessage(text) {
  const clean = text.trim();
  if (!clean) return;
  const turn = element("div", undefined, "chat-message");
  turn.append(element("p", clean, "chat-user"));
  const packet = repliesByKind.get(demoCases.get(normalizePrompt(clean)));
  turn.append(packet ? renderResult(packet, m2Data.schema)
    : element("p", "这个问题没有预设回复，请试试页面上的示例问题。", "chat-note"));
  chatLog.append(turn);
  turn.lastElementChild.scrollIntoView({block: "nearest"});
}

document.querySelector("#chat-form").addEventListener("submit", event => {
  event.preventDefault();
  addMessage(chatInput.value);
  chatInput.value = "";
  chatInput.focus();
});
for (const text of Object.keys(demoPrompts)) {
  const button = element("button", text);
  button.type = "button";
  button.addEventListener("click", () => addMessage(text));
  document.querySelector("#chat-prompts").append(button);
}
