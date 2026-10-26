"""Compute-only acceptance test for local dependencies and persistent writes."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from paseo_runtime import STORE, mount_command, stage


@unittest.skipUnless(os.environ.get('SLURM_JOB_ID'), 'requires a compute allocation')
class RuntimeTest(unittest.TestCase):
    def test_plugins_local_and_state_persistent(self):
        os.umask(0o077)
        config_home = Path(os.environ.get('PASEO_TEST_CONFIG_HOME', Path.home() / '.paseo-slurm'))
        root = stage(STORE / 'candidate.json', home=config_home)
        mounts = json.loads((root / 'mounts.json').read_text())
        with tempfile.TemporaryDirectory(prefix='runtime-check-', dir=REPO / 'runs') as directory:
            home = Path(directory)
            config = json.loads((config_home / 'config.json').read_text())
            (home / 'config.json').write_text(json.dumps({
                'pluginsEnabled': config['pluginsEnabled'], 'plugins': config['plugins']}))
            probe = home / 'persistent-write-probe'
            # Verify actual device IDs in the service's mount namespace, then
            # read a write from outside that namespace. Do not alter real state.
            check = '''import json,os,pathlib,sys
os.close(os.open('/dev/null', os.O_RDWR))
for item in json.loads(sys.argv[1]):
 assert os.stat(item['source']).st_dev == os.stat('/tmp').st_dev, item
for directory in json.loads(sys.argv[3]):
 assert os.stat(directory).st_dev == os.stat('/tmp').st_dev, directory
pathlib.Path(sys.argv[2]).write_text('survives the mount namespace')
'''
            subprocess.run(mount_command(root, ['/usr/bin/python3', '-c', check,
                           json.dumps(mounts), str(probe), json.dumps([p['path'] for p in config['plugins'].values() if p.get('enabled', True)])]), check=True, timeout=30)
            self.assertEqual(probe.read_text(), 'survives the mount namespace')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = os.environ | {'PASEO_LISTEN': f'127.0.0.1:{port}', 'PASEO_RELAY_ENABLED': 'false'}
            env.pop('NODE_OPTIONS', None)
            env.pop('PASEO_DIAGNOSTIC_DIR', None)
            started = time.monotonic()
            with (home / 'daemon.log').open('w') as log:
                child = subprocess.Popen(['/usr/bin/python3', str(REPO / 'paseo_runtime.py'),
                    'run', '--candidate', '--home', str(home)], env=env, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while True:
                        self.assertIsNone(child.poll(), (home / 'daemon.log').read_text()[-4000:])
                        self.assertLess(time.monotonic() - started, 180,
                                        (home / 'daemon.log').read_text()[-4000:])
                        try:
                            ready = subprocess.run(['/usr/bin/python3', str(REPO / 'paseo_ready.py'),
                                str(home)], env=env, timeout=5)
                            if ready.returncode == 0:
                                break
                        except subprocess.TimeoutExpired:
                            pass
                        time.sleep(.5)
                    seconds = time.monotonic() - started
                    command = [str(root / 'bin/node'), str(root / 'cli/bin/paseo'),
                        'plugin', 'ls', '--host', f'127.0.0.1:{port}', '--json']
                    result = subprocess.run(mount_command(root, command), env=env,
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    print('PLUGIN_CATALOG', result.stdout, flush=True)
                    plugins = json.loads(result.stdout)
                    self.assertIsInstance(plugins, list)
                    by_id = {p['id']: p for p in plugins}
                    for plugin_id, spec in config['plugins'].items():
                        if spec.get('enabled', True):
                            self.assertEqual(by_id[plugin_id]['status'], 'running', by_id[plugin_id])
                    evidence = dict(seconds=seconds, mounts=mounts, plugins=plugins,
                                    persistentWriteVerified=True, job=os.environ['SLURM_JOB_ID'])
                    (STORE / 'candidate-validation.json').write_text(json.dumps(evidence, indent=2)+'\n')
                    print('PLUGIN_STARTUP_SECONDS', seconds, flush=True)
                finally:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait()


if __name__ == '__main__':
    unittest.main()
