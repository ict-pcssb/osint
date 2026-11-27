const statusLine = document.querySelector("#status");
const results = document.querySelector("#result");
const traceList = document.querySelector("#trace");
const input = document.querySelector("#message");
const send = document.querySelector("#send");
const prompts = document.querySelector("#chat-prompts");
const history = [];
let setup, busy = false;

async function request(path, body) {
  const response = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)} : {});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error);
  return value;
}

async function run(path, body) {
  if (busy) return;
  busy = true;
  statusLine.textContent = "正在实时查询，请稍候…";
  results.querySelectorAll("button").forEach(button => button.disabled = true);
  try {
    const value = await request(path, body);
    history.push(...value.trace);
    traceList.replaceChildren(...history.slice(-40).map(row => element("li",
      [row.event, row.intent, row.skill, row.tool, row.provider, row.status, row.reason].filter(Boolean).join(" · "))));
    const turn = element("section", undefined, "chat-note");
    const label = body.mode === "model" ? body.message : setup.examples[body.mode];
    turn.append(element("p", label || "已选择候车区", "chat-user"));
    turn.append(renderResult(value.packet, setup.schema, {
      pick: (context_id, choice_id) => run("/pick", {context_id, choice_id}),
    }));
    results.prepend(turn);
    statusLine.textContent = "查询完成";
  } catch (error) {
    statusLine.textContent = error.message;
    results.querySelectorAll("button").forEach(button => button.disabled = false);
  } finally {
    busy = false;
  }
}
document.querySelector("#chat-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!setup?.model || busy || !input.value.trim()) return;
  const message = input.value.trim();
  input.value = "";
  run("/ask", {mode:"model", message});
});
request("/setup").then(value => {
  setup = value;
  for (const [mode, label] of Object.entries({taxi:"打车估价", hotel:"住宿候选", dining:"餐饮候选"})) {
    const button = element("button", label);
    button.type = "button";
    button.disabled = !value.available[mode];
    button.addEventListener("click", () => {
      if (busy) return;
      input.value = value.examples[mode];
      run("/ask", {mode});
    });
    prompts.append(button);
  }
  input.readOnly = !value.model;
  send.disabled = !value.model;
  document.querySelector("#availability").textContent = value.model ? "自由聊天使用已配置的主模型。" :
    "当前进程缺少主模型凭据。三个预设仍可测试真实查询。";
}).catch(error => { statusLine.textContent = error.message; });

