// No network/DOM mutation: falsey JavaScript throws must still be recorded as failures.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const {performance}=require('node:perf_hooks');
const context=vm.createContext({performance,Event:class Event{},document:{addEventListener(){}},window:{addEventListener(){}},console:{warn(){},error(){}}});
vm.runInContext(fs.readFileSync('cfd_bot/web_static/diagnostics.js','utf8')+';globalThis.logger=CFDLog;',context);
const log=context.logger;log.configure(true);
for(const [i,value] of [0,false,'',null,undefined,NaN].entries()){
 let caught=false;try{log.wrap(()=>{throw value;},'falsey-'+i)();}catch(error){caught=true;assert(Object.is(error,value));}assert(caught);
}
assert.equal(log.snapshot().records.map(JSON.parse).filter(r=>r.event==='function.raise').length,6);
vm.runInContext("globalThis.pending=CFDLog.wrap(()=>Promise.reject(undefined),'async-falsey')().then(()=>{throw Error('expected rejection');},value=>{if(value!==undefined)throw Error('identity changed');});",context);
context.pending.then(()=>{
 assert.equal(log.snapshot().records.map(JSON.parse).filter(r=>r.event==='function.raise').length,7);
 console.log('Browser falsey exceptions and Promise rejection preserved.');
}).catch(error=>{console.error(error);process.exitCode=1;});
