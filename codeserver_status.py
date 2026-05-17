#!/usr/bin/env python3
import argparse
import datetime as dt
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
    resolve_session_dir,
    run_capture,
    tail_lines,
)


def print_block(title: str, block: str) -> None:
    print()
    print(f"===== {title} =====")
    print(block)


def parse_slurm_duration(value: str) -> Optional[int]:
    raw = value.strip()
    if not raw or raw in {"INVALID", "N/A", "NOT_SET", "UNLIMITED"}:
        return None

    days = 0
    if "-" in raw:
        day_text, raw = raw.split("-", 1)
        if not day_text.isdigit():
            return None
        days = int(day_text)

    parts = raw.split(":")
    if not all(part.isdigit() for part in parts):
        return None

    nums = [int(part) for part in parts]
    if len(nums) == 2:
        hours = 0
        minutes, seconds = nums
    elif len(nums) == 3:
        hours, minutes, seconds = nums
    else:
        return None

    if minutes >= 60 or seconds >= 60:
        return None
    return (((days * 24) + hours) * 60 + minutes) * 60 + seconds


def format_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    hours, rem = divmod(seconds, 60 * 60)
    minutes, _ = divmod(rem, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}min")
    return " ".join(parts)


def parse_slurm_start(value: str) -> Optional[dt.datetime]:
    raw = value.strip()
    if not raw or raw in {"N/A", "Unknown"}:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def format_pending_reason(value: str) -> str:
    raw = value.strip() or "unknown"
    raw = raw.strip("()").upper()
    return f"({raw})"


def capture_status_cmd(argv: list[str]) -> tuple[int, str, str]:
    try:
        return run_capture(argv)
    except OSError as exc:
        return 127, "", str(exc)


def query_status_line(job_id: Optional[str]) -> str:
    if not job_id:
        return "unknown"

    rc, out, _ = capture_status_cmd(["squeue", "-h", "-j", job_id, "-o", "%T|%R|%S|%M|%l"])
    if rc == 0 and out.strip():
        state, reason, start_text, elapsed_text, limit_text = (
            out.splitlines()[0].split("|", 4)
        )
        state = state.strip()
        if state == "PENDING":
            start = parse_slurm_start(start_text)
            if start is not None:
                remaining = int((start - dt.datetime.now(start.tzinfo)).total_seconds())
                if remaining > 0 and remaining % 60:
                    remaining += 60 - (remaining % 60)
                return (
                    f"{state}, reason: {format_pending_reason(reason)}, "
                    f"{format_duration(remaining)} until start"
                )
            return f"{state}, reason: {format_pending_reason(reason)}"

        elapsed = parse_slurm_duration(elapsed_text)
        limit = parse_slurm_duration(limit_text)
        if state == "RUNNING" and elapsed is not None and limit is not None:
            return f"{state} {format_duration(elapsed)}/{format_duration(limit)}"
        return state or "unknown"

    rc, out, _ = capture_status_cmd(
        [
            "sacct",
            "-n",
            "-P",
            "-j",
            job_id,
            "--format=JobIDRaw,State,Elapsed,Timelimit",
        ]
    )
    if rc == 0 and out.strip():
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 4 or parts[0] != job_id:
                continue
            state, elapsed_text, limit_text = parts[1], parts[2], parts[3]
            elapsed = parse_slurm_duration(elapsed_text)
            limit = parse_slurm_duration(limit_text)
            if state == "RUNNING" and elapsed is not None and limit is not None:
                return f"{state} {format_duration(elapsed)}/{format_duration(limit)}"
            return state or "unknown"

    return "unknown"


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
        if args.target.isdigit():
            print(f"job id:      {args.target}")
            print(f"status:      {query_status_line(args.target)}")
            return 0
        die(f"no session found for '{args.target}'. Use --help for usage.")

    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        die(f"missing metadata: {meta_path}")

    meta = load_json(meta_path)
    run_log = pathlib.Path(meta["run_log"])
    tunnel_log = pathlib.Path(meta["tunnel_log"])
    job_id: Optional[str] = meta.get("job_id")

    print(f"session:     {meta['session_id']}")
    print(f"status:      {query_status_line(job_id)}")
    print(f"profile:     {meta['profile']}")
    print(f"run log:     {run_log}")
    print(f"tunnel log:  {tunnel_log}")
    print(f"config:      {meta['config_path']}")
    print(f"job id:      {job_id or '-'}")

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
