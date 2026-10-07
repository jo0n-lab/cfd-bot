// No persistence: render synthetic rows, compare original source / OFF / ON.
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});const results=[];
 try{
  const fixture=JSON.parse(fs.readFileSync('/tmp/cfd-web-fixture.json','utf8'));
  for(const mode of ['baseline','off','on']){
   const page=await browser.newPage();
   if(mode==='baseline')await page.route('**/app.js',route=>route.fulfill({contentType:'text/javascript',body:fs.readFileSync('/tmp/cfd-app-baseline.js','utf8')}));
   await page.goto(fixture.url);await page.waitForFunction(()=>document.querySelector('#sync-label').textContent.startsWith('갱신'));
   for(const count of [40,1000])results.push(await page.evaluate(({mode,count})=>{
    CFDLog.configure(mode==='on');
    S.overview.tickets=Array.from({length:count},(_,i)=>({name:'Ticket '+i,filename:'alone-'+i+'.json',task_type:'single',role:'alone',state:'idle'}));
    for(let n=0;n<3;n++)ticketList();
    const durations=[];
    for(let n=0;n<20;n++){const start=performance.now();ticketList();durations.push(performance.now()-start);}
    durations.sort((a,b)=>a-b);const log=CFDLog.snapshot();
    return {mode,count,p50_ms:durations[10],p95_ms:durations[18],retained:log.records.length,overwritten:log.overwritten,bytes:log.used};
   },{mode,count}));
   await page.close();
  }
  console.log(JSON.stringify(results,null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
