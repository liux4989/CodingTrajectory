"""Qualify the native live adapter against synthetic loopback workerd publications."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from coding_trajectory.control_plane.remote import CloudflareRpcClient
from datahub_plugin.api_models import serialize_api_response

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = "00000000-0000-0000-0000-000000000001"
OWNER = "local-qualification-owner-token-0000000001"
READER = "local-qualification-reader-token-000000001"


def main():
    client = CloudflareRpcClient(url="http://127.0.0.1:8794", access_token=OWNER)
    base = {"workspace_id": WORKSPACE}
    for _ in range(100):
        migrated = client.call(
            "ct_catalog_migrate",
            {**base, "agent_id": "00000000-0000-0000-0000-000000000003"},
        )
        if migrated["complete"]:
            break
    else:
        raise AssertionError(
            "catalog migration did not finish within qualification budget"
        )
    rows = client.call(
        "ct_publication_changes", {**base, "after_revision": 0, "limit": 200}
    )
    root = None
    for row in reversed(rows["changes"]):
        detail = client.call(
            "ct_published_catalog",
            {**base, "kind": "detail", "resource_id": row["artifact_id"], "limit": 1},
        )
        if detail["items"] and (detail["items"][0].get("canonical") or {}).get("graph"):
            root = row["artifact_id"]
            break
    assert root, "seed a new synthetic publication with canonical projections first"
    client.close()
    with tempfile.TemporaryDirectory(prefix="ct-live-datahub-") as directory:
        bundle = Path(directory) / "live.mjs"
        subprocess.run(
            [
                str(ROOT / "packages/plugins/datahub/web/node_modules/.bin/esbuild"),
                str(ROOT / "packages/plugins/datahub/web/worker/live.ts"),
                "--bundle",
                "--platform=node",
                "--format=esm",
                f"--outfile={bundle}",
            ],
            check=True,
            capture_output=True,
        )
        runner = Path(directory) / "run.mjs"
        runner.write_text("""
import { pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';
const actualFetch=globalThis.fetch;
const calls=[];
const {generateKeyPair,exportJWK,SignJWT}=await import(pathToFileURL(process.argv[6]));
const {publicKey,privateKey}=await generateKeyPair('RS256',{extractable:true});
const publicJwk={...await exportJWK(publicKey),kid:'qualification'};

globalThis.fetch=(input,init)=>{
  const url=new URL(input);
  if(url.origin==='https://example.cloudflareaccess.com')return Promise.resolve(Response.json({keys:[publicJwk]}));
  throw new Error('unexpected public network call');
};
const {dispatchLive,default:worker}=await import(pathToFileURL(process.argv[2]));
const env=JSON.parse(process.argv[3]);
env.CORE={fetch:(input,init)=>{
  const url=new URL(input);
  if(url.origin!=='https://core.internal')throw new Error('invalid internal path');
  calls.push(JSON.parse(init.body).method);
  return actualFetch('http://127.0.0.1:8794'+url.pathname,init);
}};
const root=process.argv[4];
const envelope={protocol:'ct.datahub.v1',method:'datahub.snapshot',params:{}};
const deniedRequest=new Request('https://datahub.invalid/api/datahub/query',{method:'POST',body:JSON.stringify(envelope)});
if((await worker.fetch(deniedRequest,env)).status!==403)throw new Error('signed-out access not denied');
const token=await new SignJWT({email:'synthetic@example.invalid'}).setProtectedHeader({alg:'RS256',kid:'qualification'})
  .setIssuer(env.CF_ACCESS_TEAM_DOMAIN).setAudience(env.CF_ACCESS_AUD).setExpirationTime('5m').sign(privateKey);
const accepted=await worker.fetch(new Request('https://datahub.invalid/api/datahub/query',{method:'POST',headers:{'cf-access-jwt-assertion':token},body:JSON.stringify(envelope)}),env);
if(accepted.status!==200 || !(await accepted.json()).ok)throw new Error('authenticated live read failed');
const query=(method,params={})=>dispatchLive({protocol:'ct.datahub.v1',id:null,method,params},env);
const snapshot=await query('datahub.snapshot');
const revision=snapshot.data.revision;
const results={snapshot,projects:await query('projects'),sessions:await query('sessions'),
  graph_detail:await query('session.graph',{session_id:root,snapshot_sequence:revision}),
  session_tree:await query('session.tree',{session_id:root,snapshot_sequence:revision}),
  changes:await query('datahub.changes',{after_revision:revision})};
const denied=await query('session.items',{item_ids:[root],include_content:true});
if(denied.ok)throw new Error('raw evidence unexpectedly available');
if(calls.some(name=>name.startsWith('ct_collector')))throw new Error('read triggered publication');
execFileSync('uv',['run','python',process.argv[5]],{stdio:'pipe'});
const fresh=await query('datahub.snapshot');
if(!fresh.ok || fresh.data.revision<=revision)throw new Error('live revision did not advance after publication');
const delta=await query('datahub.changes',{after_revision:revision});
if(!delta.ok || !delta.data.invalidations.length)throw new Error('live changes missing');
results.snapshot_after_publish=fresh;

process.stdout.write(JSON.stringify({results,requests:calls.length}));
""")
        result = subprocess.run(
            [
                "node",
                str(runner),
                str(bundle),
                json.dumps(
                    {
                        "CT_WORKSPACE_ID": WORKSPACE,
                        "CT_CORE_READ_TOKEN": READER,
                        "CF_ACCESS_TEAM_DOMAIN": "https://example.cloudflareaccess.com",
                        "CF_ACCESS_AUD": "qualification",
                    }
                ),
                root,
                str(ROOT / "scripts/qualify-cloudflare-control-plane.py"),
                str(ROOT / "packages/plugins/datahub/web/node_modules/jose/dist/webapi/index.js"),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        for handler, response in payload["results"].items():
            assert response["ok"], (handler, response)
            serialize_api_response(
                "snapshot" if handler == "snapshot_after_publish" else handler,
                response["data"],
            )
        print(
            json.dumps(
                {
                    "passed": len(payload["results"]) + 4,
                    "native_read_requests": payload["requests"],
                    "publication_requests": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
