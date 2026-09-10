import { handle, UNSUPPORTED } from "./snapshot";

type Row = Record<string, any>;
const METHODS = ["datahub.snapshot","datahub.changes","projects","sessions","session.graph","session.tree","session.items"];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default { fetch: (request,env) => handle(request,env,dispatchLive) } satisfies ExportedHandler<Env>;

function exact(row: Row, keys: string[]) {
  if(Object.keys(row).some(key=>!keys.includes(key))) throw new Error("unknown parameter");
}
function int(value: unknown, fallback: number, min=0,max=Number.MAX_SAFE_INTEGER): number {
  if(value==null) return fallback;
  if(!Number.isSafeInteger(value) || Number(value)<min || Number(value)>max) throw new Error("invalid integer");
  return Number(value);
}
function identifier(value: unknown): string {
  if(typeof value!=="string" || !UUID.test(value)) throw new Error("invalid identifier");
  return value.toLowerCase();
}

async function rpc(env: Env, method: string, params: Row): Promise<Row> {
  const origin=new URL(env.CT_CORE_URL);
  if(origin.protocol!=="https:" || origin.username || origin.password || origin.search || origin.hash || !["","/"].includes(origin.pathname)) throw new Error("unconfigured authority");
  const workspace=identifier(env.CT_WORKSPACE_ID);
  const response=await fetch(new URL("/v1/core",origin),{method:"POST",headers:{"Content-Type":"application/json","Authorization":`Bearer ${env.CT_CORE_READ_TOKEN}`},
    body:JSON.stringify({protocol:"ct.core.v1",method,params:{...params,workspace_id:workspace}}),redirect:"manual",signal:AbortSignal.timeout(20000)});
  if(!response.ok || !response.body) {
    console.warn("datahub_authority_http", response.status);
    throw new Error("authority unavailable");
  }
  const reader=response.body.getReader();let bytes=0;let encoded="";const decoder=new TextDecoder();
  try {
    while(true) { const chunk=await reader.read();if(chunk.done)break;bytes+=chunk.value.length;
      if(bytes>1024*1024) {await reader.cancel();throw new Error("response budget");}encoded+=decoder.decode(chunk.value,{stream:true}); }
  } finally {reader.releaseLock();}
  const result=JSON.parse(encoded+decoder.decode());
  if(result.protocol!=="ct.core.v1" || result.method!==method || !result.ok || result.data?.workspace_id!==workspace) {
    console.warn("datahub_authority_response_mismatch");
    throw new Error("authority response mismatch");
  }
  return result.data;
}
function transport(env: Env,revision: number) {return {workspace_id:env.CT_WORKSPACE_ID,snapshot_sequence:revision,source:"remote",freshness:"authoritative",content_scope:"chronicle"};}
function health() {return {freshness:{last_refresh_at:null,lag_seconds:null},catching_up:false,source_status:{ready:1,ingesting:0,failed:0,incomplete:0}};}
function item(row: Row): Row {
  const runtime=row.runtime??{},usage=row.usage??{};
  return {root_session_id:row.root_session_id,lineage_root_session_id:row.lineage_root_session_id??null,graph_id:row.graph_id??null,
    vendors:row.vendors??[],session_ids:row.session_ids??[],title:null,preview:null,project:row.project??null,
    started_at:runtime.started_at??null,ended_at:runtime.ended_at??null,status:runtime.status??null,turns:runtime.turns??null,
    execution_seconds:Math.trunc(runtime.execution_seconds??0),failed_tool_calls:runtime.failed_tool_calls??null,
    processed_tokens:usage.processed_tokens??null,cost_usd:null,pricing_confidence:null};
}

