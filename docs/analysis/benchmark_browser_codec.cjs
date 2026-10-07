// Same app/fixture, sequential v1/OFF/v2 comparison. No product mutations.
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true}),results=[];
 const fixture=JSON.parse(fs.readFileSync('/tmp/cfd-web-fixture.json','utf8'));
 const v1=fs.readFileSync(process.argv[2]+'/cfd_bot/web_static/diagnostics.js','utf8');
 try{
  for(const mode of ['v1','off','v2']){
   const page=await browser.newPage();
   if(mode==='v1')await page.route('**/diagnostics.js',route=>route.fulfill({contentType:'text/javascript',body:v1}));
   await page.goto(fixture.url);await page.waitForFunction(()=>document.querySelector('#sync-label').textContent.startsWith('갱신'));
   for(const count of [40,1000])results.push(await page.evaluate(({mode,count})=>{
    CFDLog.configure(mode!=='off','detailed');
    S.overview.tickets=Array.from({length:count},(_,i)=>({name:'Ticket '+i,filename:'alone-'+i+'.json',task_type:'single',role:'alone',state:'idle'}));
    for(let i=0;i<3;i++)ticketList();
    const durations=[];for(let i=0;i<20;i++){const start=performance.now();ticketList();durations.push(performance.now()-start);}
    durations.sort((a,b)=>a-b);const log=CFDLog.stats?CFDLog.stats():CFDLog.snapshot();
    return {mode,count,p50_ms:(durations[9]+durations[10])/2,p95_ms:durations[18],retained:log.retained??log.records.length,overwritten:log.overwritten,estimated_buffer_bytes:log.used};
   },{mode,count}));
   await page.close();
  }
  console.log(JSON.stringify(results,null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
