/** Real SQLite qualification of production catalog SQL and migration triggers. */
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
  const db=new DatabaseSync(':memory:');
  const sql={exec(query,...bindings){
    let rows=[];
    if(query.includes(';') && !bindings.length)db.exec(query);
    else rows=db.prepare(query).all(...bindings);
    return {toArray:()=>rows,one:()=>{if(rows.length!==1)throw new Error('expected one row');return rows[0];}};
  }};
  db.exec(`CREATE TABLE records(kind TEXT,key TEXT,sequence INTEGER,payload TEXT,PRIMARY KEY(kind,key,sequence));
    CREATE TABLE resources(digest TEXT,resource_id TEXT,PRIMARY KEY(digest,resource_id));`);
  const state={sql,get(kind,key,sequence=Number.MAX_SAFE_INTEGER){
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
  db.close();
  console.log(JSON.stringify({passed:7,records:count,pages}));
} finally {rmSync(directory,{recursive:true,force:true});}