export async function dispatchLive(envelope: {protocol:string;id?:unknown;method:string;params:Row},env: Env): Promise<unknown> {
  const {method,params}=envelope;
  let data: unknown;
  try {
    if(envelope.protocol!=="ct.datahub.v1") throw new Error("invalid protocol");
    if(method==="datahub.capabilities") {exact(params,[]);data={supported:METHODS,unsupported:[...UNSUPPORTED]};}
    else if(!METHODS.includes(method)) return result(envelope,null,"unsupported");
    else data=await query(method,params,env);
    return result(envelope,data);
  } catch(error) {
    const known=["unknown parameter","invalid integer","invalid identifier","invalid protocol","unconfigured authority","authority unavailable","response budget","authority response mismatch","projection unavailable"];
    const reason=error instanceof Error && known.includes(error.message)?error.message:"runtime_failure";
    console.warn("datahub_live_unavailable",reason);
    return result(envelope,null,"unavailable");
  }
}
function result(envelope: Row,data: unknown,error?:string) {
  return {protocol:"ct.datahub.v1",id:envelope.id??null,method:envelope.method,ok:!error,data,
    availability:{state:error??"complete",missing:error?[{field:"$",reason:error}]:[]},error:error?{code:error,message:"Committed shared data is unavailable for this request."}:null};
}
async function query(method:string,params:Row,env:Env):Promise<unknown> {
  if(method==="datahub.snapshot") {
    exact(params,[]);
    const response=await rpc(env,"ct_published_catalog",{limit:1});
    return {revision:response.published_sequence,generated_at:new Date().toISOString(),transport:transport(env,response.published_sequence),...health(),
      minimum_available_revision:0,bootstrap:{ready:true,scan_started_at:null,scan_finished_at:null,error:null,last_result:null,
        coverage:{mode:"shared",content_scope:"chronicle",horizon_days:36500}},horizon_days:36500};
  }
  if(method==="datahub.changes") {
    exact(params,["after_revision"]);
    if(params.after_revision==null) throw new Error("revision required");
    const after=int(params.after_revision,0);
    const response=await rpc(env,"ct_publication_changes",{after_revision:after,limit:200});
    // A bounded invalidation page refreshes affected lists. Overflow explicitly
    // requests a fresh snapshot rather than claiming every changed row was sent.
    return {from_revision:after,to_revision:response.published_sequence,reset_required:response.reset_required||!!response.next_cursor,
      upserts:[],deletions:[],invalidations:response.changes.length?["sessions","projects","session-tree","session-graph"]:[],
      transport:transport(env,response.published_sequence),...health()};
  }
  if(method==="sessions" || method==="projects") {
    exact(params,method==="sessions"?["limit","cursor","agent_vendor","project_name","since_days","snapshot_sequence"]:["limit","cursor","agent_vendor","snapshot_sequence"]);
    const response=await rpc(env,"ct_published_catalog",{...params,kind:method,limit:int(params.limit,50,1,200)});
    const rows=method==="projects"?response.items.map((row:Row)=>({name:row.name,path:null,vendors:row.vendors})):
      response.items.map((row:Row)=>{if(!row.projection)throw new Error("projection unavailable");return item(row.projection);});
    return {items:rows,page:{revision:response.published_sequence,next_cursor:response.next_cursor,has_more:!!response.next_cursor}};
  }
  if(method==="session.graph" || method==="session.tree") {
    exact(params,["session_id","snapshot_sequence"]);
    const session=identifier(params.session_id);
    const response=await rpc(env,"ct_published_catalog",{kind:"detail",resource_id:session,limit:1,snapshot_sequence:params.snapshot_sequence});
    const canonical=response.items[0]?.canonical;
    const detail=method==="session.graph"?canonical?.graph:canonical?.trees?.[session];
    if(!detail)throw new Error("projection unavailable");return detail;
  }
  exact(params,["item_ids","include_content","turn_id","snapshot_sequence"]);
  if(params.include_content || !Array.isArray(params.item_ids) || !params.item_ids.length || params.item_ids.length>200) throw new Error("metadata items required");
  const ids=params.item_ids.map(identifier);const byId=new Map<string,unknown>();
  // Each manifest can fill many requested IDs; prevent a request from causing an
  // unbounded sequence of remote graph fetches.
  let fetches=0;
  let revision=params.snapshot_sequence;
  for(const id of ids) {
    if(byId.has(id))continue;
    if(++fetches>8)throw new Error("item request spans too many graphs");
    const response=await rpc(env,"ct_published_catalog",{kind:"detail",resource_id:id,limit:1,snapshot_sequence:revision});
    revision=response.published_sequence;
    for(const row of response.items[0]?.canonical?.items??[])byId.set(row.item_id,row);
    if(!byId.has(id))throw new Error("projection unavailable");
  }
  const selected=ids.map((id:string)=>byId.get(id));
  if(params.turn_id!=null) {identifier(params.turn_id);if(selected.some((row:any)=>row.turn_id!==params.turn_id))throw new Error("turn mismatch");}
  return selected;
}
