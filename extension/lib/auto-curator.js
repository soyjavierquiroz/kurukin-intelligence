/* Durable, backend-agnostic queue coordinator. No TikTok data or media is stored here. */
(function(root){
  'use strict';
  const KEY='kurukin_auto_curator_v1', VERSION=1, LOCK_MS=60000, RETRY_MS=30000;
  const globals=['idle','running','paused','stopped','completed'];
  const channels=['pending','opening','scanning','scan_complete','acquiring','waiting','exhausted','completed','paused','error','needs_user','skipped'];
  const nowISO=now=>new Date(now()).toISOString();
  function normalized(value){
    if(typeof value!=='string')return null;
    let v=value.trim();
    try{const u=new URL(v);if(/^https?:$/.test(u.protocol)&&/^(www\.)?tiktok\.com$/i.test(u.hostname))v=u.pathname;}catch{}
    const match=v.match(/^\/?@?([\w.]{1,64})\/?$/);
    if(!match)return null;
    const channel=match[1].toLowerCase(); return {channel,profile_url:`https://www.tiktok.com/@${channel}`};
  }
  function normalize(input){
    const values=Array.isArray(input)?input:String(input||'').split(/[\s,;]+/); const seen=new Set(), result=[];
    for(const value of values){const item=normalized(value);if(item&&!seen.has(item.channel)){seen.add(item.channel);result.push(item);}}
    return result;
  }
  function empty(now=Date.now){return {version:VERSION,status:'idle',channels:[],active_channel_id:null,tab_id:null,lock:null,updated_at:nowISO(now)};}
  function queue(input,tabId,now=Date.now){const at=nowISO(now);return {...empty(now),status:'running',tab_id:Number.isInteger(tabId)?tabId:null,channels:normalize(input).map((item,index)=>({id:`channel-${index}-${item.channel}`,channel:item.channel,profile_url:item.profile_url,status:'pending',started_at:null,updated_at:at,last_error:null,processed_count:0,acquired_count:0,analysis_id:null,scan_id:null}))};}
  function valid(state){return !!state&&state.version===VERSION&&globals.includes(state.status)&&Array.isArray(state.channels)&&state.channels.every(c=>c&&typeof c.id==='string'&&typeof c.channel==='string'&&typeof c.profile_url==='string'&&channels.includes(c.status));}
  function active(state){return state.channels.find(c=>c.id===state.active_channel_id)||null;}
  function clone(value){return JSON.parse(JSON.stringify(value));}
  function transition(original,event,now=Date.now){
    const state=clone(valid(original)?original:empty(now)), at=nowISO(now), current=active(state);
    const update=(status,patch={})=>{if(!current)return;Object.assign(current,patch,{status,updated_at:at});};
    switch(event.type){
      case 'PAUSE': state.status='paused'; if(current&&channels.includes(current.status)&&!['completed','skipped','error','needs_user'].includes(current.status))update('paused'); break;
      case 'RESUME': if(['paused','stopped'].includes(state.status)){state.status='running';if(current&&['paused','needs_user','waiting'].includes(current.status))update('opening');} break;
      case 'STOP': state.status='stopped'; break;
      case 'SKIP': if(current){update('skipped');state.active_channel_id=null;} break;
      case 'OPENING': update('opening',{started_at:current?.started_at||at}); break;
      case 'SCANNING': update('scanning'); break;
      case 'SCAN_COMPLETE': update('scan_complete',{scan_id:event.scan_id||current?.scan_id,processed_count:Number.isSafeInteger(event.processed_count)?event.processed_count:current?.processed_count||0}); break;
      case 'ACQUIRING': update('acquiring',{analysis_id:event.analysis_id||current?.analysis_id,processed_count:Number.isSafeInteger(event.processed_count)?event.processed_count:current?.processed_count||0,acquired_count:Number.isSafeInteger(event.acquired_count)?event.acquired_count:current?.acquired_count||0}); break;
      case 'PROGRESS': update(current?.status||'acquiring',{processed_count:Number.isSafeInteger(event.processed_count)?event.processed_count:current?.processed_count||0,acquired_count:Number.isSafeInteger(event.acquired_count)?event.acquired_count:current?.acquired_count||0}); break;
      case 'WAITING': update('waiting',{last_error:event.error||null,retry_at:now()+RETRY_MS}); break;
      case 'NEEDS_USER': update('needs_user',{last_error:event.error||'TikTok requires manual intervention'}); state.status='paused'; break;
      case 'ERROR': update('error',{last_error:event.error||'AUTO_CURATOR_ERROR'}); state.status='paused'; break;
      case 'COMPLETE': if(current){update('exhausted',{processed_count:Number.isSafeInteger(event.processed_count)?event.processed_count:current.processed_count,acquired_count:Number.isSafeInteger(event.acquired_count)?event.acquired_count:current.acquired_count});update('completed');state.active_channel_id=null;} break;
    }
    state.updated_at=at; return state;
  }
  function create({storage,tabs,alarms,now=Date.now,instanceId=`auto-${Math.random().toString(36).slice(2)}`}={}){
    if(!storage?.get||!storage?.set)throw Error('AUTO_STORAGE_REQUIRED');
    const load=async()=>{const state=await storage.get(KEY);return valid(state)?state:empty(now);};
    const save=state=>storage.set(KEY,state);
    const schedule=async state=>{const c=active(state);if(state.status==='running'&&c?.status==='waiting'&&Number.isFinite(c.retry_at)&&alarms?.create)await alarms.create('kurukin-auto-curator-retry',{when:c.retry_at});};
    async function lock(state){const at=now(), held=state.lock;if(held&&held.owner!==instanceId&&held.expires_at>at)return null;state.lock={owner:instanceId,expires_at:at+LOCK_MS};return state;}
    async function persistTransition(event){let state=await lock(await load());if(!state)return null;state=transition(state,event,now);await save(state);await schedule(state);return state;}
    async function tick(){let state=await lock(await load());if(!state)return null;if(state.status!=='running'){await save(state);return state;}const c=active(state);
      if(c?.status==='waiting'&&c.retry_at>now()){await save(state);await schedule(state);return state;}
      if(c?.status==='waiting'){state=transition(state,{type:'OPENING'},now);await save(state);if(tabs?.run)await tabs.run(state.tab_id,active(state));return state;}
      if(c){await save(state);if(c.status==='opening'&&tabs?.run)await tabs.run(state.tab_id,c);return state;}
      const next=state.channels.find(item=>item.status==='pending');
      if(!next){state.status='completed';state.updated_at=nowISO(now);await save(state);return state;}
      state.active_channel_id=next.id;state=transition(state,{type:'OPENING'},now);await save(state);if(tabs?.navigate)await tabs.navigate(state.tab_id,next.profile_url);return state;
    }
    let chain=Promise.resolve();
    const exclusive=operation=>{const next=chain.then(operation,operation);chain=next.catch(()=>{});return next;};
    async function start(input,tabId){const existing=await load();if(existing.status==='running')return existing;let state=queue(input,tabId,now);state.lock={owner:instanceId,expires_at:now()+LOCK_MS};await save(state);return tick();}
    async function pause(){const state=await persistTransition({type:'PAUSE'});if(state&&tabs?.pause)await tabs.pause(state.tab_id);return state;}
    async function resume(){const state=await persistTransition({type:'RESUME'});return state?.status==='running'?tick():state;}
    async function stop(){const state=await persistTransition({type:'STOP'});if(state&&tabs?.pause)await tabs.pause(state.tab_id);return state;}
    async function skip(){const state=await persistTransition({type:'SKIP'});return state?.status==='running'?tick():state;}
    async function event(event){const state=await persistTransition(event);return state?.status==='running'&&['COMPLETE','WAITING'].includes(event.type)?tick():state;}
    async function ready(tabId){const state=await lock(await load());if(!state||state.status!=='running'||state.tab_id!==tabId){if(state)await save(state);return state;}const c=active(state);await save(state);if(c&&c.status!=='waiting'&&tabs?.run)await tabs.run(tabId,c);return state;}
    return Object.freeze({key:KEY,load,start:(input,tabId)=>exclusive(()=>start(input,tabId)),pause:()=>exclusive(pause),resume:()=>exclusive(resume),stop:()=>exclusive(stop),skip:()=>exclusive(skip),event:value=>exclusive(()=>event(value)),ready:tabId=>exclusive(()=>ready(tabId)),tick:()=>exclusive(tick)});
  }
  const api=Object.freeze({KEY,VERSION,LOCK_MS,RETRY_MS,globals,channels,normalized,normalize,empty,queue,valid,active,transition,create});
  root.KurukinAutoCurator=api;if(typeof module!=='undefined')module.exports=api;
})(globalThis);
