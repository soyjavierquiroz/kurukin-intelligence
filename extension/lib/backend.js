/* Production backend client. Loaded by the extension service worker only. */
(function(root){
  'use strict';
  const BASE_URL='https://intelligence.kuruk.in';
  const BASE_HOST='intelligence.kuruk.in';
  const safeStatus=value=>Number.isInteger(value)&&value>=100&&value<=599?value:null;
  const failure=(code,httpStatus=null,errorCode='NETWORK_ERROR')=>Object.assign(new Error(code),{code,diagnostic:{baseHost:BASE_HOST,httpStatus:safeStatus(httpStatus),errorCode}});
  const endpoint=path=>new URL(path,BASE_URL).toString();
  const validRequest=value=>!!value&&typeof value==='object'&&typeof value.tiktok_id==='string'&&/^\d{5,30}$/.test(value.tiktok_id)&&typeof value.url==='string'&&/^https:\/\/www\.tiktok\.com\/@[\w.]+\/video\/\d{5,30}$/.test(value.url)&&value.needs_audio===true;
  const requests=body=>Array.isArray(body?.enrichment_requests)?body.enrichment_requests.filter(validRequest).slice(0,10):[];
  const enrichment=body=>{const value=body?.enrichment;if(!value||typeof value!=='object')return null;const numeric=['median_views_partial','min_views','min_outlier','incremental_effective_min_views','globally_resolved_eligible','new_enrichment_budget','new_enrichment_used','new_enrichment_remaining','excluded_by_budget','eligible_incremental_by_views','eligible_total','already_resolved_global','priority_ratio','selection_views_quota','selection_outlier_quota','selection_engagement_quota','selected_by_views','selected_by_outlier','selected_by_engagement','selected_by_backfill'],optional=['median_views_final','final_candidate_count','eligible_final_by_views','eligible_final_by_outlier','eligible_final_total','priority_view_cutoff','priority_top_views_count','priority_outlier_override_count','priority_pool_count','globally_resolved_priority','missing_priority_candidates'];if(!numeric.every(key=>typeof value[key]==='number'&&Number.isFinite(value[key])&&value[key]>=0)||!optional.every(key=>value[key]===null||typeof value[key]==='number'&&Number.isFinite(value[key])&&value[key]>=0))return null;return Object.fromEntries([...numeric,...optional].map(key=>[key,value[key]]));};
  async function reserve(fetcher,videos){
    let response;
    try{response=await fetcher(endpoint('/api/v1/analyses'),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({profile:{username:videos[0]?.author,nickname:videos[0]?.nickname},videos})});}
    catch{throw failure('BACKEND_UNAVAILABLE');}
    if(response.status!==201)throw failure(response.status>=500?'BACKEND_UNAVAILABLE':'BACKEND_REJECTED',response.status,'HTTP_RESPONSE');
    let body;
    try{body=await response.json();}catch{throw failure('BACKEND_REJECTED',response.status,'INVALID_RESPONSE');}
    if(typeof body?.analysis_id!=='string')throw failure('BACKEND_REJECTED',response.status,'INVALID_RESPONSE');
    return {analysisId:body.analysis_id,requests:requests(body),enrichment:enrichment(body)};
  }
  async function checkpoint(fetcher,payload){
    let response;
    try{response=await fetcher(endpoint('/api/v1/analyses/checkpoints'),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});}
    catch{throw failure('BACKEND_UNAVAILABLE');}
    if(response.status!==200)throw failure(response.status>=500?'BACKEND_UNAVAILABLE':'BACKEND_REJECTED',response.status,'HTTP_RESPONSE');
    let body;
    try{body=await response.json();}catch{throw failure('BACKEND_REJECTED',response.status,'INVALID_RESPONSE');}
    if(typeof body?.analysis_id!=='string')throw failure('BACKEND_REJECTED',response.status,'INVALID_RESPONSE');
    return {analysisId:body.analysis_id,requests:requests(body),enrichment:enrichment(body)};
  }
  async function nextBatch(fetcher,analysisId,discoveryComplete=false,hasMore=false){
    let response;
    try{response=await fetcher(endpoint(`/api/v1/analyses/${encodeURIComponent(analysisId)}/acquisition-batches`),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({discovery_complete:discoveryComplete===true,has_more:hasMore===true})});}
    catch{throw failure('BACKEND_UNAVAILABLE');}
    if(response.status!==200)throw failure(response.status>=500?'BACKEND_UNAVAILABLE':'BACKEND_REJECTED',response.status,'HTTP_RESPONSE');
    let body;
    try{body=await response.json();}catch{throw failure('BACKEND_REJECTED',response.status,'INVALID_RESPONSE');}
    if(typeof body?.analysis_id!=='string')throw failure('BACKEND_REJECTED',response.status,'INVALID_RESPONSE');
    return {analysisId:body.analysis_id,requests:requests(body),enrichment:enrichment(body)};
  }
  async function upload(fetcher,analysisId,tiktokId,audio){
    const body=new FormData();
    body.append('audio',audio instanceof Blob&&audio.type==='audio/wav'?audio:new Blob([audio],{type:'audio/wav'}),'audio.wav');
    let response;
    try{response=await fetcher(endpoint(`/api/v1/analyses/${encodeURIComponent(analysisId)}/videos/${encodeURIComponent(tiktokId)}/audio`),{method:'POST',body});}
    catch{throw failure('BACKEND_UNAVAILABLE');}
    if(response.status!==202)throw failure(response.status>=500?'BACKEND_UNAVAILABLE':'BACKEND_REJECTED',response.status,'HTTP_RESPONSE');
    return 202;
  }
  async function reportFailure(fetcher,analysisId,tiktokId,code){
    let response;
    try{response=await fetcher(endpoint(`/api/v1/analyses/${encodeURIComponent(analysisId)}/videos/${encodeURIComponent(tiktokId)}/acquisition-failure`),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({code})});}
    catch{throw failure('BACKEND_UNAVAILABLE');}
    if(response.status!==202)throw failure(response.status>=500?'BACKEND_UNAVAILABLE':'BACKEND_REJECTED',response.status,'HTTP_RESPONSE');
    let body;
    try{body=await response.json();}catch{throw failure('BACKEND_REJECTED',response.status,'INVALID_RESPONSE');}
    if(typeof body?.released!=='boolean')throw failure('BACKEND_REJECTED',response.status,'INVALID_RESPONSE');
    return body;
  }
  const diagnostic=error=>{const value=error?.diagnostic;return {baseHost:BASE_HOST,httpStatus:safeStatus(value?.httpStatus),errorCode:['NETWORK_ERROR','HTTP_RESPONSE','INVALID_RESPONSE','RUNTIME_ERROR'].includes(value?.errorCode)?value.errorCode:'RUNTIME_ERROR'};};
  const safeError=error=>({code:['BACKEND_UNAVAILABLE','BACKEND_REJECTED'].includes(error?.code)?error.code:'BACKEND_UNAVAILABLE',diagnostic:diagnostic(error)});
  const api=Object.freeze({BASE_URL,BASE_HOST,endpoint,requests,reserve,checkpoint,nextBatch,upload,reportFailure,safeError,diagnostic});
  root.KurukinBackend=api;
  if(typeof module!=='undefined')module.exports=api;
})(globalThis);
