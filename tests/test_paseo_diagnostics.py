"""Integration check: run in a small Slurm allocation with Node on PATH."""
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codeserver_inner as inner
from codeserver_lib import load_config


class DiagnosticEnvironmentTest(unittest.TestCase):
    def test_worker_preload_restores_options_before_children_and_keeps_diagnostics(self):
        repo = Path(__file__).resolve().parents[1]
        node = shutil.which('node')
        if not node:
            self.skipTest('requires Node')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / 'daemon-worker.js'
            worker.write_text("""
import {spawnSync} from 'node:child_process';
process.title = 'Paseo Daemon';
const child = spawnSync(process.execPath, ['-e',
  'console.log(JSON.stringify({options:process.env.NODE_OPTIONS,marker:process.env.PASEO_DIAGNOSTIC_NODE_OPTIONS}))'],
  {encoding:'utf8'});
if (child.error || child.status !== 0 || !child.stdout.trim()) throw new Error(JSON.stringify(child));
console.log(child.stdout);
setTimeout(() => {}, 1200);
""")
            for index, original in enumerate((None, '', '--no-warnings')):
                with self.subTest(original=original):
                    env = os.environ.copy()
                    env.update(PASEO_DIAGNOSTIC_DIR=str(root / f'diagnostics-{index}'),
                               PASEO_DIAGNOSTIC_NODE_OPTIONS=json.dumps(original),
                               NODE_OPTIONS=(original or '') + ' --import ' + str(repo / 'paseo_diagnostics.mjs'))
                    result = subprocess.run([node, str(worker)], env=env, capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    expected = {} if original is None else {'options': original}
                    self.assertEqual(json.loads(result.stdout), expected)
                    records = (Path(env['PASEO_DIAGNOSTIC_DIR']) / 'operations.jsonl').read_text()
                    self.assertIn('hook_ready', records)


@unittest.skipUnless(os.environ.get('SLURM_JOB_ID'), 'requires a compute allocation')
class PaseoDiagnosticsTest(unittest.TestCase):
    def test_real_blocking_read_has_stack_kernel_wait_and_health_delay(self):
        repo = Path(__file__).resolve().parents[1]
        cfg = load_config(repo / 'codeserver.toml')
        node = shutil.which('node')
        self.assertIsNotNone(node)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for sidecar in cfg['sidecars'].values():
                sidecar['command'] = sidecar['command'].replace(
                    '/tmp/paseo-diagnostics-$UID-${SLURM_JOB_ID:?}', str(tmp / 'diagnostics'))
            fifo = tmp / 'blocked-read'
            os.mkfifo(fifo)
            writer_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = os.environ | {'TEST_FIFO': str(fifo), 'TEST_PORT': str(port),
                                'PASEO_DIAGNOSTIC_HEALTH_URL': f'http://127.0.0.1:{port}/api/health',
                                'NODE_OPTIONS': '--import ' + str(repo / 'paseo_diagnostics.mjs')}
            # Exercise the configured launch wrapper and existing sidecar lifecycle.
            spec = cfg['sidecars']['paseo']
            original = 'exec /usr/bin/python3 /gpfs/radev/home/ez275/Apps/codeserver/paseo_runtime.py run --home "$HOME/.paseo-slurm"'
            spec['command'] = spec['command'].replace(original,
                'exec ' + shlex.quote(node) + ' ' + shlex.quote(str(repo / 'tests/paseo_stall_fixture.mjs')))
            spec.pop('ready_command')
            processes = inner.start_sidecars(cfg, 'cpu', tmp, env)
            try:
                deadline = time.monotonic() + 20
                while not Path(str(fifo) + '.started').exists():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.05)
                time.sleep(1.8)
                os.write(writer_fd, b'PRIVATE_TEST_CONTENT_NOT_FOR_LOGS')
                os.close(writer_fd)
                time.sleep(3)
            finally:
                inner.stop_sidecars(processes, 8)
            self.assertTrue(all(p.process.poll() is not None for p in processes))
            text = (tmp / 'paseo-diagnostics/events.jsonl').read_text()
            self.assertNotIn('PRIVATE_TEST_CONTENT_NOT_FOR_LOGS', text)
            rows = [json.loads(line) for line in text.splitlines()]
            calls = [r for r in rows if r['kind'] == 'slow_sync' and r['operation'] == 'fs.readFileSync']
            self.assertTrue(any(r['ms'] >= 1500 and 'paseo_stall_fixture.mjs' in r['stack'] for r in calls))
            self.assertTrue(any(r['kind'] == 'health' and r['ms'] >= 500 for r in rows))
            self.assertTrue(any(r['kind'] == 'process' and r.get('fdPath') == str(fifo) for r in rows),
                            [r for r in rows if r['kind'] == 'process'])
            self.assertTrue(any(r['kind'] == 'hook_ready' for r in rows))

    def test_real_daemon_inherits_hook_and_exports_samples(self):
        repo = Path(__file__).resolve().parents[1]
        cfg = load_config(repo / 'codeserver.toml')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for sidecar in cfg['sidecars'].values():
                sidecar['command'] = sidecar['command'].replace(
                    '/tmp/paseo-diagnostics-$UID-${SLURM_JOB_ID:?}', str(tmp / 'diagnostics'))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}/api/health'
            env = os.environ | {'PASEO_LISTEN': f'127.0.0.1:{port}',
                                'PASEO_RELAY_ENABLED': 'false',
                                'PASEO_DIAGNOSTIC_HEALTH_URL': url}
            spec = cfg['sidecars']['paseo']
            spec['command'] = spec['command'].replace('"$HOME/.paseo-slurm"', shlex.quote(str(tmp / 'home')))
            # This checks instrumentation, not installation freshness. Use the
            # validated candidate so a disposable home never republishes current.
            spec['command'] = spec['command'].replace('paseo_runtime.py run ',
                                                       'paseo_runtime.py run --candidate ')
            (tmp / 'home').mkdir()
            (tmp / 'home/config.json').write_text('{}')
            spec['ready_command'] = spec['ready_command'].replace('"$HOME/.paseo-slurm"', shlex.quote(str(tmp / 'home')))
            spec['ready_timeout'] = '60s'
            processes = inner.start_sidecars(cfg, 'cpu', tmp, env)
            try:
                time.sleep(3)
            finally:
                inner.stop_sidecars(processes, 20)
            rows = [json.loads(line) for line in (tmp / 'paseo-diagnostics/events.jsonl').read_text().splitlines()]
            self.assertTrue(any(r['kind'] == 'hook_ready' for r in rows))
            self.assertTrue(any(r['kind'] == 'process' for r in rows))
            self.assertTrue(any(r['kind'] == 'health' and r.get('status') == 200 for r in rows))
            for r in rows:
                if r['kind'] == 'daemon_attached':
                    self.assertTrue(r['exe'].startswith('/tmp/paseo-runtime-'), r['exe'])
                    self.assertFalse(Path(f'/proc/{r["pid"]}').exists())


if __name__ == '__main__':
    unittest.main()
