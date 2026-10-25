#!/usr/bin/env python3
"""Build a versioned plugin installation without modifying its development checkout."""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile

STORE = Path.home() / "Apps/data/paseo-plugin-releases"
CACHE = Path(f"/tmp/paseo-plugin-releases-{os.getuid()}")


def snapshot(source, destination):
    """Include current tracked/untracked source; exclude Git metadata and ignored state."""
    names = subprocess.check_output(['git', '-C', str(source), 'ls-files',
        '--cached', '--others', '--exclude-standard', '--deduplicate', '-z']).decode().split('\0')
    destination.mkdir()
    for name in filter(None, names):
        origin = source / name
        if not origin.exists() and not origin.is_symlink():  # a tracked deletion
            continue
        if not origin.resolve().is_relative_to(source.resolve()):
            raise ValueError(f'Source link escapes checkout: {origin}')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target, follow_symlinks=False)


def publish(package, store, plugin_id):
    """Publish one immutable archive; never expand dependency trees on GPFS."""
    if not re.fullmatch(r'[a-z][a-z0-9-]*', plugin_id):
        raise ValueError(f'Invalid plugin ID: {plugin_id}')
    parent = store / plugin_id
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='plugin-archive-', dir='/tmp') as tmp:
        archive = Path(tmp) / 'package.tar.gz'
        def stable_metadata(member):
            member.mtime = member.uid = member.gid = 0
            member.uname = member.gname = ''
            return member
        with archive.open('wb') as output, gzip.GzipFile(fileobj=output, mode='wb', compresslevel=1, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as bundle:
                bundle.add(package, arcname='package', filter=stable_metadata)
        revision = hashlib.sha256(archive.read_bytes()).hexdigest()
        destination = parent / (revision + '.tar.gz')
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix='.publish-', dir=parent) as pending:
                copy = Path(pending) / 'archive'
                shutil.copyfile(archive, copy)
                copy.rename(destination)
    return destination


def extract_release(archive, destination):
    if destination.is_relative_to(CACHE):
        CACHE.mkdir(mode=0o700, exist_ok=True)
        if CACHE.is_symlink() or CACHE.stat().st_uid != os.getuid() or CACHE.stat().st_mode & 0o077:
            raise RuntimeError(f'Plugin cache is not private: {CACHE}')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (destination.parent / (destination.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _extract_release(archive, destination)


def _extract_release(archive, destination):
    expected = archive.name.removesuffix('.tar.gz')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if (destination.stat().st_uid != os.getuid() or destination.stat().st_mode & 0o077
                or (destination / '.archive-sha256').read_text() != expected):
            raise RuntimeError(f'Invalid local plugin cache: {destination}')
        return destination
    with tempfile.TemporaryDirectory(prefix='.extract-', dir=destination.parent) as tmp:
        tmp = Path(tmp); local_archive = tmp / 'package.tar.gz'
        shutil.copyfile(archive, local_archive)
        if hashlib.sha256(local_archive.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Plugin archive checksum mismatch: {archive}')
        with tarfile.open(local_archive) as bundle:
            bundle.extractall(tmp, filter='data')
        package = tmp / 'package'
        package.chmod(0o700)
        (package / '.archive-sha256').write_text(expected)
        package.rename(destination)
    return destination


def managed_path(path):
    path = Path(path)
    if not path.is_relative_to(CACHE):
        return None
    parts = path.relative_to(CACHE).parts
    if (len(parts) != 2 or not re.fullmatch(r'[a-z][a-z0-9-]*', parts[0]) or len(parts[1]) != 64
            or any(c not in '0123456789abcdef' for c in parts[1])):
        raise ValueError(f'Invalid managed plugin path: {path}')
    return parts


def stage_plugins(home):
    config = json.loads((home / 'config.json').read_text())
    for plugin in config.get('plugins', {}).values():
        if plugin.get('enabled', True) and (parts := managed_path(plugin['path'])):
            plugin_id, revision = parts
            extract_release(STORE / plugin_id / (revision + '.tar.gz'), Path(plugin['path']))


def prepare(source, store):
    manifest = json.loads((source / 'paseo-plugin.json').read_text())
    plugin_id = manifest['id']
    # A node-local npm cache avoids small-file traffic on GPFS during builds.
    env = os.environ | {'npm_config_cache': f'/tmp/paseo-plugin-npm-{os.getuid()}'}
    env.pop('NODE_OPTIONS', None)
    with tempfile.TemporaryDirectory(prefix='paseo-plugin-build-', dir='/tmp') as tmp:
        root = Path(tmp); checkout = root / 'source'
        snapshot(source, checkout)
        def npm(*args, cwd=checkout, capture=False):
            return subprocess.run(['npm', *args], cwd=cwd, env=env, check=True,
                text=True, stdout=subprocess.PIPE if capture else None)
        npm('ci', '--ignore-scripts', '--no-audit', '--no-fund')
        npm('run', 'build', '--if-present')
        npm('run', 'typecheck')
        packed = json.loads(npm('pack', '--ignore-scripts', '--json', '--pack-destination',
                                str(root), capture=True).stdout)[0]
        archive = root / packed['filename']
        with tarfile.open(archive) as bundle:
            bundle.extractall(root, filter='data')
        package = root / 'package'
        # npm pack excludes package-lock; the deployment and autoresearch CLI need it.
        shutil.copy2(checkout / 'package-lock.json', package / 'package-lock.json')
        npm('ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund', cwd=package)
        (package / 'deployment.json').write_text(json.dumps(dict(
            source=str(source),
            gitCommit=subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
        ), indent=2) + '\n')
        archive = publish(package, store, plugin_id)
        release = extract_release(archive, CACHE / plugin_id / archive.name.removesuffix('.tar.gz'))
        return plugin_id, release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path.cwd())
    parser.add_argument('--home', type=Path, default=Path.home() / '.paseo-slurm')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    args.source = args.source.resolve(); args.home = args.home.resolve()
    if not os.environ.get('SLURM_JOB_ID'):
        parser.error('Run on the daemon compute node inside its Slurm allocation.')
    if '/paseo-runtime-' in Path('/proc/self/mountinfo').read_text():
        # Paseo terminals inherit mounts. Publish to persistent storage outside them.
        command = ['env', f'SLURM_JOB_ID={os.environ["SLURM_JOB_ID"]}',
            '/usr/bin/python3', str(Path(__file__).resolve()), '--source', str(args.source),
            '--home', str(args.home)] + (['--prepare-only'] if args.prepare_only else [])
        return subprocess.call(['ssh', '-o', 'BatchMode=yes', socket.gethostname(), shlex.join(command)])
    os.umask(0o077)
    store = STORE
    store.mkdir(parents=True, exist_ok=True)
    with (store / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        plugin_id, release = prepare(args.source, store)
        print(json.dumps(dict(plugin=plugin_id, release=str(release))), flush=True)
        if not args.prepare_only:
            from paseo_runtime import CLI, NODE
            subprocess.run([str(NODE), str(Path(__file__).with_name('paseo_plugin_activate.mjs')),
                str(CLI), str(args.home), plugin_id, str(release)], check=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
