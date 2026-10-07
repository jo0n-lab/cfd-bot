// Real browser: bounded local records, ON/OFF, credentials and server correlation.
const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage();
  const fixture=JSON.parse(fs.readFileSync('/tmp/cfd-web-fixture.json','utf8'));
  await page.goto(fixture.url);
  await page.waitForFunction(()=>document.querySelector('#sync-label').textContent.startsWith('갱신'));
  await page.locator('[data-nav="tickets"]').click();
  await page.locator('[data-action="open-ticket"]').first().click();
  await page.locator('#f-name').waitFor();
  const data=await page.evaluate(()=>{
   if(CFDLog.level!=='basic')throw Error('server level not applied');
   const begin=CFDLog.stats().retained;esc('normal');
   if(CFDLog.stats().retained!==begin)throw Error('basic helper recorded');
   CFDLog.configure(true,'detailed');esc('detailed');
   if(!CFDLog.snapshot().records.map(JSON.parse).some(r=>r.event==='function.call'&&r.function.startsWith('web.ui.esc:')))throw Error('detailed helper missing');
   CFDLog.configure(true,'basic');
   const before=CFDLog.snapshot();
   CFDLog.configure(false);toast('OFF diagnostic test');
   const off=CFDLog.snapshot();
   CFDLog.configure(true);
   const object={id:'test-object'},failure=new Error('not-recorded-free-text');
   if(CFDLog.wrap(x=>x,'identity')(object)!==object)throw Error('identity changed');
   try{CFDLog.wrap(()=>{throw failure;},'failure')();}catch(error){if(error!==failure)throw Error('error identity changed');}
   const originalStringify=JSON.stringify;
   JSON.stringify=()=>{throw Error('eager JSON encoding');};
   try{CFDLog.wrap(x=>x,'no-eager-encode')('value');}finally{JSON.stringify=originalStringify;}
   return {before:before.records.length,off:off.records.length,records:CFDLog.snapshot().records.map(JSON.parse),csrf:S.csrf,compact:CFDLog.exportLines()};
  });
  assert.equal(data.before,data.off);
  assert(data.records.some(r=>r.event==='ui.click'));
  assert(data.records.some(r=>r.event==='function.raise'&&r.function==='failure'));
  assert(data.records.some(r=>r.event==='http.response'&&r.data.server_trace));
  const output=JSON.stringify(data.records);
  assert(!output.includes(data.csrf));
  assert(!output.includes('not-recorded-free-text'));
  const packed=data.compact.join('\n')+'\n';
  assert(!packed.includes(data.csrf));
  assert.equal(JSON.parse(data.compact[0]).schema_version,2);
  assert(data.compact.some(line=>JSON.parse(line).event==='browser.batch.v2'));
  fs.writeFileSync('/tmp/cfd-browser-compact.jsonl',packed);
  fs.writeFileSync('/tmp/cfd-browser-diagnostics.jsonl',data.records.map(JSON.stringify).join('\n')+'\n');
  console.log(JSON.stringify({browser_logging:'passed',records:data.records.length,on_off:true,csrf_redacted:true,http_correlation:true}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
