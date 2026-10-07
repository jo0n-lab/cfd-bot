// Issue #30: single render counts, retained records and sampled CPU profile.
// Requires tests.web_fixture and Playwright; no production data or mutation.
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const output=process.argv[2]||'/tmp/cfd-log-audit-30';
 fs.mkdirSync(output,{recursive:true});
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage();
  const fixture=JSON.parse(fs.readFileSync('/tmp/cfd-web-fixture.json','utf8'));
  const source=fs.readFileSync(path.join(__dirname,'../../cfd_bot/web_static/diagnostics.js'),'utf8');
  // Analysis only: retain one full render instead of overwriting the 8 MiB ring.
  await page.route('**/diagnostics.js',route=>route.fulfill({contentType:'text/javascript',body:source.replace('capacity=8*1024*1024','capacity=128*1024*1024')}));
  await page.goto(fixture.url);await page.waitForFunction(()=>document.querySelector('#sync-label').textContent.startsWith('갱신'));
  const records=await page.evaluate(()=>{
   CFDLog.configure(true);
   S.overview.tickets=Array.from({length:1000},(_,i)=>({name:'Ticket '+i,filename:'alone-'+i+'.json',task_type:'single',role:'alone',state:'idle'}));
   const before=CFDLog.snapshot().records.length;ticketList();
   return CFDLog.snapshot().records.slice(before).map(JSON.parse);
  });
  fs.writeFileSync(path.join(output,'browser-records.json'),JSON.stringify(records));
  const calls={},events={};
  for(const record of records){events[record.event]=(events[record.event]||0)+1;if(record.event==='function.call')calls[record.function]=(calls[record.function]||0)+1;}
  const session=await page.context().newCDPSession(page);await session.send('Profiler.enable');await session.send('Profiler.start');
  await page.evaluate(()=>{for(let i=0;i<5;i++)ticketList();});
  const {profile}=await session.send('Profiler.stop');
  const cpu=profile.nodes.filter(n=>n.hitCount).sort((a,b)=>b.hitCount-a.hitCount).slice(0,16).map(n=>({function:n.callFrame.functionName,file:n.callFrame.url.split('/').at(-1),line:n.callFrame.lineNumber+1,samples:n.hitCount}));
  fs.writeFileSync(path.join(output,'browser-analysis.json'),JSON.stringify({rows:1000,records:records.length,bytes:Buffer.byteLength(JSON.stringify(records)),events,top_calls:Object.entries(calls).sort((a,b)=>b[1]-a[1]).slice(0,12),profile_total_samples:profile.samples.length,profile_top:cpu},null,2));
  console.log(path.join(output,'browser-analysis.json'));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
