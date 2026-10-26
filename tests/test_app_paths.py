"""Check the shared launch wrapper and actual tunnel/sidecar inheritance."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codeserver_submit import write_batch_script

WRAPPER = ROOT / 'with_app_paths.sh'


class AppPathsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='app-paths-')
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.bashrc = self.home / '.bashrc'
        self.bashrc.write_text('touch "$HOME/outside-block"\nreturn\n'
            '# app-storage:begin\n'
            'export APP_STORAGE_ROOT="$HOME/project apps"\n'
            'export UV_CACHE_DIR="$APP_STORAGE_ROOT/cache"\n'
            'export LOAD_COUNT=$(( ${LOAD_COUNT:-0} + 1 ))\n'
            'private_value=not_exported\n'
            '# app-storage:end\n'
            'export UNWANTED=bad\n')
        self.env = os.environ | {'HOME': str(self.home),
            'PATH': '/tmp/runtime/bin:/usr/bin:/bin',
            'UV_LINK_MODE': 'copy', 'CONDA_ALWAYS_COPY': 'true',
            'CONDA_ALLOW_SOFTLINKS': 'false'}
        for key in ('LOAD_COUNT', 'UNWANTED', 'private_value'):
            self.env.pop(key, None)

    def assert_storage(self, env):
        self.assertEqual(env['APP_STORAGE_ROOT'], str(self.home / 'project apps'))
        self.assertEqual(env['UV_CACHE_DIR'], str(self.home / 'project apps/cache'))
        self.assertEqual(env['LOAD_COUNT'], '1')
        self.assertEqual(env['PATH'], self.env['PATH'])
        for key in ('UV_LINK_MODE', 'CONDA_ALWAYS_COPY', 'CONDA_ALLOW_SOFTLINKS'):
            self.assertEqual(env[key], self.env[key])
        self.assertNotIn('UNWANTED', env)
        self.assertNotIn('private_value', env)
        self.assertFalse((self.home / 'outside-block').exists())

    def test_exec_argv_and_only_trusted_block_even_with_bash_env(self):
        result = subprocess.run([str(WRAPPER), '/usr/bin/python3', '-c',
            'import json,os,sys; print(json.dumps([dict(os.environ),sys.argv[1:]]))',
            'a b', '$(touch should-not-exist)'],
            env=self.env | {'BASH_ENV': str(self.bashrc)}, capture_output=True, text=True, check=True)
        env, argv = json.loads(result.stdout)
        self.assert_storage(env)
        self.assertEqual(argv, ['a b', '$(touch should-not-exist)'])

    def test_invalid_missing_markers_and_failing_exports_abort(self):
        for contents in ('export UNWANTED=bad\n',
                         '# app-storage:begin\nexport A=1\n',
                         '# app-storage:end\n# app-storage:begin\n',
                         '# app-storage:begin\n# app-storage:end\n# app-storage:end\n',
                         '# app-storage:begin\nfalse\n# app-storage:end\n'):
            with self.subTest(contents=contents):
                self.bashrc.write_text(contents)
                result = subprocess.run([str(WRAPPER), '/usr/bin/touch', str(self.home / 'launched')],
                    env=self.env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / 'launched').exists())
                if 'false' not in contents:
                    self.assertIn('app-storage block', result.stderr)

    def test_existing_direct_and_new_batch_startups(self):
        probe = self.home / 'probe.py'
        probe.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ['HOME'])
if '--version' in sys.argv:
    print('fixture')
elif len(sys.argv) > 1 and sys.argv[1] == 'sidecar':
    (home / 'sidecar.json').write_text(json.dumps(dict(os.environ)))
    time.sleep(30)
else:
    for _ in range(100):
        if (home / 'sidecar.json').exists(): break
        time.sleep(.05)
    (home / 'tunnel.json').write_text(json.dumps(dict(os.environ)))
''')
        probe.chmod(0o755)
        config = self.home / 'codeserver.toml'
        config.write_text(f'''root_dir = {json.dumps(str(self.home))}
code_bin = {json.dumps(str(probe))}
code_tunnel_args = []
default_profile = "cpu"
[sidecars.probe]
command = "exec /usr/bin/python3 {probe} sidecar"
ready_command = "test -f {self.home}/sidecar.json"
ready_timeout = "5s"
[profiles.cpu]
sidecars = ["probe"]
''')
        for batch in (False, True):
            with self.subTest(batch=batch):
                for name in ('sidecar.json', 'tunnel.json'):
                    (self.home / name).unlink(missing_ok=True)
                logs = self.home / ('batch' if batch else 'direct')
                args = [str(ROOT / 'codeserver_inner.py'), '--config', str(config),
                    '--profile', 'cpu', '--session-dir', str(logs),
                    '--run-log', str(logs / 'run.log'), '--tunnel-log', str(logs / 'tunnel.log')]
                if batch:
                    script = self.home / 'batch.sh'
                    write_batch_script(script, '/usr/bin/python3', ROOT / 'codeserver_inner.py',
                        config, 'cpu', logs, logs / 'run.log', logs / 'tunnel.log', [])
                    command = ['/bin/bash', str(script)]
                else:
                    command = ['/usr/bin/python3', *args]
                result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for name in ('sidecar.json', 'tunnel.json'):
                    self.assert_storage(json.loads((self.home / name).read_text()))


if __name__ == '__main__':
    unittest.main()
