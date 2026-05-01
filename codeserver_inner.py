#!/usr/bin/env python3
import argparse
import os
import pathlib
import pty
import subprocess
import sys
from typing import Dict

from codeserver_lib import load_config, merged_env


def forward_pty_output(argv, env: Dict[str, str], tunnel_log: pathlib.Path) -> int:
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)

    with tunnel_log.open("ab") as logf:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            logf.write(chunk)
            logf.flush()

    _, status = os.waitpid(proc.pid, 0)
    os.close(master_fd)

    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--session-dir", required=True)
    ap.add_argument("--run-log", required=True)
    ap.add_argument("--tunnel-log", required=True)
    args = ap.parse_args()

    cfg = load_config(pathlib.Path(args.config))
    session_dir = pathlib.Path(args.session_dir)
    tunnel_log = pathlib.Path(args.tunnel_log)

    session_dir.mkdir(parents=True, exist_ok=True)
    tunnel_log.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(merged_env(cfg, args.profile))

    code_bin = cfg["code_bin"]
    code_args = cfg["code_tunnel_args"]
    argv = [code_bin] + code_args

    print(f"[codeserver_inner] host={os.uname().nodename}")
    print(f"[codeserver_inner] profile={args.profile}")
    print(f"[codeserver_inner] session_dir={session_dir}")
    print(f"[codeserver_inner] tunnel_log={tunnel_log}")
    print(f"[codeserver_inner] command={' '.join(argv)}")
    sys.stdout.flush()

    return forward_pty_output(argv, env, tunnel_log)


if __name__ == "__main__":
    raise SystemExit(main())
