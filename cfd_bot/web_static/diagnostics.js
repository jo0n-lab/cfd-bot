'use strict';
// Schema 2: bounded numeric batches; JSON conversion occurs only at export/snapshot.
const CFDLog = (() => {
  let enabled=false, serial=0, stack=[], chunks=[], head=0, used=0, overwritten=0, count=0;
  const capacity=8*1024*1024, session=Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  const utc=Date.now(),mono=performance.now(),interactions=new WeakMap(),errorDetails=new WeakMap();
  const book=typeof CFD_DIAGNOSTIC_CODES==='undefined'?{javascript_functions:[],event_codes:{1:'function.call',2:'function.return',3:'function.raise',710:'browser.start'}}:CFD_DIAGNOSTIC_CODES;
  const functions=book.javascript_functions.slice(),functionIds=new Map(functions.map((name,id)=>[name,id])),eventNames={...book.event_codes};
  const eventIds=new Map(Object.entries(eventNames).map(([id,name])=>[name,Number(id)]));
  const secret=/token|password|secret|authorization|cookie|csrf/i;
  const readable=/^(action|id|name|filename|path|method|status|view|field|function|ticket|job_id|server_trace|request_call_id|interaction_id|label)$/;
  const lengths=new Map(),sizes=new WeakMap();
  function sized(value,bytes){sizes.set(value,bytes);return value;}
  function summary(value,key='',depth=0) {
    if(key&&secret.test(key))return '[redacted]';
    if(value==null||typeof value==='boolean'||typeof value==='number')return value??null;
    if(typeof value==='string'){
      if(key&&readable.test(key))return value.slice(0,256);
      let shape=lengths.get(value.length);if(!shape){shape=sized({type:'string',length:value.length},96);if(lengths.size>=512)lengths.clear();lengths.set(value.length,shape);}return shape;
    }
    if(depth>2)return sized({type:Array.isArray(value)?'array':typeof value},80);
    if(Array.isArray(value)){
      const items=[];let bytes=160;
      for(let i=0;i<Math.min(12,value.length);i++){const item=summary(value[i],key,depth+1);items.push(item);bytes+=8+size(item);}
      return sized({count:value.length,items,truncated:value.length>12},bytes);
    }
    if(Object.getPrototypeOf(value)===Object.prototype){
      const out={};let bytes=24;for(const k of Object.keys(value).slice(0,24)){const item=summary(value[k],k,depth+1);out[k]=item;bytes+=k.length*2+8+size(item);}return sized(out,bytes);
    }
    return sized({type:typeof value},80);
  }
  function size(value){
    if(value==null)return 8;if(typeof value==='string')return 16+value.length*2;
    if(typeof value!=='object')return 8;
    const prior=sizes.get(value);if(prior!==undefined)return prior;
    let n=24;for(const [k,v] of Object.entries(value))n+=k.length*2+8+size(v);sizes.set(value,n);return n;
  }
  function active(){
    let chunk=chunks.at(-1);
    if(!chunk||chunk.rows.length>=128){chunk={rows:[],values:[],ids:new WeakMap(),scalars:new Map(),bytes:64};chunks.push(chunk);used+=64;}
    return chunk;
  }
  function ref(chunk,value){
    value=value??null;const object=typeof value==='object'&&value!==null;
    const key=object?value:typeof value+':'+String(value),map=object?chunk.ids:chunk.scalars;
    let index=map.get(key);if(index!==undefined)return index;
    index=chunk.values.length;map.set(key,index);chunk.values.push(value);
    const bytes=size(value);chunk.bytes+=bytes;used+=bytes;return index;
  }
  function push(code,call,parent,fields,now=performance.now()){
    const chunk=active();chunk.rows.push([code,now-mono,++serial,call,parent,...fields(chunk)]);
    chunk.bytes+=96;used+=96;count++;
    while(head<chunks.length-1&&used>capacity){const stale=chunks[head];chunks[head++]=null;used-=stale.bytes;count-=stale.rows.length;overwritten+=stale.rows.length;}
    if(head>=64){chunks=chunks.slice(head);head=0;}
  }
  function event(name,fields={}){
    if(!enabled)return;let code=eventIds.get(name);if(code===undefined){code=10000+eventIds.size;eventIds.set(name,code);eventNames[code]=name;}
    const data=summary(fields);push(code,stack.at(-1)??null,null,c=>[ref(c,data)]);
  }
  function wrap(fn,name){
    let functionId=functionIds.get(name);if(functionId===undefined){functionId=functions.length;functions.push(name);functionIds.set(name,functionId);}
    const inputs=new Map();
    function inputSummary(args){
      let key='';for(const arg of args){if(arg!=null&&typeof arg==='object'||typeof arg==='function')return summary(args);key+=typeof arg==='string'?'s'+arg.length+';':typeof arg+':'+String(arg)+';';}
      let value=inputs.get(key);if(value)return value;
      value=summary(args);if(inputs.size>=64)inputs.clear();inputs.set(key,value);return value;
    }
    return function(...args){
      if(!enabled)return fn.apply(this,args);
      const id=++serial,parent=stack.at(-1)??null,start=performance.now();
      const input=inputSummary(args),interaction=args[0] instanceof Event?interactions.get(args[0])||null:null;
      push(1,id,parent,c=>[functionId,ref(c,input),interaction],start);
      const finish=(value,error,failed=false)=>{
        const now=performance.now();let result;
        if(failed){
          const object=typeof error==='object'&&error!==null;
          result=object?errorDetails.get(error):null;
          if(!result){result={error_id:session+':error:'+serial,type:error?.name||typeof error,message_length:String(error?.message??error??'').length,stack:String(error?.stack||'').split('\n').slice(1,12).map(line=>line.slice(0,512))};if(object)errorDetails.set(error,result);}
        }else result=summary(value);
        push(failed?3:2,id,parent,c=>[functionId,now-start,ref(c,result)],now);
      };
      stack.push(id);
      try{const result=fn.apply(this,args);if(result instanceof Promise)return result.then(value=>{finish(value);return value;},error=>{finish(null,error,true);throw error;});finish(result);return result;}
      catch(error){finish(null,error,true);throw error;}finally{stack.pop();}
    };
  }
  function configure(value){const prior=enabled;enabled=!!value;if(enabled&&!prior)event('browser.start',{schema_version:2,capacity_bytes:capacity});}
  function withCaller(call,operation){if(!enabled||call==null)return operation();stack.push(call);try{return operation();}finally{stack.pop();}}
  function callAs(call,operation,...args){if(!enabled||call==null)return operation(...args);stack.push(call);try{return operation(...args);}finally{stack.pop();}}
  function expand(chunk){return chunk.rows.map(row=>{
    const [code,delta,seq,call,parent]=row;
    const record={event:eventNames[code],ts_ms:utc+delta,mono_ms:mono+delta,session,seq,call_id:call,parent_call_id:parent};
    if(code===1)Object.assign(record,{function:functions[row[5]],input:chunk.values[row[6]],interaction_id:row[7]});
    else if(code===2||code===3)Object.assign(record,{function:functions[row[5]],duration_ms:row[6],result:chunk.values[row[7]]});
    else record.data=chunk.values[row[5]];
    return record;
  });}
  function snapshot(){return {records:chunks.slice(head).flatMap(c=>expand(c).map(JSON.stringify)),overwritten,used};}
  function compact(){return {event:'browser.export',schema_version:2,codebook_sha256:book.sha256,session,utc_origin_ms:utc,mono_origin_ms:mono,overwritten,retained:count,event_codes:{...eventNames},function_codes:functions.slice()};}
  function exportLines(){return [JSON.stringify(compact()),...chunks.slice(head).map(c=>JSON.stringify({event:'browser.batch.v2',values:c.values,records:c.rows}))];}
  function download(){const blob=new Blob([exportLines().join('\n')+'\n'],{type:'application/x-ndjson'});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='cfd-web-'+session+'.jsonl';a.click();URL.revokeObjectURL(url);}
  for(const kind of ['click','change','input'])document.addEventListener(kind,eventObject=>{
    if(!enabled)return;const target=eventObject.target.closest?.('[data-action],input,select,textarea,a,button');if(!target)return;
    const interaction=session+':ui:'+(++serial);interactions.set(eventObject,interaction);
    event('ui.'+kind,{interaction_id:interaction,action:target.dataset.action||target.getAttribute('href')||kind,id:target.dataset.id,name:target.dataset.name,field:target.name||target.id,value_length:typeof target.value==='string'?target.value.length:undefined,checked:target.type==='checkbox'?target.checked:undefined});
  },true);
  window.addEventListener('error',e=>event('browser.error',{name:e.error?.name,line:e.lineno,path:e.filename,message_length:e.message?.length}));
  window.addEventListener('unhandledrejection',e=>event('browser.unhandledrejection',{name:e.reason?.name,message_length:e.reason?.message?.length}));
  window.addEventListener('hashchange',e=>{if(!enabled)return;const interaction=session+':ui:'+(++serial);interactions.set(e,interaction);event('ui.navigation',{view:location.hash,interaction_id:interaction});});
  for(const level of ['warn','error']){const original=console[level];console[level]=function(...args){if(enabled)event('browser.console.'+level,{arguments:args});return original.apply(console,args);};}
  return {wrap,event,configure,download,withCaller,callAs,currentCall:()=>stack.at(-1)??null,get enabled(){return enabled;},snapshot,exportLines,stats:()=>({retained:count,overwritten,used})};
})();
