const checks = [];
const cards = [...document.querySelectorAll("#samples article[data-kind]")];
const required = ["ack", "weather", "verification", "transfer", "cost_breakdown", "candidates", "itinerary", "patch_preview"];
checks.push(cards.length === 8 && required.every(kind => cards.some(card => card.dataset.kind === kind)));
checks.push(document.querySelector('[data-kind=itinerary] ol').children.length === 2);
checks.push(document.querySelector('[data-kind=patch_preview]').textContent.includes("返程"));
checks.push(document.querySelector('[data-kind=cost_breakdown]').textContent.includes("待查"));
checks.push(document.querySelector('[data-kind=candidates]').textContent.includes("入住总价待查"));
checks.push(document.querySelector('[data-kind=verification]').textContent.includes("暂无证据"));
checks.push(document.querySelector('[data-kind=weather]').textContent.includes("2026-11-27"));
const errors = document.querySelectorAll("#edge-cases [data-error]");
checks.push(errors.length === 2 && [...errors].every(e => e.textContent.includes("暂无法展示")));
const fixture = document.querySelector("#edge-cases [data-kind=verification]");
fixture.querySelector("summary").click();
checks.push(fixture.querySelector("details").open);
checks.push(!fixture.querySelector("img,iframe,script") && !window.m2Injected);
checks.push(!document.querySelector("#samples a[href], #edge-cases a[href]"));
checks.push(document.querySelector('[data-kind=bundle] [data-kind=weather_unavailable]') !== null);
checks.push(document.querySelector('[data-kind=bundle] [data-kind=cost_breakdown]') !== null);
checks.push(window.m2AttemptedRequests === 0);
checks.push(document.documentElement.scrollWidth <= window.innerWidth &&
    (window.innerWidth > 710 || getComputedStyle(document.querySelector("#samples")).gridTemplateColumns.split(" ").length === 1));
document.body.dataset.m02Browser = checks.every(Boolean) ? "PASS" : "FAIL";
document.body.dataset.m02Checks = checks.map(Boolean).join(",");

document.body.dataset.m02Width = String(window.innerWidth);
