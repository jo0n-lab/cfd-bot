// Browser lifecycle regressions without network, Telegram or solver execution.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const events={document:{},window:{}},nodes=new Map(),timers=[];
function node(q){if(!nodes.has(q))nodes.set(q,{textContent:'',innerHTML:'draft input remains',hidden:false,disabled:false});return nodes.get(q);}
const cache=new Map();
const context=vm.createContext({console,URLSearchParams,Date,Set,AbortController,sessionStorage:{getItem:k=>cache.get(k),setItem:(k,v)=>cache.set(k,v)},
 CFDLog:{wrap:f=>f,currentCall:()=>null,callAs:(_,f,...args)=>f(...args),configure(){},enabled:false},
 document:{hidden:false,addEventListener:(n,f)=>events.document[n]=f,querySelector:node,querySelectorAll:()=>[]},
 window:{addEventListener:(n,f)=>events.window[n]=f},location:{hash:'#overview'},
 setInterval:(f,ms)=>timers.push({f,ms}),setTimeout,clearTimeout,
});
vm.runInContext(fs.readFileSync('cfd_bot/web_static/app.js','utf8').replace(/\nstart\(\);\s*$/,''),context);
(async()=>{
 await vm.runInContext(`(async()=>{
  render=()=>{};updateOverview=()=>{};ticketList=()=>'';
  api=async()=>{throw Error('temporary network failure');};await start();
 })()`,context);
 assert.equal(timers.length,1,'initial failure must not prevent retry');
 for(const n of ['focus','pageshow','online'])assert.equal(typeof events.window[n],'function');
 assert.equal(typeof events.document.visibilitychange,'function');
 await vm.runInContext(`(async()=>{
   let resolveFirst,count=0;S.csrf='test';
   api=()=>{count++;if(count===1)return new Promise(r=>resolveFirst=r);return Promise.resolve({at:1,tickets:[],queue:[],live:[],history:[]});};
   const first=refresh(),second=refresh();globalThis.samePromise=first===second;
   resolveFirst({at:1,tickets:[],queue:[],live:[],history:[]});await second;
   globalThis.requests=count;globalThis.released=S.refreshing===null;
   location.hash='#tickets';S.draft={current:'alone-test.json',values:{name:'unsaved'},dirty:true};
   S.overview.tickets=[{filename:'alone-test.json',state:'running'}];
   renderEditor=()=>{throw Error('must not rebuild editing form');};
   api=async()=>({at:2,ticket_states:{'alone-test.json':{state:'idle',run_enabled:true,queue_enabled:true,label:'run'}}});
   await refresh();globalThis.draft=S.draft;globalThis.ticket=S.overview.tickets[0];
   location.hash='#data';S.dataId='case-a';let loads=0;loadData=async()=>{loads++;};await refresh();globalThis.loads=loads;
   location.hash='#queue';let release;api=()=>new Promise(r=>release=r);const pending=refresh();
   location.hash='#tickets';api=async()=>({at:3,tickets:[],ticket_states:{}});release({at:3,queue:[],history:[]});await pending;
 })()`,context);
 assert.equal(context.samePromise,true);assert.equal(context.requests,2);assert.equal(context.released,true);
 assert.equal(context.draft.values.name,'unsaved');assert.equal(context.draft.dirty,true);
 assert.equal(context.ticket.state,'idle');assert.equal(node('#editor-container').innerHTML,'draft input remains');
 assert.equal(context.loads,1,'data refresh must reload actual result');
 await vm.runInContext("S.busy=false;api=async()=>({at:4,ticket_states:{}});",context);
 events.document.visibilitychange();await vm.runInContext('S.refreshing',context);
 await vm.runInContext(`S.ticketVersion='version-a';saveTicketCache([{filename:'alone.json',state:'running',run_enabled:true}]);S.overview=null;S.ticketVersion='';restoreTicketCache();globalThis.cached={version:S.ticketVersion,state:S.overview.tickets[0].state,enabled:S.overview.tickets[0].run_enabled};`,context);
 assert.equal(context.cached.version,'version-a');assert.equal(context.cached.state,'waiting');assert.equal(context.cached.enabled,false);
 console.log('PASS: metadata-only session cache, startup retry, resume events, shared/follow-up refresh, draft preservation, result refresh, navigation race');
})().catch(e=>{console.error(e);process.exitCode=1;});
