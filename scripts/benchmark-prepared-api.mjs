// Invoked by qualify-prepared-api.py --benchmark-output; local Linux workerd only.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../cloudflare/control-plane/package.json', import.meta.url));
const sha = text => createHash('sha256').update(text).digest('hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function inspector(mf) {
  const url = await mf.getInspectorURL();
  url.protocol = 'http:';
  const targets = await (await fetch(new URL('/json/list', url))).json();
  const target = targets.find(t => t.title.includes('direct-api'));
  assert.ok(target, 'application inspector target missing');
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let id = 0;
  const pending = new Map();
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    const entry = pending.get(message.id);
    if (!entry) return;
    clearTimeout(entry.timer); pending.delete(message.id);
    if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
    else entry.resolve(message.result);
  };
  return {
    call(method, params = {}) {
      return new Promise((resolve, reject) => {
        const key = ++id;
        const timer = setTimeout(() => { pending.delete(key); reject(new Error(`${method} timed out`)); }, 15000);
        pending.set(key, { resolve, reject, timer });
        ws.send(JSON.stringify({ id: key, method, params }));
      });
    },
    close: () => ws.close(),
  };
}

export async function benchmark({ mf, fixture, bundle, output, request }) {
  const pid = execFileSync('ps', ['-eo', 'pid=,ppid=,comm='], { encoding: 'utf8' })
    .trim().split('\n').map(line => line.trim().split(/\s+/))
    .find(([, parent, name]) => Number(parent) === process.pid && name === 'workerd')?.[0];
  assert.ok(pid, 'workerd child missing');
  const ticks = Number(execFileSync('getconf', ['CLK_TCK'], { encoding: 'utf8' }));
  function usage() {
    const stat = readFileSync(`/proc/${pid}/stat`, 'utf8').split(') ')[1].split(' ');
    const status = readFileSync(`/proc/${pid}/status`, 'utf8');
    return { cpu_ms: (Number(stat[11]) + Number(stat[12])) * 1000 / ticks,
      rss_bytes: Number(status.match(/^VmRSS:\s+(\d+)/m)[1]) * 1024,
      lifetime_peak_rss_bytes: Number(status.match(/^VmHWM:\s+(\d+)/m)[1]) * 1024 };
  }
  const report = {
    recorded_at: new Date().toISOString(), head: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
    bundle_sha256: sha(bundle), fixture_sha256: sha(JSON.stringify(fixture)), shape: fixture.benchmark.shape,
    harness_sha256: Object.fromEntries(['qualify-prepared-api.py', 'qualify-prepared-api.mjs', 'benchmark-prepared-api.mjs']
      .map(name => [name, sha(readFileSync(new URL(name, import.meta.url)))])),
    environment: { node: process.version, workerd: require('workerd/package.json').version,
      miniflare: require('miniflare/package.json').version, platform: process.platform, cpu_tick_ms: 1000 / ticks },
    limitations: ['Synthetic new sessions; not historical fixture parity.',
      'First read is after publication/preflight in a fresh process, not a cold isolate or cold storage guarantee.',
      'Wall time includes loopback, harness JSON parsing and scheduling; process CPU includes all local runtime services, not billed Worker CPU.',
      'RSS is whole workerd process; 10ms sampling may miss peaks; HWM includes publication. Heap samples are not exact request peaks.',
      'Read/byte counts are offline-reader expectations, not measured R2 platform spans.'],
    expected_reads: fixture.benchmark.expected_reads, expected_fetched_bytes: fixture.benchmark.expected_fetched_bytes,
    phases: [],
  };
  const expected = fixture.benchmark.expected_data;
  let verified = 0;
  async function sample() {
    const start = performance.now();
    const result = await request();
    const wall = performance.now() - start;
    assert.deepEqual(result.data.turns, expected.turns);
    assert.deepEqual(result.data.sessions, expected.sessions);
    const { next_cursor: ignored, ...page } = result.data.page;
    const { next_cursor: ignoredExpected, ...expectedPage } = expected.page;
    assert.deepEqual(page, expectedPage);
    assert.equal(Boolean(ignored), Boolean(ignoredExpected));
    report.response_bytes = Buffer.byteLength(JSON.stringify(result));
    assert.ok(report.response_bytes <= 448 * 1024);
    verified++;
    return wall;
  }
  async function phase(name, batches, concurrency) {
    const before = usage(); let peak = before.rss_bytes;
    const timer = setInterval(() => { peak = Math.max(peak, usage().rss_bytes); }, 10);
    const samples = []; const start = performance.now();
    try {
      for (let n = 0; n < batches; n++) samples.push(...await Promise.all(Array.from({ length: concurrency }, sample)));
    } finally { clearInterval(timer); }
    const after = usage(); const sorted = [...samples].sort((a,b) => a-b);
    const percentile = p => sorted[Math.ceil(p * sorted.length) - 1];
    const row = { name, requests: samples.length, concurrency, wall_total_ms: performance.now() - start,
      wall_p50_ms: percentile(.5), wall_p95_ms: percentile(.95), wall_p99_ms: percentile(.99),
      process_cpu_ms: after.cpu_ms - before.cpu_ms,
      process_cpu_ms_per_request: (after.cpu_ms - before.cpu_ms) / samples.length,
      rss_before_bytes: before.rss_bytes, rss_after_bytes: after.rss_bytes,
      rss_sample_peak_bytes: Math.max(peak, after.rss_bytes), lifetime_peak_rss_bytes: after.lifetime_peak_rss_bytes,
      samples_ms: samples };
    report.phases.push(row);
    console.log(`${report.shape} ${name}: p50/p95/p99=${row.wall_p50_ms.toFixed(2)}/${row.wall_p95_ms.toFixed(2)}/${row.wall_p99_ms.toFixed(2)}ms processCPU/request=${row.process_cpu_ms_per_request.toFixed(2)}ms RSS=${(row.rss_sample_peak_bytes/1048576).toFixed(1)}MiB`);
  }
  await phase('first-read-after-publication', 1, 1);
  await phase('warm-sequential', 100, 1);
  await phase('warm-concurrency-8', 25, 8);
  await phase('warm-sequential-repeat', 100, 1);
  await sleep(1000);
  report.post_idle = usage();
  const cdp = await inspector(mf);
  try {
    try { report.heap_before_profile = await cdp.call('Runtime.getHeapUsage'); }
    catch (error) { report.heap_unavailable = String(error); }
    await cdp.call('Profiler.enable');
    await cdp.call('Profiler.setSamplingInterval', { interval: 1000 });
    await cdp.call('Profiler.start');
    await phase('profiled-sequential', 100, 1);
    const { profile } = await cdp.call('Profiler.stop');
    writeFileSync(`${output}.cpuprofile`, JSON.stringify(profile));
    const nodes = new Map(profile.nodes.map(node => [node.id, node]));
    const totals = new Map();
    for (let n = 0; n < (profile.samples ?? []).length; n++) {
      const frame = nodes.get(profile.samples[n]).callFrame;
      const key = `${frame.functionName || '(anonymous)'} ${frame.url}:${frame.lineNumber + 1}`;
      totals.set(key, (totals.get(key) ?? 0) + profile.timeDeltas[n]);
    }
    report.profile = { interval_us: 1000, elapsed_ms: (profile.endTime-profile.startTime)/1000,
      top_frames: [...totals].sort((a,b) => b[1]-a[1]).slice(0,20).map(([frame, us]) => ({ frame, sampled_ms: us/1000 })) };
    try { report.heap_after_profile = await cdp.call('Runtime.getHeapUsage'); }
    catch { /* Already recorded capability failure above. */ }
    try {
      await cdp.call('HeapProfiler.collectGarbage');
      report.heap_after_forced_gc = await cdp.call('Runtime.getHeapUsage');
      report.process_after_forced_gc = usage();
    } catch (error) { report.forced_gc_unavailable = String(error); }
  } finally { cdp.close(); }
  report.verified_responses = verified;
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n');
  console.log(`PASS ${verified} response projections match the offline reader; report: ${output}`);
}
