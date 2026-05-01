#!/usr/bin/env python3
import argparse
import pathlib
import tomllib
from typing import Optional

from codeserver_lib import (
    ConfigError,
    default_config_path,
    die,
    find_auth_block,
    load_config,
    load_json,
    query_job_status,
    resolve_session_dir,
    tail_lines,
)


def print_block(title: str, block: str) -> None:
    print()
    print(f"===== {title} =====")
    print(block)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Show session status for the latest session, a profile, or a session id.",
    )
    ap.add_argument(
        "target",
        nargs="?",
        default="latest",
        help="One of: latest, a profile name, or a session id.",
    )
    ap.add_argument(
        "--config",
        default=str(default_config_path()),
        help="Path to the TOML config file.",
    )
    args = ap.parse_args()

    try:
        cfg = load_config(pathlib.Path(args.config).resolve())
    except (ConfigError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        die(f"{exc}. Use --help for usage.", code=2)
    try:
        session_dir = resolve_session_dir(cfg, args.target)
    except FileNotFoundError as exc:
        die(f"{exc}. Use --help for usage.")

    if not session_dir.exists():
        die(f"no session found for '{args.target}'. Use --help for usage.")

    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        die(f"missing metadata: {meta_path}")

    meta = load_json(meta_path)
    run_log = pathlib.Path(meta["run_log"])
    tunnel_log = pathlib.Path(meta["tunnel_log"])
    job_id: Optional[str] = meta.get("job_id")

    print(f"session:     {meta['session_id']}")
    print(f"profile:     {meta['profile']}")
    print(f"session dir: {session_dir}")
    print(f"config:      {meta['config_path']}")
    print(f"job id:      {job_id or '-'}")
    print(f"run log:     {run_log}")
    print(f"tunnel log:  {tunnel_log}")

    if job_id:
        status = query_job_status(job_id)
        print(f"job status:  {status or 'unknown'}")
    else:
        print("job status:  unknown")

    auth_found = False

    run_auth = find_auth_block(run_log)
    if run_auth:
        print_block("auth prompt from run.log", run_auth)
        auth_found = True

    tunnel_auth = find_auth_block(tunnel_log)
    if tunnel_auth:
        print_block("auth prompt from tunnel.log", tunnel_auth)
        auth_found = True

    print()
    print(f"NEEDS_REAUTH={'yes' if auth_found else 'no'}")

    if not auth_found:
        print()
        print("===== recent run.log =====")
        for line in tail_lines(run_log, 40):
            print(line)
        print()
        print("===== recent tunnel.log =====")
        for line in tail_lines(tunnel_log, 40):
            print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
