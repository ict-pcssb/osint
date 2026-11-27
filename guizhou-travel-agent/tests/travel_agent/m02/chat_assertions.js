const input = document.querySelector("#chat-input");
const form = document.querySelector("#chat-form");
const log = document.querySelector("#chat-log");
function send(text) {
  input.value = text;
  form.requestSubmit();
}
send("我周六准备去青岩。");
send("明天青岩下雨吗？");
send("一家三口门票和观光车多少钱？");
send("帮我查一下上海的实时油价");
send("  ");
const checks = [
  log.querySelectorAll(".chat-message").length === 4,
  [...log.querySelectorAll("article[data-kind]")].map(card => card.dataset.kind).join(",") === "ack,weather,cost_breakdown",
  log.textContent.includes("请试试页面上的示例问题"),
  document.documentElement.scrollWidth <= window.innerWidth,
];
document.body.dataset.m02Browser = checks.every(Boolean) ? "PASS" : "FAIL";
document.body.dataset.m02Checks = checks.map(Boolean).join(",");
