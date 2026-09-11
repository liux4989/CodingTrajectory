/** Real SQLite qualification of production catalog SQL and migration triggers. */
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const directory=mkdtempSync(join(tmpdir(),'ct-catalog-'));
try {
  const bundle=join(directory,'catalog.mjs');
  execFileSync(resolve('packages/plugins/datahub/web/node_modules/.bin/esbuild'),[
    'cloudflare/control-plane/src/catalog.ts','--bundle','--platform=node','--format=esm',`--outfile=${bundle}`],{stdio:'pipe'});
  const {initializeCatalog,catalogRead,migrateCatalog}=await import(pathToFileURL(bundle));
  const v2Bundle=join(directory,'catalog-v2.mjs');
  execFileSync(resolve('packages/plugins/datahub/web/node_modules/.bin/esbuild'),[
    'cloudflare/control-plane/src/catalog-v2.ts','--bundle','--platform=node','--format=esm',`--outfile=${v2Bundle}`],{stdio:'pipe'});
  const {initializeCatalogSelections,catalogReadV2}=await import(pathToFileURL(v2Bundle));
  const db=new DatabaseSync(':memory:');
  let resourceBytes=0;
  let resourcePlan=[];
  const sql={exec(query,...bindings){
    let rows=[];
    if(query.includes(';') && !bindings.length)db.exec(query);
    else rows=db.prepare(query).all(...bindings);
    if(query.includes('coverage,payload FROM resource_projection')) resourceBytes+=Buffer.byteLength(JSON.stringify(rows));
    if(query.startsWith('SELECT c.artifact_id,')) resourcePlan=db.prepare('EXPLAIN QUERY PLAN '+query).all(...bindings).map(row=>row.detail);
    return {toArray:()=>rows,one:()=>{if(rows.length!==1)throw new Error('expected one row');return rows[0];}};
  }};
  db.exec(`CREATE TABLE records(kind TEXT,key TEXT,sequence INTEGER,payload TEXT,PRIMARY KEY(kind,key,sequence));
    CREATE TABLE resources(digest TEXT,resource_id TEXT,PRIMARY KEY(digest,resource_id));`);
  const state={sql,head(){return db.prepare('SELECT coalesce(max(sequence),0) value FROM records').get().value;},get(kind,key,sequence=Number.MAX_SAFE_INTEGER){
    const row=db.prepare('SELECT payload FROM records WHERE kind=? AND key=? AND sequence<=? ORDER BY sequence DESC LIMIT 1').get(kind,key,sequence);
    return row?JSON.parse(row.payload):undefined;
  }};
  const workspace='00000000-0000-4000-8000-000000000001';
  const project='00000000-0000-4000-8000-000000000002';
  const insert=db.prepare('INSERT INTO records VALUES(?,?,?,?)');
  insert.run('project',project,1,JSON.stringify({display_name:'Synthetic catalog'}));
  const artifact=(n)=>({artifact_id:`10000000-0000-4000-8000-${String(n).padStart(12,'0')}`,project_id:project,
    content_sha256:String(n).padStart(64,'0'),observed_at:'2026-09-10T00:00:00Z',vendors:['codex_cli'],deleted:false,
    revision:1,published_sequence:n+1,session_ids:[]});
  let row=artifact(1);
  insert.run('artifact',row.artifact_id,2,JSON.stringify(row));
  initializeCatalog(state);
  let blocked=false;
  try{catalogRead(state,'ct_published_catalog',{workspace_id:workspace});}catch{blocked=true;}
  if(!blocked)throw new Error('legacy migration should be explicit');
  if(!migrateCatalog(state).complete)throw new Error('small migration failed');
  db.exec('BEGIN');
  for(let n=1;n<=10001;n++){
    row=artifact(n);
    if(n>1)insert.run('artifact',row.artifact_id,n+1,JSON.stringify(row));
    insert.run('projections',row.content_sha256,n+1,JSON.stringify({runtime_usage:{items:[{root_session_id:row.artifact_id}]}}));
  }
  db.exec('COMMIT');
  let cursor=null;let count=0;let pages=0;const revision=10002;
  do {
    const result=catalogRead(state,'ct_published_catalog',{workspace_id:workspace,limit:200,cursor});
    if(result.published_sequence!==revision)throw new Error('read revision drift');
    count+=result.items.length;pages++;cursor=result.next_cursor;
  }while(cursor);
  if(count!==10001 || pages!==51)throw new Error('catalog stopped at old record ceiling');
  const first=catalogRead(state,'ct_published_catalog',{workspace_id:workspace,limit:1});
  let rejected=false;
  try{catalogRead(state,'ct_published_catalog',{workspace_id:workspace,project_name:'other',cursor:first.next_cursor});}catch{rejected=true;}
  if(!rejected)throw new Error('cursor crossed filter scope');
  row=artifact(10002);insert.run('artifact',row.artifact_id,10003,JSON.stringify(row));
  const pinned=catalogRead(state,'ct_published_catalog',{workspace_id:workspace,cursor:first.next_cursor,limit:1});
  if(pinned.published_sequence!==revision)throw new Error('concurrent publication invalidated pinned cursor');
  const delta=catalogRead(state,'ct_publication_changes',{workspace_id:workspace,after_revision:revision});
  if(delta.changes.length!==1)throw new Error('change feed omitted publication');

  // V2 exercises the real selection tables, generated contracts and indexed SQL.
  db.exec(`CREATE TABLE upload_authority(id INTEGER PRIMARY KEY,incarnation TEXT);
    INSERT INTO upload_authority VALUES(1,'20000000-0000-4000-8000-000000000001');`);
  initializeCatalogSelections(state);
  const detailIdentity='a'.repeat(64);
  row={...artifact(1),resource_projection_identity:detailIdentity};
  db.prepare("UPDATE records SET payload=? WHERE kind='artifact' AND key=? AND sequence=2").run(JSON.stringify(row),row.artifact_id);
  const itemId=n=>`40000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
  const member=db.prepare('INSERT INTO resources VALUES(?,?)');
  const projectionRow=db.prepare('INSERT INTO resource_projections VALUES(?,?,?,?,?)');
  member.run(row.content_sha256,itemId(1));
  const itemPayload={item_id:itemId(1),turn_id:project,type:'synthetic-metadata'};
  projectionRow.run(detailIdentity,'item',itemId(1),'complete',JSON.stringify(itemPayload));
  const principal={workspace_id:workspace,agent_id:'30000000-0000-4000-8000-000000000001',roles:['read']};
  const read=(request,who=principal)=>{
    db.exec('BEGIN');
    try { const response=catalogReadV2(state,{workspace_id:workspace,...request},who);db.exec('COMMIT');return response; }
    catch(error){db.exec('ROLLBACK');throw error;}
  };
  const fails=(request,code,who)=>assert.throws(()=>read(request,who),error=>error.code===code);
  const narrow=()=>read({kind:'resources',resource_kind:'item',resource_ids:[itemId(1)]});
  const small=narrow();
  assert.deepEqual(small.items[0].payload,itemPayload);
  const smallBytes=resourceBytes;
  db.exec('BEGIN');
  for(let n=2;n<=10001;n++) {
    member.run(row.content_sha256,itemId(n));
    projectionRow.run(detailIdentity,'item',itemId(n),'complete',JSON.stringify({item_id:itemId(n),padding:'x'.repeat(128)}));
  }
  db.exec('COMMIT');
  resourceBytes=0;
  assert.deepEqual(narrow().items,small.items);
  assert(smallBytes>0);
  assert.equal(resourceBytes,smallBytes);
  assert(resourcePlan.some(detail=>detail.includes('resources_identity')));
  assert(resourcePlan.some(detail=>detail.includes('catalog_digest')));
  assert(!resourcePlan.some(detail=>detail.startsWith('SCAN ')));
  const batch=read({kind:'resources',resource_kind:'item',resource_ids:[itemId(10001),itemId(1)],limit:1});
  assert.equal(batch.items[0].resource_id,itemId(10001));
  fails({kind:'resources',resource_kind:'tree',resource_ids:[itemId(10001),itemId(1)],cursor:batch.next_cursor},'invalid_cursor');
  fails({kind:'resources',resource_kind:'item',resource_ids:[itemId(1),itemId(10001)],cursor:batch.next_cursor},'invalid_cursor');
  const continued=read({kind:'resources',resource_kind:'item',resource_ids:[itemId(10001),itemId(1)],cursor:batch.next_cursor});
  assert.equal(continued.items[0].resource_id,itemId(1));
  assert.equal(continued.next_cursor,null);
  assert.equal(read({kind:'resources',resource_kind:'tree',resource_ids:[itemId(1)]}).items[0].coverage,'projection_unavailable');
  assert.equal(read({kind:'resources',resource_kind:'item',resource_ids:[itemId(10002)]}).items[0].coverage,'not_found');
  const treePage=db.prepare('INSERT INTO resource_projection_pages VALUES(?,?,?,?,?,?,?)');
  for(let index=0;index<2;index++) treePage.run(detailIdentity,'tree',itemId(1),index,2,'complete',
    JSON.stringify({root_session_id:itemId(1),branches:[{session_id:itemId(index+1)}]}));
  const tree=read({kind:'resources',resource_kind:'tree',resource_ids:[itemId(1),itemId(2)],limit:1});
  assert.equal(tree.items[0].page_index,0);
  const treeNext=read({kind:'resources',resource_kind:'tree',resource_ids:[itemId(1),itemId(2)],cursor:tree.next_cursor,limit:1});
  assert.equal(treeNext.items[0].page_index,1);
  assert.equal(treeNext.items[0].payload.branches[0].session_id,itemId(2));
  assert.equal(treeNext.selection.token,tree.selection.token);
  const treeLast=read({kind:'resources',resource_kind:'tree',resource_ids:[itemId(1),itemId(2)],cursor:treeNext.next_cursor});
  assert.equal(treeLast.items[0].resource_id,itemId(2));
  assert.equal(treeLast.items[0].coverage,'projection_unavailable');
  assert.equal(treeLast.next_cursor,null);
  const base=read({kind:'status'});
  assert.equal(base.selection.publication_revision,10003);
  assert.equal(base.selection.project_metadata_revision,1);
  assert.deepEqual(base.items,[]);
  assert.equal(base.coverage,'unknown');
  assert.equal(Date.parse(base.selection.expires_at)-Date.parse(base.selection.evaluated_at),1800000);
  const emptyProject='00000000-0000-4000-8000-000000000004';
  insert.run('project',emptyProject,10004,JSON.stringify({display_name:'Empty registered',modified_at:'2026-09-10T01:00:00Z'}));
  insert.run('project',project,10005,JSON.stringify({display_name:'Renamed catalog',modified_at:'2026-09-10T02:00:00Z'}));
  const latest=read({kind:'status'});
  assert.equal(latest.selection.publication_revision,10003);
  assert.equal(latest.selection.project_metadata_revision,10005);
  assert.equal(read({kind:'projects',selection:base.selection.token}).items[0].name,'Synthetic catalog');
  const registered=read({kind:'projects',population:'registered',limit:1});
  assert.equal(registered.items[0].name,'Renamed catalog');
  const registeredNext=read({kind:'projects',population:'registered',cursor:registered.next_cursor});
  assert.deepEqual(registeredNext.items.map(p=>p.project_id),[emptyProject]);
  assert.equal(registeredNext.next_cursor,null);
  assert.deepEqual(read({kind:'projects'}).items.map(p=>p.project_id),[project]);
  assert.deepEqual(read({kind:'projects',population:'registered',modified_since:'2026-09-10T01:30:00Z'}).items.map(p=>p.project_id),[project]);
  assert.deepEqual(read({kind:'projects',population:'registered',agent_vendor:'missing'}).items,[]);

  const page=read({kind:'sessions',include:['usage','runtime'],limit:1});
  assert.equal(page.items[0].projection.project,'Renamed catalog');
  assert.equal(page.items[0].artifact_id,artifact(1).artifact_id);
  // Later source publication must not affect a continuation's selected revision.
  row={...artifact(1),deleted:true,published_sequence:10006};
  insert.run('artifact',row.artifact_id,10006,JSON.stringify(row));
  assert.equal(narrow().items[0].coverage,'not_found');
  assert.deepEqual(read({kind:'resources',resource_kind:'item',resource_ids:[itemId(1)],selection:small.selection.token}).items,small.items);
  const next=read({kind:'sessions',include:['runtime','usage'],cursor:page.next_cursor,limit:1});
  assert.equal(next.selection.token,page.selection.token);
  assert.equal(next.items[0].artifact_id,artifact(2).artifact_id);
  assert.equal(next.selection.publication_revision,10003);
  assert.equal(read({kind:'sessions',include:['runtime','usage'],limit:1}).items[0].artifact_id,artifact(2).artifact_id);
  const feed=read({kind:'changes',after_revision:10002,limit:1});
  assert.equal(feed.items[0].revision,10003);
  row={...artifact(2),deleted:true,published_sequence:10007};
  insert.run('artifact',row.artifact_id,10007,JSON.stringify(row));
  const feedNext=read({kind:'changes',after_revision:10002,cursor:feed.next_cursor,limit:1});
  assert.equal(feedNext.selection.publication_revision,10006);
  assert.equal(feedNext.items[0].revision,10006);
  assert.equal(feedNext.items[0].deleted,true);
  assert.equal(feedNext.next_cursor,null);
  assert.equal(read({kind:'changes',after_revision:10006}).items[0].revision,10007);
  fails({kind:'changes',after_revision:10003,cursor:feed.next_cursor},'invalid_cursor');
  assert.equal(read({kind:'changes',after_revision:10008}).reset_required,true);
  assert.deepEqual(read({kind:'changes',after_revision:10007}).items,[]);
  // Restore the fixture's latest visibility for the time-boundary scenarios.
  row={...artifact(2),published_sequence:10008};
  insert.run('artifact',row.artifact_id,10008,JSON.stringify(row));
  fails({kind:'projects',cursor:page.next_cursor},'invalid_cursor');
  fails({kind:'sessions',include:['runtime'],cursor:page.next_cursor},'invalid_cursor');
  fails({kind:'sessions',include:['runtime','usage'],project_name:'other',cursor:page.next_cursor},'invalid_cursor');
  fails({kind:'sessions',include:['runtime','usage'],selection:base.selection.token,cursor:page.next_cursor},'invalid_cursor');
  fails({kind:'sessions',cursor:`${base.selection.authority_incarnation}.${Date.parse(base.selection.expires_at)}.${crypto.randomUUID()}`},'invalid_cursor');
  const changedExpiry=page.next_cursor.split('.');changedExpiry[1]=String(Number(changedExpiry[1])+60000);
  fails({kind:'sessions',include:['runtime','usage'],cursor:changedExpiry.join('.')},'invalid_cursor');
  fails({kind:'status',selection:base.selection.token},'selection_scope_denied',{...principal,agent_id:emptyProject});
  fails({kind:'status',workspace_id:emptyProject},'workspace_denied');
  fails({kind:'status',snapshot_sequence:1},'invalid_contract');
  // A missing variant is unavailable, not a fabricated empty/complete result.
  const unavailable=read({kind:'sessions',limit:1});
  assert.equal(unavailable.coverage,'partial');
  assert.equal(unavailable.items[0].coverage,'projection_unavailable');
  assert.equal(unavailable.items[0].projection,null);

  const originalNow=Date.now;
  try {
    // The relative-time cutoff must not drift across an activity-time boundary.
    Date.now=()=>Date.parse('2026-09-11T00:00:00Z');
    const timed=read({kind:'sessions',since_days:1,limit:1});
    assert.equal(timed.items[0].artifact_id,artifact(2).artifact_id);
    Date.now=()=>Date.parse('2026-09-11T00:01:00Z');
    const timedNext=read({kind:'sessions',since_days:1,cursor:timed.next_cursor,limit:1});
    assert.equal(timedNext.items[0].artifact_id,artifact(3).artifact_id);
    assert.deepEqual(read({kind:'sessions',since_days:1}).items,[]);
    Date.now=()=>Date.parse(page.selection.expires_at)-1;
    assert.equal(read({kind:'sessions',include:['runtime','usage'],cursor:page.next_cursor,limit:1}).selection.token,page.selection.token);
    Date.now=()=>Date.parse(page.selection.expires_at);
    fails({kind:'sessions',include:['runtime','usage'],cursor:page.next_cursor},'selection_expired');
    fails({kind:'status',selection:page.selection.token},'selection_expired');
    db.prepare('DELETE FROM catalog_cursors WHERE token=?').run(page.next_cursor);
    db.prepare('DELETE FROM catalog_selections WHERE token=?').run(page.selection.token);
    fails({kind:'sessions',include:['runtime','usage'],cursor:page.next_cursor},'selection_expired');
    fails({kind:'status',selection:page.selection.token},'selection_expired');
  } finally {Date.now=originalNow;}
  db.exec("UPDATE upload_authority SET incarnation='20000000-0000-4000-8000-000000000002'");
  fails({kind:'status',selection:base.selection.token},'selection_reset');
  fails({kind:'sessions',include:['runtime','usage'],cursor:page.next_cursor},'selection_reset');
  db.close();
  console.log(JSON.stringify({legacy_scenarios:7,v2:'selection, metadata, populations, paging, authorization, expiry, reset passed',
    independent_resources:{siblings_added:10000,single_payload_bytes:smallBytes,indexed_membership:true},records:count,pages}));
} finally {rmSync(directory,{recursive:true,force:true});}
