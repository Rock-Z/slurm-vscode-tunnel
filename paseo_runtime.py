#!/usr/bin/env python3
"""Build a persistent runtime archive; stage and run it on a compute node."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time

REPO = Path(__file__).resolve().parent
STORE = REPO / 'runs' / 'paseo-runtime'
NODE = Path.home() / '.local/node24/bin/node'
CLI = Path.home() / 'Apps/npm/lib/node_modules/@getpaseo/cli'
COMMANDS = ('npm', 'npx', 'codex', 'claude', 'bun', 'uv', 'uvx')


def source_state(home):
    # A daemon's children inherit its dependency overlays. Never publish those
    # copies as the persistent installation (including via a manual build).
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        if '/paseo-runtime-' in line.split()[3]:
            raise RuntimeError('Runtime sources are overlaid; launch outside the Paseo mount namespace')
    sources = [NODE.parent.parent.resolve(), CLI.parents[1].resolve()]
    config = json.loads((home / 'config.json').read_text())
    for plugin in config.get('plugins', {}).values():
        if plugin.get('enabled', True):
            if plugin.get('source') != 'directory':
                raise RuntimeError('Resolve managed plugins to their installed directory before building')
            source = Path(plugin['path']).expanduser().resolve()
            # Source checkouts must stay writable on GPFS, including worktrees
            # whose .git is a file. Published plugins still get local I/O.
            if not (source / '.git').exists():
                sources.append(source)
    commands = {}
    for command in COMMANDS:
        executable = shutil.which(command)
        if executable:
            source = Path(executable).resolve()
            commands[command] = str(source)
            if str(source).startswith('/gpfs/') and not any(source == p or p in source.parents for p in sources):
                sources.append(source)
    # npm installs replace package directories even on a same-version reinstall.
    # Inspect only package roots, never their recursive dependency trees.
    paths = {NODE, REPO / 'paseo_runtime.py', REPO / 'paseo_diagnostics.mjs', *sources}
    paths.update(Path(p) for p in commands.values())
    for source in sources:
        if source.is_dir():
            paths.update(source.iterdir())
    for entry in sources[1].iterdir():
        if entry.name.startswith('@') and entry.is_dir():
            paths.update(entry.iterdir())
    for path in list(paths):
        if path.is_dir():
            paths.add(path / 'package.json')
    stats = {}
    for path in sorted(paths):
        try:
            stat = path.stat()
        except FileNotFoundError:
            stats[str(path)] = None
        else:
            stats[str(path)] = [stat.st_ino, stat.st_mode, stat.st_size,
                                stat.st_mtime_ns, stat.st_ctime_ns]
    return dict(sources=[str(p) for p in sources], commands=commands, stats=stats)


def ensure_current(home):
    return build(home, reuse=True)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def build(home, candidate=False, reuse=False):
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('Build the archive inside a compute allocation')
    STORE.mkdir(parents=True, exist_ok=True)
    # Serialize builders across jobs and recheck after acquiring the lock.
    with (STORE / 'build.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = source_state(home)
        manifest_path = STORE / ('candidate.json' if candidate else 'current.json')
        if reuse and manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest.get('sourceState') == state and (STORE / manifest['archive']).is_file():
                return manifest
        return build_archive(home, state, manifest_path)


def build_archive(home, state, manifest_path):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='paseo-bundle-') as temporary:
        archive = Path(temporary) / 'runtime.tar.gz'
        # Keep persistent config/state out of the archive. Only installed code is mounted.
        sources = [Path(p) for p in state['sources']]
        mounts = [dict(source=str(source), archive=f'dependencies/{i}') for i, source in enumerate(sources)]
        # uvx resolves its own executable and runs the sibling uv binary.
        # Keep the pair together without copying the rest of ~/.local/bin.
        for mount in mounts:
            for name in ('uv', 'uvx'):
                if mount['source'] == state['commands'].get(name):
                    mount['archive'] = f'dependencies/uv-tools/{name}'
        metadata = Path(temporary) / 'mounts.json'
        metadata.write_text(json.dumps(mounts, indent=2) + '\n')
        links = Path(temporary) / 'links'
        (links / 'bin').mkdir(parents=True)
        (links / 'bin/node').symlink_to('../dependencies/0/bin/node')
        (links / 'cli').symlink_to('dependencies/1/@getpaseo/cli')
        for command, executable in state['commands'].items():
            source = Path(executable)
            for mount in mounts:
                base = Path(mount['source'])
                if source == base or base in source.parents:
                    target = Path(mount['archive']) / source.relative_to(base)
                    (links / 'bin' / command).symlink_to(Path('..') / target)
                    break
        def package_filter(member):
            return None if '.git' in Path(member.name).parts else member
        with tarfile.open(archive, 'w:gz', compresslevel=1) as bundle:
            for mount in mounts:
                print(json.dumps(dict(kind='packaging', source=mount['source'])), flush=True)
                bundle.add(mount['source'], arcname=mount['archive'], filter=package_filter)
            bundle.add(metadata, arcname='mounts.json')
            bundle.add(links / 'bin', arcname='bin')
            bundle.add(links / 'cli', arcname='cli')
            bundle.add(REPO / 'paseo_diagnostics.mjs', arcname='paseo_diagnostics.mjs')
        # Validate archive links locally, avoiding a second GPFS metadata walk.
        with tarfile.open(archive) as bundle:
            for member in bundle:
                tarfile.data_filter(member, Path(temporary) / 'validate')
        sha = digest(archive)
        destination = STORE / (sha + '.tar.gz')
        manifest = dict(format=2, mounts=mounts, archive=destination.name, sha256=sha, bytes=archive.stat().st_size,
                        paseoVersion=json.loads((CLI / 'package.json').read_text())['version'],
                        nodeVersion=subprocess.check_output([str(NODE), '--version'], text=True).strip(),
                        sourceState=state)
        # Ordinary package updaters do not take our lock. Refuse a moving source
        # rather than labelling a mixed archive as current.
        if source_state(home) != state:
            raise RuntimeError('Runtime sources changed during build; retry after the update completes')
        with tempfile.TemporaryDirectory(dir=STORE, prefix='publish-') as publication:
            pending = Path(publication) / 'archive'
            shutil.copyfile(archive, pending)
            pending.replace(destination)
            if source_state(home) != state:
                raise RuntimeError('Runtime sources changed during publication; retry after the update completes')
            pending = Path(publication) / 'manifest'
            pending.write_text(json.dumps(manifest, indent=2) + '\n')
            pending.replace(manifest_path)
        print(json.dumps(dict(kind='bundle_built', seconds=time.monotonic()-started,
                              **{k: v for k, v in manifest.items() if k != 'sourceState'})), flush=True)
        return manifest


def stage(manifest_path=None, home=None):
    job = os.environ['SLURM_JOB_ID']
    manifest = (json.loads(manifest_path.read_text()) if manifest_path else
                ensure_current(home or Path.home() / '.paseo-slurm'))
    root = Path(f'/tmp/paseo-runtime-{os.getuid()}-{job}-{manifest["sha256"][:12]}')
    started = time.monotonic()
    lock = Path(f'/tmp/paseo-runtime-{os.getuid()}-{job}.lock')
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if not root.exists():
            with tempfile.TemporaryDirectory(prefix=root.name + '-', dir='/tmp') as temporary:
                temporary = Path(temporary)
                archive = temporary / 'runtime.tar.gz'
                shutil.copyfile(STORE / manifest['archive'], archive)
                if digest(archive) != manifest['sha256']:
                    raise RuntimeError('Runtime archive checksum mismatch')
                unpacked = temporary / 'unpacked'
                unpacked.mkdir()
                with tarfile.open(archive) as bundle:
                    bundle.extractall(unpacked, filter='data')
                (unpacked / 'bin/paseo').symlink_to('../cli/bin/paseo')
                unpacked.replace(root)
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise RuntimeError('Runtime directory is not private')
    if manifest_path is None and source_state(home or Path.home() / '.paseo-slurm') != manifest['sourceState']:
        raise RuntimeError('Runtime sources changed during staging; retry after the update completes')
    print(json.dumps(dict(kind='runtime_staged', root=str(root), seconds=time.monotonic()-started,
                          **{k: v for k, v in manifest.items() if k != 'sourceState'})), flush=True)
    return root


def mount_command(root, command):
    metadata = root / 'mounts.json'
    if not metadata.exists():  # The currently deployed v1 archive remains usable during rollout.
        return command
    # A plain root bind is nodev: ignored child stdio must still open /dev/null.
    argv = ['/usr/bin/bwrap', '--bind', '/', '/', '--dev-bind', '/dev', '/dev']
    for mount in json.loads(metadata.read_text()):
        argv += ['--bind', str(root / mount['archive']), mount['source']]
    return argv + ['--'] + command


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'stage', 'run'])
    parser.add_argument('--home', type=Path, default=Path.home() / '.paseo-slurm')
    parser.add_argument('--candidate', action='store_true', help='Build or run the candidate archive without activating it')
    args = parser.parse_args()
    if args.action == 'build':
        build(args.home, args.candidate)
        return
    root = stage(STORE / 'candidate.json' if args.candidate else None, home=args.home)
    if args.action == 'run':
        os.environ['PATH'] = str(root / 'bin') + ':' + os.environ['PATH']
        os.environ['PASEO_DIAGNOSTIC_NODE_OPTIONS'] = json.dumps(os.environ.get('NODE_OPTIONS'))
        os.environ['NODE_OPTIONS'] = (os.environ.get('NODE_OPTIONS', '') +
                                      ' --import ' + str(root / 'paseo_diagnostics.mjs')).strip()
        node = str(root / 'bin/node')
        command = mount_command(root, [node, '--disable-warning=DEP0040', str(root / 'cli/bin/paseo'),
                                      'daemon', 'run', '--home', str(args.home)])
        os.execv(command[0], command)


if __name__ == '__main__':
    main()
