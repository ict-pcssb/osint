const outcomes = [];
const check = (label, pass) => outcomes.push([label, Boolean(pass)]);
for (const [key, {probe}] of Object.entries(m7.samples)) {
  document.querySelector("#question").value = key;
  document.querySelector("#chat-form").requestSubmit();
  const card = document.querySelector("#result [data-kind]");
  const wording = card?.textContent || "";
  check(probe+"-kind", card?.dataset.kind === probe);
  check(probe+"-only", !document.querySelector("#result [data-kind=itinerary], #result [data-error]"));
  if (probe === "weather") check("forecast", ["70%", "°C", "花溪区", "采集时间"].every(word => wording.includes(word)));
  if (probe === "transfer") check("transfer", ["01:20", "冲突", "末班待查", "15 分钟"].every(word => wording.includes(word)));
  if (probe === "candidates") check("candidates", ["G604", "待核实", "尚未预订"].every(word => wording.includes(word)));
}
check("zero-network", window.m7Network === 0);
document.body.dataset.m07Status = outcomes.every(([, pass]) => pass) ? "PASS" : "FAIL";
document.body.dataset.m07Checks = outcomes.map(([key,pass]) => key + "=" + pass).join(",");

