// The CLI and supervisor pass the preload to the daemon worker. Restore the
// original Node options before that worker can launch providers or terminals.
import fs from 'node:fs';
import cp from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
import {performance} from 'node:perf_hooks';
import {basename, join} from 'node:path';

if (basename(process.argv[1] ?? '') === 'daemon-worker.js' &&
    process.env.PASEO_DIAGNOSTIC_NODE_OPTIONS !== undefined) {
  const originalOptions = JSON.parse(process.env.PASEO_DIAGNOSTIC_NODE_OPTIONS);
  if (originalOptions === null) delete process.env.NODE_OPTIONS;
  else process.env.NODE_OPTIONS = originalOptions;
  delete process.env.PASEO_DIAGNOSTIC_NODE_OPTIONS;
}

const directory = process.env.PASEO_DIAGNOSTIC_DIR;
const original = {...fs};
let active = false;
let writing = false;
let size = 0;
const logPath = directory && join(directory, 'operations.jsonl');
function record(data) {
  if (!active || writing) return;
  writing = true;
  try {
    if (size > 2 * 1024 * 1024) {
      if (original.existsSync(logPath + '.1')) original.renameSync(logPath + '.1', logPath + '.2');
      original.renameSync(logPath, logPath + '.1');
      size = 0;
    }
    const line = JSON.stringify({time: Date.now() / 1000, pid: process.pid, ...data}) + '\n';
    original.appendFileSync(logPath, line, {mode: 0o600});
    size += Buffer.byteLength(line);
  } catch { /* Diagnostics must never change the operation's result. */ }
  finally { writing = false; }
}
function wrap(fn, operation, pathArgument) {
  const wrapped = function (...args) {
    if (!active || writing) return Reflect.apply(fn, this, args);
    const started = performance.now();
    try { return Reflect.apply(fn, this, args); }
    finally {
      const ms = performance.now() - started;
      if (ms >= 100) record({kind: 'slow_sync', operation, ms,
        path: pathArgument && ['string', 'number'].includes(typeof args[0]) ? args[0] : undefined,
        stack: new Error().stack.split('\n').slice(2).join('\n')});
    }
  };
  // Preserve Node's promisify symbols and native-method properties.
  return Object.defineProperties(wrapped, Object.getOwnPropertyDescriptors(fn));
}
if (directory) {
  for (const key of Object.keys(fs)) {
    if (typeof fs[key] !== 'function' || !(key.endsWith('Sync') || key === 'watch')) continue;
    const fn = fs[key];
    fs[key] = Object.assign(wrap(fn, 'fs.' + key, true), fn);
    if (fn.native) fs[key].native = wrap(fn.native, 'fs.' + key + '.native', true);
  }
  for (const key of ['spawn', 'spawnSync', 'execFile', 'execFileSync', 'execSync']) {
    cp[key] = wrap(cp[key], 'child_process.' + key, false);
  }
  syncBuiltinESMExports();
  let previous = performance.now();
  const timer = setInterval(() => {
    if (!active && process.title === 'Paseo Daemon') {
      try {
        original.mkdirSync(directory, {recursive: true, mode: 0o700});
        const info = original.statSync(directory);
        if (info.uid !== process.getuid() || (info.mode & 0o077)) return;
        const stat = original.readFileSync('/proc/self/stat', 'utf8').split(') ').at(-1).split(' ');
        original.writeFileSync(join(directory, 'daemon.json'), JSON.stringify({pid: process.pid, startTicks: stat[19]}), {mode: 0o600});
        size = original.existsSync(logPath) ? original.statSync(logPath).size : 0;
        active = true;
        record({kind: 'hook_ready', node: process.version, thresholdMs: 100});
      } catch { return; }
    }
    const now = performance.now();
    if (active && now - previous > 1200) record({kind: 'event_loop_gap', ms: now - previous - 1000});
    previous = now;
  }, 1000);
  timer.unref();
}
