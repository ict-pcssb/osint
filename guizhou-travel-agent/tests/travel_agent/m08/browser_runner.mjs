import {spawn} from "node:child_process";
import {mkdir,readFile,writeFile} from "node:fs/promises";
import {join} from "node:path";
import {setTimeout as delay} from "node:timers/promises";

const [chromeExe,url,profile,output,rawWidth="1100",requested="pickup,taxi,hotel,dining",interactions]=process.argv.slice(2);
const width=Number(rawWidth);
const captures=new Set(requested.split(","));
await mkdir(profile,{recursive:true});
const child=spawn(chromeExe,["--headless=new","--disable-gpu","--disable-background-networking","--disable-component-update",
  "--disable-sync","--no-first-run","--no-default-browser-check","--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
  "--remote-debugging-port=0","--remote-debugging-address=127.0.0.1","--user-data-dir="+profile,"about:blank"],
  {stdio:"ignore",windowsHide:true});

let ws;
try{
  let port;
  for(let i=0;i<120&&!port;i++){
    try{port=(await readFile(join(profile,"DevToolsActivePort"),"utf8")).split(/\r?\n/)[0];}catch{}
    if(!port) await delay(65);
  }
  if(!port) throw new Error("Chrome startup timed out");
  const tabs=await (await fetch("http://127.0.0.1:"+port+"/json/list")).json();
  ws=new WebSocket(tabs.find(t=>t.type==="page").webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.addEventListener("open",resolve,{once:true});
    ws.addEventListener("error",reject,{once:true});});
  let id=0;
  const pending=new Map();
  ws.addEventListener("message",event=>{
    const message=JSON.parse(event.data), handle=pending.get(message.id);
    if(!handle) return;
    pending.delete(message.id);
    clearTimeout(handle.timer);
    if(message.error) handle.reject(new Error(message.error.message)); else handle.resolve(message.result);
  });
  function cdp(method,params={}){
    return new Promise((resolve,reject)=>{
      const key=++id;
      const timer=setTimeout(()=>{pending.delete(key);reject(new Error(method+" timed out"));},8000);
      pending.set(key,{resolve,reject,timer});
      ws.send(JSON.stringify({id:key,method,params}));
    });
  }
  await cdp("Page.enable");
  await cdp("Emulation.setDeviceMetricsOverride",{width,height:850,deviceScaleFactor:1,mobile:width<600});
  await cdp("Page.navigate",{url});
  const shots=new Set();
  let record;
  for(let i=0;i<220;i++){
    const p=await cdp("Runtime.evaluate",{expression:'({...document.body?.dataset})',returnByValue:true});
    record=p.result.value||{};
    if(interactions==="a11y" && record.testKey==="Space" && record.keyDone!=="Space"){
      await cdp("Page.bringToFront");
      await cdp("Emulation.setFocusEmulationEnabled",{enabled:true});
      await cdp("Input.dispatchKeyEvent",{type:"keyDown",key:" ",code:"Space",text:" ",unmodifiedText:" ",windowsVirtualKeyCode:32});
      await cdp("Input.dispatchKeyEvent",{type:"keyUp",key:" ",code:"Space",windowsVirtualKeyCode:32});
      await cdp("Runtime.evaluate",{expression:"document.body.dataset.keyDone='Space'; delete document.body.dataset.testKey"});
    }
    const name=record.capture;
    if(captures.has(name)&&!shots.has(name)){
      const screenshot=await cdp("Page.captureScreenshot",{format:"png",captureBeyondViewport:true});
      await writeFile(join(output,name+".png"),Buffer.from(screenshot.data,"base64"));
      shots.add(name);
      await cdp("Runtime.evaluate",{expression:"document.body.dataset.captured="+JSON.stringify(name)});
    }
    if(record.verdict) break;
    await delay(100);
  }
  const passed=record?.verdict==="PASS"&&shots.size===captures.size;
  console.log(JSON.stringify({status:passed?"PASS":"FAIL",checks:JSON.parse(record?.checks||"[]"),
    failure:record?.failure||null,screenshots:[...shots].map(name=>name+".png")}));
  if(!passed) process.exitCode=1;
}finally{
  if(ws) ws.close();
  child.kill();
}

