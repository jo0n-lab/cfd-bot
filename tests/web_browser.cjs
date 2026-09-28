// Optional end-to-end check. Requires Playwright and tests.web_fixture running.
// PLAYWRIGHT_MODULE=/path/to/playwright-core node tests/web_browser.cjs
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const liveUrl = process.argv[2] === '--live' ? process.argv[3] : null;
const fixture = liveUrl ? {url:liveUrl} : JSON.parse(fs.readFileSync('/tmp/cfd-web-fixture.json', 'utf8'));
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1050}});
    const errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    page.on('console', message=>{if(message.type()==='error'&&!message.text().includes('404'))errors.push(message.text());});
    await page.goto(fixture.url);
    await page.waitForFunction(()=>document.querySelector('#sync-label').textContent.startsWith('갱신'));
    if(liveUrl){
      // Read-only deployment smoke: never click save, run, queue or delete.
      const state=await page.evaluate(async()=>await(await fetch('/api/overview')).json());
      assert.equal(state.error,null);
      await page.screenshot({path:'/tmp/cfd-web-live-dashboard.png',fullPage:true});
      await page.locator('[data-nav="tickets"]').click();
      const macro=state.tickets.find(t=>t.task_type==='macro');
      if(macro){
        await page.locator(`[data-action="open-ticket"][data-name="${macro.filename}"]`).click();
        await page.locator('#f-name').waitFor();
        await page.screenshot({path:'/tmp/cfd-web-live-editor.png',fullPage:true});
      }
      const single=state.tickets.find(t=>t.task_type==='single'&&t.role==='alone');
      if(single){
        await page.locator(`[data-action="open-ticket"][data-name="${single.filename}"]`).click();
        await page.locator('#f-name').waitFor();
        await page.locator('[data-tab="resources"]').click();
        await page.locator('#execution-source').waitFor();
        await page.screenshot({path:'/tmp/cfd-web-live-single-execution.png',fullPage:true});
      }
      await page.locator('[data-nav="data"]').click();
      await page.locator('[data-action="select-data"]').first().click();
      await page.locator('#data-result .detail-grid').waitFor();
      await page.screenshot({path:'/tmp/cfd-web-live-data.png',fullPage:true});
      await page.locator('[data-action="client-downloads"]').click();
      for(const platform of ['Windows','macOS']){
        const file=await page.request.get(fixture.url+`/downloads/CFD-Control-Room-${platform}.zip`);
        assert.equal(file.status(),200);
        assert.equal((await file.body()).subarray(0,2).toString(),'PK');
      }
      await page.locator('#modal-actions [data-action="close-modal"]').click();
      assert.deepEqual(errors,[]);
      console.log(JSON.stringify({live_smoke:'passed',tickets:state.tickets.length,running:state.live.length,queue:state.queue.length}));
      return;
    }
    await page.screenshot({path:'/tmp/cfd-web-dashboard.png',fullPage:true});
    await page.locator('[data-nav="tickets"]').click();
    await page.locator('[data-action="open-ticket"]').first().click();
    await page.locator('#f-name').waitFor();
    await page.locator('#f-name').fill('Browser edited');
    await page.locator('[data-tab="resources"]').click();
    await page.locator('#execution-source').selectOption('ticket');
    await page.locator('#f-macro_cores').fill('4');
    await page.locator('#f-macro_command').fill('./Allrun --foreground');
    await page.locator('[data-action="save"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    assert.equal(await page.locator('#execution-source').inputValue(),'ticket');
    assert.equal(await page.locator('#f-macro_cores').inputValue(),'4');
    const single=JSON.parse(fs.readFileSync(fixture.root+'/tickets/alone-demo.json','utf8'));
    assert.equal(single.resource_source,'ticket');
    assert.equal(single.cores,4);
    assert.deepEqual(single.command,['./Allrun','--foreground']);
    await page.screenshot({path:'/tmp/cfd-web-single-execution.png',fullPage:true});

    await page.locator('[data-action="save"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    await page.locator('[data-tab="exports"]').click();
    await page.locator('[data-action="add-export"]').click();
    await page.locator('#ex-name-0').fill('solver');
    await page.locator('#ex-path-0').fill('log.solver');
    await page.locator('[data-action="save"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    await page.locator('[data-tab="basic"]').click();
    await page.locator('[data-action="browse"][data-field-target="logs"]').click();
    await page.locator('[data-action="pick-file"]').filter({hasText:'log.solver'}).click();
    assert.equal(await page.locator('#f-logs').inputValue(),'log.solver');
    await page.locator('[data-action="save"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    await page.screenshot({path:'/tmp/cfd-web-editor.png',fullPage:true});
    await page.locator('[data-action="duplicate-ticket"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('변경'));
    await page.locator('#f-case_dir').fill(fixture.root+'/copy-case');
    await page.locator('#f-name').fill('Copy for deletion');
    await page.locator('[data-action="save"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    const copyName=await page.locator('.ticket-row.selected input').getAttribute('data-select');
    await page.locator(`input[data-select="${copyName}"]`).check();
    await page.locator('[data-action="delete-selected"]').click();
    await page.locator('[data-action="confirm-modal"]').click();
    await page.waitForFunction(name=>!document.querySelector(`input[data-select="${name}"]`),copyName);
    await page.locator('[data-action="new-macro"]').click();
    await page.locator('#f-case_dir').fill(fixture.root+'/batch');
    await page.locator('#f-name').fill('Browser batch');
    await page.locator('[data-tab="resources"]').click();
    await page.locator('#f-macro_cores').fill('2');
    await page.locator('[data-tab="macro"]').click();
    await page.locator('[data-action="discover"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('.macro-row').length===2);
    assert.equal(await page.locator('.has-post').count(),1);
    await page.locator('[data-action="case-up"]').nth(1).click();
    assert.match(await page.locator('.macro-row').first().textContent(),/beta/);
    await page.locator('[data-action="save"]').click();
    await page.locator('[data-action="confirm-modal"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    const macro = await page.evaluate(async()=>{
      const r=await fetch('/api/overview');return (await r.json()).tickets.find(t=>t.task_type==='macro');
    });
    assert.equal(macro.state,'queued');
    await page.locator('[data-action="open-ticket"]').filter({hasText:'alpha'}).click();
    await page.locator('#f-name').waitFor();
    await page.locator('[data-tab="resources"]').click();
    assert.equal(await page.locator('#f-macro_cores').isDisabled(),true);
    assert.equal(await page.locator('#f-macro_cores').inputValue(),'2');
    assert.match(await page.locator('.editor-form').textContent(),/상속/);

    await page.locator('[data-nav="data"]').click();
    await page.locator('[data-action="select-data"]').filter({hasText:'Browser edited'}).click();
    await page.locator('.artifact img').waitFor();
    await page.waitForFunction(()=>document.querySelector('.artifact img')?.naturalWidth>0);
    assert.equal(await page.locator('.artifact img').evaluate(el=>el.complete&&el.naturalWidth>0),true);
    assert.equal(await page.locator('.artifact-info a').count(),2);
    await page.screenshot({path:'/tmp/cfd-web-data.png',fullPage:true});
    await page.locator('[data-nav="queue"]').click();
    await page.locator('[data-action="pause-queue"]').click();
    await page.locator('[data-action="resume-queue"]').waitFor();
    await page.locator('[data-action="resume-queue"]').click();
    await page.locator('[data-action="pause-queue"]').waitFor();
    await page.setViewportSize({width:390,height:844});
    await page.locator('[data-nav="tickets"]').click();
    await page.locator('[data-action="open-ticket"]').filter({hasText:'Browser edited'}).click();
    await page.locator('#f-name').waitFor();
    await page.screenshot({path:'/tmp/cfd-web-mobile.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log('Browser checks passed: single execution settings, edit, file picker, exports, clone/delete, macro discovery/order/queue, data preview, queue pause/resume, mobile.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
