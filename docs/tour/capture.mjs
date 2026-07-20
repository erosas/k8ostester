// Drive the running k8ost-console over the Chrome DevTools Protocol and capture
// the tour: the still screenshots (Operate / Build / armed break-glass) and the
// frames of a live failover (kill the primary → CNPG promotes a replica → 3/3).
//
// Not run directly — capture.sh starts the console-facing Chrome and ffmpeg
// around it. Env: TOUR_URL, TOUR_NS, TOUR_CLUSTER, TOUR_CONTEXT, TOUR_OUT.
// No npm dependencies — Node's global WebSocket speaks CDP directly.
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

const URL = process.env.TOUR_URL || 'http://127.0.0.1:8700';
const NS = process.env.TOUR_NS || 'demo';
const CLUSTER = process.env.TOUR_CLUSTER || 'orders';
const CTX = process.env.TOUR_CONTEXT || 'kind-k8ost';
const OUT = process.env.TOUR_OUT || 'docs/tour';
const CDP = 'http://127.0.0.1:9222';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pad = (n) => String(n).padStart(3, '0');
const kubectl = (...args) => execFileSync('kubectl', ['--context', CTX, '-n', NS, ...args]).toString().trim();

// --- CDP plumbing ---------------------------------------------------------
const targets = await (await fetch(`${CDP}/json`)).json();
let page = targets.find((t) => t.type === 'page');
if (!page) page = await (await fetch(`${CDP}/json/new?about:blank`, { method: 'PUT' })).json();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res) => ws.addEventListener('open', res, { once: true }));

let id = 1;
const rpc = (method, params) =>
  new Promise((resolve) => {
    const wanted = id++;
    const onMsg = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id === wanted) { ws.removeEventListener('message', onMsg); resolve(m.result); }
    };
    ws.addEventListener('message', onMsg);
    ws.send(JSON.stringify({ id: wanted, method, params }));
  });

const evalPage = (expression) => rpc('Runtime.evaluate', { expression });

// A full-page still (grows with content) unless `clip` is given, in which case
// a fixed rectangle is captured — the GIF frames need identical dimensions.
async function frame(out, { height = 820, scale = 2, clip = null } = {}) {
  await rpc('Emulation.setDeviceMetricsOverride',
    { width: 1360, height, deviceScaleFactor: scale, mobile: false });
  const params = clip
    ? { format: 'png', clip, captureBeyondViewport: false, fromSurface: true }
    : { format: 'png', captureBeyondViewport: true, fromSurface: true };
  const { data } = await rpc('Page.captureScreenshot', params);
  fs.writeFileSync(out, Buffer.from(data, 'base64'));
}
const OPERATE_CLIP = { x: 0, y: 0, width: 1360, height: 800, scale: 1 };

await rpc('Page.enable', {});
await rpc('Runtime.enable', {});
await rpc('Page.navigate', { url: URL });
await sleep(5000); // let the SSE snapshot render

// --- still screenshots ----------------------------------------------------
fs.mkdirSync(OUT, { recursive: true });
await frame(`${OUT}/01-operate.png`, { height: 860 });

await evalPage("pick('builder')");
await sleep(1200);
await frame(`${OUT}/02-build.png`, { height: 1500 });

await evalPage("pick('operate'); document.getElementById('arm').checked = true; renderActions();");
await sleep(1200);
await frame(`${OUT}/03-breakglass.png`, { height: 860 });

// --- live-failover frames -------------------------------------------------
// Reset to Operate, capture the operate viewport every 1.5s while we kill the
// primary a couple frames in. Small scale keeps the GIF light.
await evalPage("pick('operate'); document.getElementById('arm').checked = false; renderActions();");
await sleep(1000);
const framesDir = `${OUT}/frames`;
fs.rmSync(framesDir, { recursive: true, force: true });
fs.mkdirSync(framesDir, { recursive: true });

const FRAMES = 30;
const primary = kubectl('get', 'cluster', CLUSTER, '-o', 'jsonpath={.status.currentPrimary}');
console.log(`failover: current primary is ${primary}`);
for (let i = 0; i < FRAMES; i++) {
  await frame(`${framesDir}/f${pad(i)}.png`, { height: 900, scale: 1, clip: OPERATE_CLIP });
  if (i === 2) {
    kubectl('delete', 'pod', primary, '--wait=false');
    console.log(`failover: deleted ${primary} at frame ${i}`);
  }
  await sleep(1500);
}
console.log(`done: 3 stills + ${FRAMES} frames in ${OUT}`);
ws.close();
process.exit(0);
