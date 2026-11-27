import {spawn} from "node:child_process";
import {mkdir,readFile,writeFile} from "node:fs/promises";
import {join} from "node:path";
import {setTimeout as delay} from "node:timers/promises";

const [browser, url, profile, reportFolder] = process.argv.slice(2);
await mkdir(profile,{recursive:true});
const chrome = spawn(browser,[
  "--headless=new","--disable-gpu","--disable-background-networking","--disable-component-update",
  "--disable-sync","--no-first-run","--no-default-browser-check","--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
  "--remote-debugging-port=0","--remote-debugging-address=127.0.0.1","--user-data-dir="+profile,"about:blank",
],{stdio:"ignore",windowsHide:true});
let ws;
try {
  let port;
  for(let i=0;i<100 && !port;i++){
    try {port=(await readFile(join(profile,"DevToolsActivePort"),"utf8")).split(/\r?\n/)[0];} catch {}
    if(!port) await delay(80);
  }
  if(!port) throw new Error("Chrome did not publish a debugging port");
  const targets = await (await fetch("http://127.0.0.1:"+port+"/json/list")).json();
  ws = new WebSocket(targets.find(item=>item.type==="page").webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{
    ws.addEventListener("open",resolve,{once:true});
    ws.addEventListener("error",reject,{once:true});
  });
  let id=0;
  const errors=[];
  const waiting=new Map();
  ws.addEventListener("message",event=>{
    const packet=JSON.parse(event.data);
    if(packet.method==="Runtime.exceptionThrown") errors.push(packet.params.exceptionDetails.exception?.description || packet.params.exceptionDetails.text);
    if(waiting.has(packet.id)){
      const {resolve,reject,timer}=waiting.get(packet.id);
      clearTimeout(timer);
      waiting.delete(packet.id);
      if(packet.error) reject(new Error(packet.error.message)); else resolve(packet.result);
    }
  });
  function call(method,params={}){
    return new Promise((resolve,reject)=>{
      const number=++id;
      const timer=setTimeout(()=>{waiting.delete(number);reject(new Error(method+" timed out"));},8000);
      waiting.set(number,{resolve,reject,timer});
      ws.send(JSON.stringify({id:number,method,params}));
    });
  }
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride",{width:1260,height:1320,deviceScaleFactor:1,mobile:false});
  const navigation=await call("Page.navigate",{url});
  let record;
  for(let i=0;i<120;i++){
    const packet=await call("Runtime.evaluate",{expression:
      '({status:document.body?.dataset.m04Browser,checks:document.body?.dataset.m04Checks,error:document.body?.dataset.m04Error,m06Checks:document.body?.dataset.m06Checks,m06Status:document.body?.dataset.m06Status,capture:document.body?.dataset.m06Capture})',
      returnByValue:true});
    record=packet.result?.value;
    if(["needs_verification","conflict"].includes(record?.capture)){
      const shot=await call("Page.captureScreenshot",{format:"png",captureBeyondViewport:true});
      await writeFile(join(reportFolder,record.capture+".png"),Buffer.from(shot.data,"base64"));
      await call("Runtime.evaluate",{expression:"document.body.dataset.m06Captured = "+JSON.stringify(record.capture)});
    }
    if(record?.status) break;
    await delay(100);
  }
  if(record?.status !== "PASS" || record?.m06Status !== "PASS"){
    const page=await call("Runtime.evaluate",{expression:'({url:location.href,title:document.title,text:document.querySelector("#status")?.textContent})',returnByValue:true});
    console.error(JSON.stringify({record,errors,navigation,page:page.result.value}));
    process.exitCode=1;
  } else {
    const screenshot=await call("Page.captureScreenshot",{format:"png",captureBeyondViewport:true});
    await writeFile(join(reportFolder,"timeline.png"),Buffer.from(screenshot.data,"base64"));
    const box=await call("Runtime.evaluate",{expression:'(() => {const r=document.querySelector("#map-panel").getBoundingClientRect(); return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height,scale:1};})()',returnByValue:true});
    const map=await call("Page.captureScreenshot",{format:"png",clip:box.result.value,captureBeyondViewport:true});
    await writeFile(join(reportFolder,"map.png"),Buffer.from(map.data,"base64"));
    console.log(JSON.stringify(record));
  }
} finally {
  if(ws) ws.close();
  chrome.kill();
}

