"""Exercise real archive publication/staging with small disposable installations."""
import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paseo_runtime as runtime

REAL_WHICH = shutil.which


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='runtime-freshness-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.node = self.base / 'node/bin/node'
        self.cli = self.base / 'npm/@getpaseo/cli'
        self.plugin = self.base / 'plugin'
        self.home = self.base / 'home'
        for p in (self.node.parent, self.cli / 'bin', self.home, self.plugin):
            p.mkdir(parents=True)
        self.node.write_text('#!/bin/sh\necho v24.fixture\n')
        self.node.chmod(0o755)
        (self.cli / 'package.json').write_text('{"version":"1.0"}')
        (self.cli / 'bin/paseo').write_text('old executable')
        (self.base / 'paseo_diagnostics.mjs').write_text('// fixture')
        (self.home / 'config.json').write_text(json.dumps({'plugins': {
            'fixture': {'source': 'directory', 'path': str(self.plugin)}}}))
        self.job = f'fixture-{os.getpid()}-{self.base.name}'
        self.roots = []
        original_read = Path.read_text
        def fixture_mounts(path, *args, **kwargs):
            if str(path) == '/proc/self/mountinfo':
                return ''
            return original_read(path, *args, **kwargs)
        for p in (patch.object(Path, 'read_text', fixture_mounts),
                  patch.object(runtime, 'NODE', self.node),
                  patch.object(runtime, 'CLI', self.cli),
                  patch.object(runtime, 'REPO', self.base),
                  patch.object(runtime, 'STORE', self.base / 'store'),
                  patch.object(runtime.shutil, 'which', return_value=None),
                  patch.dict(os.environ, {'SLURM_JOB_ID': self.job})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.clean_stages)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def clean_stages(self):
        for p in Path('/tmp').glob(f'paseo-runtime-{os.getuid()}-{self.job}*'):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()

    def test_reinstall_staging_and_running_copy(self):
        old = runtime.stage(home=self.home)
        manifest_path = runtime.STORE / 'current.json'
        published = manifest_path.stat().st_mtime_ns
        with patch.object(runtime, 'build_archive', side_effect=AssertionError('unexpected rebuild')):
            self.assertEqual(runtime.stage(home=self.home), old)
        self.assertEqual(manifest_path.stat().st_mtime_ns, published)
        # Simulate npm replacing a scoped package without changing its version.
        replacement = self.cli.with_name('replacement')
        shutil.copytree(self.cli, replacement)
        (replacement / 'bin/paseo').write_text('new executable')
        shutil.rmtree(self.cli)
        replacement.rename(self.cli)
        new = runtime.stage(home=self.home)
        self.assertNotEqual(new, old)
        self.assertEqual((old / 'cli/bin/paseo').read_text(), 'old executable')
        self.assertEqual((new / 'cli/bin/paseo').read_text(), 'new executable')
        self.assertEqual(json.loads(manifest_path.read_text())['paseoVersion'], '1.0')

    def test_node_other_packages_plugins_and_hook(self):
        original = runtime.ensure_current(self.home)
        for path in (self.node, self.plugin / 'package.json', self.base / 'paseo_diagnostics.mjs',
                     self.base / 'paseo_runtime.py',
                     self.cli.parents[1] / 'other/package.json'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#!/bin/sh\necho changed\n' if path == self.node else '{}')
            fresh = runtime.ensure_current(self.home)
            self.assertNotEqual(fresh['sha256'], original['sha256'])
            original = fresh

    def test_launch_saves_original_node_options_for_worker(self):
        for original in (None, '', '--no-warnings'):
            with self.subTest(original=original), patch.dict(os.environ):
                if original is None:
                    os.environ.pop('NODE_OPTIONS', None)
                else:
                    os.environ['NODE_OPTIONS'] = original
                with patch.object(runtime, 'stage', return_value=self.base), patch.object(
                        sys, 'argv', ['paseo_runtime.py', 'run', '--home', str(self.home)]), patch.object(
                        runtime.os, 'execv') as execute:
                    runtime.main()
                self.assertEqual(json.loads(os.environ['PASEO_DIAGNOSTIC_NODE_OPTIONS']), original)
                self.assertIn('--import ' + str(self.base / 'paseo_diagnostics.mjs'),
                              os.environ['NODE_OPTIONS'])
                self.assertIn('daemon', execute.call_args.args[1])

    def test_git_plugins_are_live_but_published_plugins_are_packaged(self):
        for marker in ('directory', 'worktree'):
            with self.subTest(marker=marker):
                git = self.plugin / '.git'
                if marker == 'directory':
                    git.mkdir()
                else:
                    git.rmdir()
                    git.write_text('gitdir: /elsewhere/worktrees/plugin')
                root = runtime.stage(home=self.home)
                mounts = json.loads((root / 'mounts.json').read_text())
                self.assertNotIn(str(self.plugin), [m['source'] for m in mounts])
                self.assertNotIn(str(self.plugin), runtime.mount_command(root, ['true']))
                state = runtime.source_state(self.home)
                (self.plugin / 'source.js').write_text(marker)
                self.assertEqual(runtime.source_state(self.home), state)
        git.unlink()
        root = runtime.stage(home=self.home)
        self.assertIn(str(self.plugin), runtime.mount_command(root, ['true']))

    def test_unpacked_uvx_can_find_uv(self):
        uv, uvx = (REAL_WHICH(name) for name in ('uv', 'uvx'))
        if not uv or not uvx:
            self.skipTest('requires installed uv and uvx')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runs') as directory:
            commands = {}
            for name, executable in (('uv', uv), ('uvx', uvx)):
                target = Path(directory) / name
                shutil.copyfile(executable, target)
                target.chmod(0o755)
                commands[name] = str(target)
            with patch.object(runtime.shutil, 'which', side_effect=lambda name: commands.get(name)):
                root = runtime.stage(home=self.home)
        self.assertEqual((root / 'bin/uv').resolve().parent, (root / 'bin/uvx').resolve().parent)
        env = os.environ.copy()
        env.pop('NODE_OPTIONS', None)
        result = subprocess.run([str(root / 'bin/uvx'), '--version'], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('uvx', result.stdout)

    def test_relocated_installation_roots(self):
        # Home aliases point to project storage; package the actual directories.
        relocated = self.base / 'project/apps'
        relocated.mkdir(parents=True)
        node_root = self.node.parent.parent
        node_root.rename(relocated / 'node24')
        node_root.symlink_to(relocated / 'node24')
        npm_root = self.cli.parents[1]
        npm_root.rename(relocated / 'npm')
        apps = self.base / 'Apps'
        apps.symlink_to(relocated)
        cli_alias = apps / 'npm/@getpaseo/cli'
        with patch.object(runtime, 'CLI', cli_alias), patch.object(
                runtime.shutil, 'which', side_effect=lambda name:
                str(self.node) if name == 'node' else
                str(cli_alias / 'bin/paseo') if name == 'codex' else None):
            state = runtime.source_state(self.home)
            self.assertEqual(state['sources'][:2],
                             [str(relocated / 'node24'), str(relocated / 'npm')])
            manifest = runtime.ensure_current(self.home)
            with tarfile.open(runtime.STORE / manifest['archive']) as bundle:
                self.assertTrue(bundle.getmember('dependencies/0').isdir())
                self.assertTrue(bundle.getmember('dependencies/1').isdir())
            root = runtime.stage(home=self.home)
            self.assertEqual((root / 'bin/node').read_text(), self.node.read_text())
            self.assertEqual((root / 'bin/codex').read_text(), 'old executable')
            command = runtime.mount_command(root, ['true'])
            self.assertIn(str(relocated / 'node24'), command)
            self.assertIn(str(relocated / 'npm'), command)
            self.assertNotIn(str(node_root), command)
            (cli_alias / 'package.json').write_text('{"version":"2.0"}')
            self.assertNotEqual(runtime.ensure_current(self.home)['sha256'], manifest['sha256'])
            replacement = relocated / 'node24-new'
            shutil.copytree(relocated / 'node24', replacement)
            node_root.unlink()
            node_root.symlink_to(replacement)
            self.assertNotEqual(runtime.source_state(self.home), state)

    def test_external_package_symlink_still_rejected(self):
        external = self.base / 'external'
        external.mkdir()
        (self.cli / 'external').symlink_to(external)
        with self.assertRaises(tarfile.AbsoluteLinkError):
            runtime.ensure_current(self.home)

    def test_candidate_stays_pinned(self):
        current = runtime.ensure_current(self.home)
        runtime.build(self.home, candidate=True)
        candidate = runtime.STORE / 'candidate.json'
        before = candidate.read_bytes()
        (self.cli / 'package.json').write_text('{"version":"2.0"}')
        runtime.ensure_current(self.home)
        self.assertEqual(candidate.read_bytes(), before)
        root = runtime.stage(candidate)
        self.assertEqual(json.loads((root / 'cli/package.json').read_text())['version'], '1.0')
        self.assertNotEqual(json.loads((runtime.STORE / 'current.json').read_text())['sha256'], current['sha256'])

    def test_failure_and_update_during_build_do_not_publish(self):
        runtime.ensure_current(self.home)
        path = runtime.STORE / 'current.json'
        before = path.read_bytes()
        (self.cli / 'package.json').write_text('{"version":"2.0"}')
        with patch.object(runtime.tarfile, 'open', side_effect=OSError('fixture build failure')):
            with self.assertRaises(OSError):
                runtime.stage(home=self.home)
        self.assertEqual(path.read_bytes(), before)
        real_digest = runtime.digest
        def changing_digest(archive):
            result = real_digest(archive)
            (self.cli / 'package.json').write_text('{"version":"3.0"}')
            return result
        with patch.object(runtime, 'digest', side_effect=changing_digest):
            with self.assertRaisesRegex(RuntimeError, 'changed during build'):
                runtime.ensure_current(self.home)
        self.assertEqual(path.read_bytes(), before)

    def test_concurrent_startups_publish_one_archive(self):
        ctx = multiprocessing.get_context('fork')
        start = ctx.Event()
        def launch():
            start.wait()
            runtime.stage(home=self.home)
        children = [ctx.Process(target=launch) for _ in range(3)]
        for child in children:
            child.start()
        start.set()
        for child in children:
            child.join(30)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(len(list(runtime.STORE.glob('*.tar.gz'))), 1)
        self.assertEqual(list(runtime.STORE.glob('publish-*')), [])

    def test_namespace_guard(self):
        original = Path.read_text
        def mounted(path, *args, **kwargs):
            if str(path) == '/proc/self/mountinfo':
                return '1 2 0:1 /paseo-runtime-123/dependencies/1 /global rw - tmpfs tmpfs rw\n'
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', mounted):
            with self.assertRaisesRegex(RuntimeError, 'overlaid'):
                runtime.ensure_current(self.home)

    def test_external_executable_retarget_and_replacement(self):
        # Production also bundles native GPFS tools outside Node/npm.
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runs') as directory:
            binary = Path(directory) / 'uv'
            binary.write_text('old native tool')
            with patch.object(runtime.shutil, 'which', side_effect=lambda name: str(binary) if name == 'uv' else None):
                first = runtime.stage(home=self.home)
                binary.write_text('updated native tool')
                second = runtime.stage(home=self.home)
                self.assertEqual((first / 'bin/uv').read_text(), 'old native tool')
                self.assertEqual((second / 'bin/uv').read_text(), 'updated native tool')
                replacement = binary.with_name('uv-new')
                binary.rename(replacement)
            with patch.object(runtime.shutil, 'which', side_effect=lambda name: str(replacement) if name == 'uv' else None):
                third = runtime.ensure_current(self.home)
                self.assertIn(str(replacement), third['sourceState']['sources'])


if __name__ == '__main__':
    unittest.main()
