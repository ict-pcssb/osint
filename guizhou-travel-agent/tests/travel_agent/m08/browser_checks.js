(async () => {
  const checks=[];
  const area=document.querySelector("#result"), body=document.body;
  const check=(name,pass)=>checks.push([name,Boolean(pass)]);
  function view(kind){return area.querySelector("[data-kind='"+kind+"']");}
  const sleep=()=>new Promise(resolve=>setTimeout(resolve,35));
  async function until(f){
    for(let i=0;i<130;i++){if(f())return;await sleep();}
    throw new Error("expected state not displayed");
  }
  async function screenshot(name){
    body.dataset.capture=name;
    await until(()=>body.dataset.captured===name);
    delete body.dataset.capture;
  }
  async function query(key){
    const text=document.querySelector("#candidate-query");
    text.value=key;
    document.querySelector("#candidate-form").requestSubmit();
    await until(()=>!document.querySelector("#candidate-form button").disabled);
  }

  try {
    await query("taxi");
    const pickup=view("pickup_choices");
    check("two_pickup_buttons",pickup?.querySelectorAll("button").length===2);
    check("no_preselected_quote",(await callApi("/api/count")).taxi===1);
    check("no_vendor_context",!JSON.stringify(candidatePacket).includes("businessData"));
    await screenshot("pickup");
    const context=candidatePacket.result.tool_context_id;
    let rejected=false;
    try{await callApi("/api/choose-candidate",{context_id:context,choice_id:"forged"});}
    catch(error){rejected=error.message.includes("候选");}
    check("forged_choice_rejected",rejected && (await callApi("/api/count")).taxi===1);
    pickup.querySelectorAll("button")[1].click();
    await until(()=>view("candidates"));
    check("taxi_vehicle_quote",["80.00","110.00","东广场","整车"].every(word=>area.textContent.includes(word)));
    check("two_taxi_calls",(await callApi("/api/count")).taxi===2);
    await screenshot("taxi");
    await query("hotel");
    check("hotel_split_unknown_total",view("candidates") && ["入住总价待查","260.00","按需确认"].every(word=>area.textContent.includes(word)));
    await screenshot("hotel");
    await query("hotel_empty");
    check("hotel_empty",view("unavailable") && !view("itinerary"));
    await query("dining");
    check("dining_primary_alternative",view("candidates") && ["首选","备选","当晚营业"].every(word=>area.textContent.includes(word)));
    await screenshot("dining");
    check("no_business_writes",(await callApi("/api/count")).writes.join(",")==="0,0,0");
  }catch(error){body.dataset.failure=error.message;}
  body.dataset.checks=JSON.stringify(checks);
  body.dataset.verdict=!body.dataset.failure && checks.length===10 && checks.every(([,pass])=>pass)?"PASS":"FAIL";
})();

