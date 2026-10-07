'use strict';
// Local diagnostic recorder: no collector, network request, or application state.
const CFDLog = (() => {
  let enabled=false, serial=0, stack=[], records=[], head=0, used=0, overwritten=0;
  const capacity=8*1024*1024, session=Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  const interactions=new WeakMap();
  const secret=/token|password|secret|authorization|cookie|csrf/i;
  const readable=/^(action|id|name|filename|path|method|status|view|field|function|ticket|job_id|server_trace|request_call_id|interaction_id|label)$/;
  function summary(value,key='',depth=0) {
    if(secret.test(key))return '[redacted]';
    if(value==null||typeof value==='boolean'||typeof value==='number')return value;
    if(typeof value==='string')return readable.test(key)?value.slice(0,256):{type:'string',length:value.length};
    if(depth>2)return {type:Array.isArray(value)?'array':typeof value};
    if(Array.isArray(value))return {count:value.length,items:value.slice(0,12).map(v=>summary(v,key,depth+1)),truncated:value.length>12};
    if(Object.getPrototypeOf(value)===Object.prototype){const out={};for(const k of Object.keys(value).slice(0,24))out[k]=summary(value[k],k,depth+1);return out;}
    return {type:typeof value};
  }
  function append(event,fields={}) {
    if(!enabled)return;
    const record={event,ts_ms:Date.now(),mono_ms:performance.now(),session,seq:++serial,...fields};
    const line=JSON.stringify(record);records.push(line);used+=line.length*2;
    // Retention is explicit; recording is never sampled.
    while(head<records.length&&used>capacity){used-=records[head].length*2;head++;overwritten++;}
    if(head>=4096){records=records.slice(head);head=0;}
  }
  function event(name,fields={}){if(enabled)append(name,{call_id:stack.at(-1)||null,data:summary(fields)});}
  function wrap(fn,name) {
    return function(...args){
      if(!enabled)return fn.apply(this,args);
      const id=session+':'+(++serial),parent=stack.at(-1)||null,start=performance.now();
      append('function.call',{function:name,call_id:id,parent_call_id:parent,interaction_id:args[0] instanceof Event?interactions.get(args[0])||null:null,input:summary(args)});
      const finish=(value,error)=>append(error?'function.raise':'function.return',{function:name,call_id:id,parent_call_id:parent,duration_ms:performance.now()-start,result:error?{type:error.name,message_length:String(error.message||'').length,stack:String(error.stack||'').split('\n').slice(1,12)}:summary(value)});
      stack.push(id);
      try {
        const result=fn.apply(this,args);
        if(result instanceof Promise)return result.then(value=>{finish(value);return value;},error=>{finish(null,error);throw error;});
        finish(result);return result;
      }catch(error){finish(null,error);throw error;}
      finally{stack.pop();}
    };
  }
  function configure(value){const prior=enabled;enabled=!!value;if(enabled&&!prior)append('browser.start',{schema_version:1,capacity_bytes:capacity});}
  function withCaller(call,operation){if(!enabled||!call)return operation();stack.push(call);try{return operation();}finally{stack.pop();}}
  function callAs(call,operation,...args){if(!enabled||!call)return operation(...args);stack.push(call);try{return operation(...args);}finally{stack.pop();}}
  function download(){const blob=new Blob([JSON.stringify({event:'browser.export',overwritten,retained:records.length-head,session})+'\n'+records.slice(head).join('\n')+'\n'],{type:'application/x-ndjson'});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='cfd-web-'+session+'.jsonl';a.click();URL.revokeObjectURL(url);}
  for(const kind of ['click','change','input'])document.addEventListener(kind,eventObject=>{
    if(!enabled)return;const target=eventObject.target.closest?.('[data-action],input,select,textarea,a,button');if(!target)return;
    const interaction=session+':ui:'+(++serial);interactions.set(eventObject,interaction);
    event('ui.'+kind,{interaction_id:interaction,action:target.dataset.action||target.getAttribute('href')||kind,id:target.dataset.id,name:target.dataset.name,field:target.name||target.id,value_length:typeof target.value==='string'?target.value.length:undefined,checked:target.type==='checkbox'?target.checked:undefined});
  },true);
  window.addEventListener('error',e=>event('browser.error',{name:e.error?.name,line:e.lineno,path:e.filename,message_length:e.message?.length}));
  window.addEventListener('unhandledrejection',e=>event('browser.unhandledrejection',{name:e.reason?.name,message_length:e.reason?.message?.length}));
  window.addEventListener('hashchange',e=>{if(!enabled)return;const interaction=session+':ui:'+(++serial);interactions.set(e,interaction);event('ui.navigation',{view:location.hash,interaction_id:interaction});});
  for(const level of ['warn','error']){const original=console[level];console[level]=function(...args){if(enabled)event('browser.console.'+level,{arguments:args});return original.apply(console,args);};}
  return {wrap,event,configure,download,withCaller,callAs,currentCall:()=>stack.at(-1)||null,get enabled(){return enabled;},snapshot:()=>({records:records.slice(head),overwritten,used})};
})();
