#!/usr/bin/env python3
"""Job-scoped passive Paseo sampler; slow-call stacks come from the Node hook."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import threading
import time
import urllib.request


def logger(path, size=8 * 1024 * 1024, backups=7):
    result = logging.Logger(str(path))
    handler = RotatingFileHandler(path, maxBytes=size, backupCount=backups)
    handler.setFormatter(logging.Formatter('%(message)s'))
    result.addHandler(handler)
    return result


def raise_log_error(record):
    raise  # RotatingFileHandler calls this inside its exception handler.


def main():
    os.umask(0o077)
    local = Path(os.environ['PASEO_DIAGNOSTIC_DIR'])
    local.mkdir(mode=0o700, parents=True, exist_ok=True)
    if local.stat().st_uid != os.getuid() or local.stat().st_mode & 0o077:
        raise RuntimeError('Diagnostic directory must be private and owned by this user')
    session = Path(os.environ['CS_SESSION_DIR']) / 'paseo-diagnostics'
    samples = logger(local / 'samples.jsonl')
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())

    def emit(kind, **fields):
        samples.info(json.dumps(dict(time=time.time(), kind=kind, **fields)))

    # GPFS export runs independently, so it cannot stall /proc sampling.
    def export():
        positions = {}
        remote = None
        while True:
            try:
                if remote is None:
                    session.mkdir(mode=0o700, parents=True, exist_ok=True)
                    remote = logger(session / 'events.jsonl')
                    remote.handlers[0].handleError = raise_log_error
                files = sorted(local.glob('*.jsonl*'), key=lambda p: p.stat().st_mtime)
                present = set()
                for path in files:
                    with path.open('rb') as stream:
                        inode = os.fstat(stream.fileno()).st_ino
                        present.add(inode)
                        stream.seek(positions.get(inode, 0))
                        while line := stream.readline():
                            if not line.endswith(b'\n'):
                                break
                            remote.info(line.decode('utf8').rstrip('\n'))
                            positions[inode] = stream.tell()
                positions = {k: v for k, v in positions.items() if k in present}
            except OSError as exc:
                emit('export_error', error=type(exc).__name__)
            if stopping.is_set():
                break
            stopping.wait(10)

    def health():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = os.environ.get('PASEO_DIAGNOSTIC_HEALTH_URL', 'http://127.0.0.1:6767/api/health')
        while not stopping.is_set():
            start = time.monotonic()
            try:
                with opener.open(url, timeout=2) as response:
                    emit('health', ms=(time.monotonic()-start)*1000, status=response.status)
            except Exception as exc:
                emit('health', ms=(time.monotonic()-start)*1000, error=type(exc).__name__)
            stopping.wait(1)

    emit('sampler_started', hostname=socket.gethostname(), job=os.environ.get('SLURM_JOB_ID'),
         local=str(local), export=str(session), clockTicks=os.sysconf('SC_CLK_TCK'), pageBytes=os.sysconf('SC_PAGE_SIZE'))
    workers = [threading.Thread(target=fn, daemon=True) for fn in (health, export)]
    for worker in workers:
        worker.start()
    previous_pid = None
    try:
        while not stopping.is_set():
            try:
                identity = json.loads((local / 'daemon.json').read_text())
                pid = identity['pid']
                proc = Path(f'/proc/{pid}')
                stat = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()
                if stat[19] != identity['startTicks'] or proc.stat().st_uid != os.getuid():
                    raise ProcessLookupError('stale identity')
                row = dict(pid=pid, state=stat[0], cpuTicks=int(stat[11])+int(stat[12]),
                           rssPages=int(stat[21]), load=os.getloadavg())
                for name in ('schedstat', 'wchan', 'syscall'):
                    try:
                        row[name] = (proc / name).read_text().strip()
                    except PermissionError:
                        row[name] = 'permission_denied'
                # Resolve the fd for blocked read/write calls, never their buffers.
                call = row.get('syscall', '').split()
                if len(call) > 1 and call[0] in ('0', '1', '17', '18', '74', '75'):
                    try:
                        row['fdPath'] = os.readlink(proc / 'fd' / str(int(call[1], 0)))
                    except (OSError, ValueError):
                        pass
                if pid != previous_pid:
                    emit('daemon_attached', pid=pid, exe=os.readlink(proc / 'exe'),
                         cgroup=(proc / 'cgroup').read_text(),
                         affinity=sorted(os.sched_getaffinity(pid)),
                         mounts=[line for line in (proc / 'mountinfo').read_text().splitlines()
                                 if ' - gpfs ' in line or ' /tmp ' in line])
                    previous_pid = pid
                emit('process', **row)
            except (FileNotFoundError, ProcessLookupError, json.JSONDecodeError):
                pass  # Daemon not yet started, restarting, or identity being replaced.
            except Exception as exc:
                emit('sample_error', error=type(exc).__name__)
            stopping.wait(.5)
    finally:
        emit('sampler_stopped')
        stopping.set()
        for worker in workers:
            worker.join(timeout=4)


if __name__ == '__main__':
    main()
