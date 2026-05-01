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
    tail_lines,
)
from codeserver_relay import format_duration, is_chain_dir, resolve_chain_or_session_dir


def print_block(title: str, block: str) -> None:
    print()
    print(f"===== {title} =====")
    print(block)


def print_session(session_dir: pathlib.Path) -> int:
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
    for title, path in (("auth prompt from run.log", run_log), ("auth prompt from tunnel.log", tunnel_log)):
        block = find_auth_block(path)
        if block:
            print_block(title, block)
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


def print_chain(chain_dir: pathlib.Path) -> int:
    chain_path = chain_dir / "chain.json"
    chain = load_json(chain_path)
    print(f"relay chain: {chain['chain_id']}")
    print(f"profile:     {chain['profile']}")
    print(f"chain dir:   {chain_dir}")
    print(f"config:      {chain['config_path']}")
    print(f"requested:   {format_duration(int(chain['requested_time_seconds']))}")
    print(f"limit:       {format_duration(int(chain['profile_max_seconds']))}")
    print(f"overlap:     {format_duration(int(chain['relay_overlap_seconds']))}")
    print(f"segments:    {len(chain.get('jobs', []))}")
    print()
    print("IDX  JOB_ID       STATE     BEGIN       DURATION  PREV_JOB    LOG")
    for job in chain.get("jobs", []):
        job_id = str(job.get("job_id") or "-")
        status = query_job_status(job_id) if job_id and not job_id.startswith("DRY-RUN") else None
        state = "unknown"
        if status:
            for field in status.split():
                if field.startswith("state="):
                    state = field.split("=", 1)[1]
                    break
        begin = format_duration(int(job.get("begin_offset_seconds", 0)))
        duration = format_duration(int(job.get("duration_seconds", 0)))
        prev = str(job.get("previous_job_id") or "-")
        print(
            f"{int(job['index']):<4} {job_id:<12} {state:<9} {begin:<11} "
            f"{duration:<9} {prev:<11} {job.get('run_log', '-') }"
        )

    auth_found = False
    for job in chain.get("jobs", []):
        for log_key in ("run_log", "tunnel_log"):
            path = pathlib.Path(job[log_key])
            block = find_auth_block(path)
            if block:
                print_block(f"auth prompt from job-{int(job['index']):03d}/{log_key}", block)
                auth_found = True
    print()
    print(f"NEEDS_REAUTH={'yes' if auth_found else 'no'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Show session status for latest, a profile, session id, chain id, or job id.")
    ap.add_argument("target", nargs="?", default="latest", help="latest, profile name, session id, chain id, or job id.")
    ap.add_argument("--config", default=str(default_config_path()), help="Path to the TOML config file.")
    args = ap.parse_args()

    try:
        cfg = load_config(pathlib.Path(args.config).resolve())
    except (ConfigError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        die(f"{exc}. Use --help for usage.", code=2)
    try:
        session_dir = resolve_chain_or_session_dir(cfg, args.target)
    except FileNotFoundError as exc:
        die(f"{exc}. Use --help for usage.")

    if not session_dir.exists():
        die(f"no session found for '{args.target}'. Use --help for usage.")
    if is_chain_dir(session_dir):
        return print_chain(session_dir)
    return print_session(session_dir)


if __name__ == "__main__":
    raise SystemExit(main())
