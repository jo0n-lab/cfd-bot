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
    await page.waitForFunction(()=>document.querySelector('#sync-label').textContent.startsWith('관측'));
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
    const trackedHistory=page.locator('section.panel').filter({hasText:'최근 실행 이력'});
    assert.equal(await trackedHistory.locator('.status.running').filter({hasText:'추적 가능'}).count(),1);
    assert.equal(await trackedHistory.locator('.status.invalid').filter({hasText:'티켓 없음'}).count(),1);
    const trackedRow=trackedHistory.locator('tr').filter({hasText:'Demo steady flow'});
    await trackedRow.locator('[data-action="job-detail"]').click();
    assert.equal(await page.locator('#modal-body [data-action="case-data"]').count(),1);
    await page.locator('#modal-body [data-action="case-data"]').click();
    await page.locator('#data-result .detail-grid').waitFor();
    assert.equal(await page.locator('#data-result').getByText('Demo steady flow').count()>0,true);
    await page.locator('[data-nav="overview"]').click();
    const missingRow=page.locator('section.panel').filter({hasText:'최근 실행 이력'}).locator('tr').filter({hasText:'Deleted ticket case'});
    assert.equal(await missingRow.locator('[data-action="case-data"]').count(),0);
    await missingRow.locator('[data-action="job-detail"]').click();
    assert.equal(await page.locator('#modal-body').getByText(/티켓 JSON/).count(),1);
    await page.locator('#modal-actions [data-action="close-modal"]').click();
    let flickerRound=0;
    await page.route('**/api/overview',async route=>{
      const response=await route.fetch();
      const state=await response.json();
      flickerRound+=1;
      state.at=(state.at||Date.now()/1000)+flickerRound;
      state.live=[{
        id:'flicker-case',name:'Flicker Case',case_dir:fixture.root+'/demo',status:'running',
        started:state.at-120,actual_cores:4,actual_cpu_list:'0-3',registered:false,
        owner:'test',time:40+flickerRound,
        estimate:flickerRound===1
          ?{target:100,progress:.4,remaining_seconds:185,basis:'recent_log_rate'}
          :{target:100,progress:null,remaining_seconds:null,basis:'unknown'}
      }];
      await route.fulfill({response,json:state});
    });
    await page.evaluate(()=>refresh());
    await page.locator('[data-run-key="flicker-case"]').waitFor();
    await page.evaluate(()=>document.querySelector('[data-run-key="flicker-case"] progress').dataset.identity='kept');
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('[data-run-key="flicker-case"] progress').getAttribute('data-identity'),'kept');
    assert.equal(await page.locator('[data-run-key="flicker-case"] progress').getAttribute('value'),'0.4');
    assert.equal(await page.locator('[data-run-key="flicker-case"] [data-run-remaining]').textContent(),'3분');
    await page.unroute('**/api/overview');
    await page.evaluate(()=>refresh());
    await page.locator('[data-nav="tickets"]').click();
    await page.locator('[data-action="select-all-tickets"]').click();
    assert.equal(await page.locator('.ticket-row input:checked').count(),await page.locator('.ticket-row input').count());
    await page.locator('[data-action="clear-ticket-selection"]').click();
    assert.equal(await page.locator('.ticket-row input:checked').count(),0);
    await page.locator('[data-action="open-ticket"]').first().click();
    await page.locator('#f-name').waitFor();
    await page.locator('#f-name').fill('Browser edited');
    await page.locator('[data-tab="resources"]').click();
    assert.equal(await page.locator('#f-queue_cpu_set').count(),0);
    assert.match(await page.locator('.editor-form').textContent(),/코어 수로 자동 결정/);
    await page.locator('#execution-source').selectOption('ticket');
    await page.locator('#f-macro_cores').fill('4');
    await page.locator('#f-macro_command').fill('./Allrun --foreground');
    assert.equal(await page.locator('#monitoring-command').count(),0);
    await page.locator('[data-field="monitoring_cpu"]').check();
    assert.equal(await page.locator('#monitoring-command').count(),1);
    assert.equal(await page.locator('#monitoring-command').isEnabled(),true);
    await page.locator('#monitoring-command').fill('./Allmonitor --interval 2');
    await page.locator('[data-action="save"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    assert.equal(await page.locator('#execution-source').inputValue(),'ticket');
    assert.equal(await page.locator('#f-macro_cores').inputValue(),'4');
    const single=JSON.parse(fs.readFileSync(fixture.root+'/tickets/alone-demo.json','utf8'));
    assert.equal(single.resource_source,'ticket');
    assert.equal(single.cores,4);
    assert.deepEqual(single.command,['./Allrun','--foreground']);
    assert.deepEqual(single.monitoring,{allocate_cpu:true,command:['./Allmonitor','--interval','2']});
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
    await page.locator('[data-tab="exports"]').click();
    await page.locator('[data-action="add-export"]').click();
    await page.locator('[data-action="browse"][data-field-target="export:0"]').click();
    await page.locator('#toast').getByText('하위 케이스를 먼저 검색하세요.').waitFor();
    assert.equal(await page.locator('#modal[open]').count(),0);
    await page.locator('[data-tab="resources"]').click();
    await page.locator('#f-macro_cores').fill('2');
    await page.locator('[data-tab="macro"]').click();
    await page.locator('#f-case_include_patterns').fill('alpha\nbeta');
    await page.locator('#f-case_exclude_patterns').fill('skip*');
    await page.locator('[data-action="discover"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('.macro-row').length===2);
    assert.equal(await page.locator('.has-post').count(),1);
    await page.locator('[data-action="case-up"]').nth(1).click();
    assert.match(await page.locator('.macro-row').first().textContent(),/beta/);
    await page.locator('[data-tab="exports"]').click();
    await page.locator('#ex-name-0').fill('shape');
    await page.locator('[data-action="browse"][data-field-target="export:0"]').click();
    await page.locator('[data-action="browse-folder"]').filter({hasText:'monitoring'}).click();
    await page.locator('[data-action="pick-file"]').filter({hasText:'shape.png'}).click();
    assert.equal(await page.locator('#ex-path-0').inputValue(),'monitoring/shape.png');
    await page.locator('[data-action="save"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    await page.locator('[data-action="queue-ticket"]').click();
    await page.locator('[data-action="confirm-modal"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-state').textContent.includes('저장된'));
    const macro = await page.evaluate(async()=>{
      const r=await fetch('/api/overview');return (await r.json()).tickets.find(t=>t.task_type==='macro');
    });
    assert.equal(macro.state,'queued');
    const macroFile=JSON.parse(fs.readFileSync(fixture.root+'/tickets/'+macro.filename,'utf8'));
    assert.deepEqual(macroFile.discovery,{include_patterns:['alpha','beta'],exclude_patterns:['skip*']});
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
    const runningRow=page.locator('tr').filter({hasText:'Browser running'});
    await runningRow.locator('[data-action="stop-job"]').click();
    await page.locator('#modal-actions [data-action="confirm-modal"]').click();
    await page.waitForFunction(()=>!document.querySelector('[data-action="stop-job"]'));
    assert.equal(await page.locator('tr').filter({hasText:'Browser running'}).locator('.status.interrupted').count(),1);
    await page.locator('[data-action="select-all-queue"]').click();
    assert.equal(await page.locator('[data-queue-select]:checked').count(),2);
    await page.locator('[data-action="clear-queue-selection"]').click();
    assert.equal(await page.locator('[data-queue-select]:checked').count(),0);
    await page.locator('[data-action="select-all-queue"]').click();
    await page.locator('[data-action="cancel-selected-jobs"]').click();
    await page.locator('[data-action="confirm-modal"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('[data-queue-select]').length===0);
    await page.setViewportSize({width:390,height:844});
    await page.locator('[data-nav="tickets"]').click();
    await page.locator('[data-action="open-ticket"]').filter({hasText:'Browser edited'}).click();
    await page.locator('#f-name').waitFor();
    await page.screenshot({path:'/tmp/cfd-web-mobile.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log('Browser checks passed: ticket and queue select all/clear, bulk queue cancel, running job stop, single execution settings, edit, file picker, child-relative macro exports, clone/delete, macro discovery/order/queue, data preview, queue pause/resume, mobile.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
