#!/usr/bin/env python3
"""Check the expected local supervisor and health endpoint without loading Node."""
import json
import os
from pathlib import Path
import socket
import sys
import urllib.request

try:
    home = Path(sys.argv[1]).expanduser()
    state = json.loads((home / 'paseo.pid').read_text())
    assert state['hostname'] == socket.gethostname()
    pid = int(state['pid'])
    os.kill(pid, 0)
    job = os.environ.get('SLURM_JOB_ID')
    if job:
        assert f'/job_{job}/' in Path(f'/proc/{pid}/cgroup').read_text()
    address = state['listen']
    assert address.startswith(('127.0.0.1:', 'localhost:'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://{address}/api/health', timeout=2) as response:
        assert response.status == 200
except (OSError, ValueError, KeyError, TypeError, AssertionError):
    sys.exit(1)
