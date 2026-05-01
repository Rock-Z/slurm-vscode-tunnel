#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import shutil
import sys
import tomllib
from typing import Any, Dict, List, Optional

import codeserver_status
import codeserver_stop
import codeserver_submit
from codeserver_lib import (
    ConfigError,
    default_config_path,
    die,
    expand_path,
    load_config,
    load_json,
    profile_names,
    query_job_status,
    run_capture,
)
from codeserver_relay import format_duration


TOP_HELP = """usage: cs [command] [options] [target]

Manage VS Code tunnel sessions on Slurm.

commands:
  submit, s [profile]        Submit a new code tunnel session.
  status, stat, i [target]   Show status for latest, profile, session id, chain id, or job id.
  list, l                    List known sessions/chains and Slurm state.
  stop, x [target]           Cancel a session or relay chain.
  proxy, p [profile|job]     Proxy stdin/stdout to SSH on the session node.
  profiles                   Show configured profiles.
  config                     Show resolved config path and key settings.

short action flags:
  -s [profile]               Alias for: submit [profile]
  -i [target]                Alias for: status [target]
  -l                         Alias for: list
  -x [target]                Alias for: stop [target]
  -p [profile|job]           Alias for: proxy [profile|job]

examples:
  cs submit                  Submit the default profile.
  cs -s cpu --time 72h       Submit a long CPU relay chain if needed.
  cs status gpu              Show current GPU tunnel status.
  cs list --expand           List relay chain segments.
  cs stop cpu                Cancel current CPU tunnel session/chain.
"""

SHORT_ACTIONS = {"-s": "submit", "-i": "status", "-l": "list", "-x": "stop", "-p": "proxy"}
ACTIVE_STATES = {"BOOT_FAIL", "CONFIGURING", "COMPLETING", "PENDING", "PREEMPTED", "RUNNING", "RESIZING", "REQUEUED", "REQUEUE_FED", "REQUEUE_HOLD", "REQUEUE_HOLD_FED", "SIGNALING", "SPECIAL_EXIT", "STAGE_OUT", "STOPPED", "SUSPENDED"}


def normalize_argv(argv: List[str]) -> List[str]:
    if not argv or argv[0] in ("-h", "--help"):
        return argv
    if argv[0] in SHORT_ACTIONS:
        return [SHORT_ACTIONS[argv[0]]] + argv[1:]
    return argv


def add_config_arg(parser: argparse.ArgumentParser, *, default: Any = str(default_config_path())) -> None:
    parser.add_argument("-c", "--config", default=default, help="Path to the TOML config file.")


def load_cfg(path: str) -> Dict[str, Any]:
    try:
        return load_config(pathlib.Path(path).resolve())
    except (ConfigError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        die(f"{exc}. Use --help for usage.", code=2)


def call_legacy_main(module: Any, prog: str, args: List[str]) -> int:
    old_argv = sys.argv
    try:
        sys.argv = [prog] + args
        return int(module.main())
    finally:
        sys.argv = old_argv


def status_from_text(status: Optional[str]) -> str:
    if not status:
        return "unknown"
    for field in status.split():
        if field.startswith("state="):
            return field.removeprefix("state=")
    return "unknown"


def is_active_state(state: str) -> bool:
    return state.upper() in ACTIVE_STATES


def chain_state(chain: Dict[str, Any]) -> str:
    states: List[str] = []
    for job in chain.get("jobs", []):
        job_id = str(job.get("job_id") or "")
        status = query_job_status(job_id) if job_id and not job_id.startswith("DRY-RUN") else None
        states.append(status_from_text(status))
    if any(state == "RUNNING" for state in states):
        return "RUNNING"
    if any(state == "PENDING" for state in states):
        return "PENDING"
    return states[0] if states else "unknown"


def session_rows(cfg: Dict[str, Any], expand: bool = False) -> List[Dict[str, str]]:
    root = expand_path(cfg["root_dir"])
    rows: List[Dict[str, str]] = []
    logs_dir = root / "logs"
    if not logs_dir.exists():
        return rows

    for chain_path in sorted(logs_dir.glob("*/chain.json"), reverse=True):
        try:
            chain = load_json(chain_path)
        except (OSError, json.JSONDecodeError):
            continue
        state = chain_state(chain)
        rows.append({"session": str(chain.get("chain_id") or chain_path.parent.name), "profile": str(chain.get("profile") or "-"), "job": f"{len(chain.get('jobs', []))} jobs", "state": state, "dir": str(chain_path.parent)})
        if expand:
            for job in chain.get("jobs", []):
                job_id = str(job.get("job_id") or "")
                status = query_job_status(job_id) if job_id and not job_id.startswith("DRY-RUN") else None
                rows.append({"session": f"  job-{int(job['index']):03d}", "profile": str(chain.get("profile") or "-"), "job": job_id or "-", "state": status_from_text(status), "dir": str(job.get("session_dir") or "-")})

    chain_dirs = {p.parent.resolve() for p in logs_dir.glob("*/chain.json")}
    for meta_path in sorted(logs_dir.glob("*/meta.json"), reverse=True):
        if meta_path.parent.resolve() in chain_dirs:
            continue
        try:
            meta = load_json(meta_path)
        except (OSError, json.JSONDecodeError):
            continue
        job_id = str(meta.get("job_id") or "")
        status = query_job_status(job_id) if job_id and not job_id.startswith("DRY-RUN") else None
        rows.append({"session": str(meta.get("session_id") or meta_path.parent.name), "profile": str(meta.get("profile") or "-"), "job": job_id or "-", "state": status_from_text(status), "dir": str(meta_path.parent)})
    return rows


def print_table(rows: List[Dict[str, str]]) -> None:
    headers = ["SESSION", "PROFILE", "JOB", "STATE", "DIR"]
    data = [[row["session"], row["profile"], row["job"], row["state"], row["dir"]] for row in rows]
    widths = [len(header) for header in headers]
    for row in data:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    print(fmt.format(*headers))
    for row in data:
        print(fmt.format(*row))


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    rows = session_rows(cfg, expand=args.expand)
    if args.active:
        rows = [row for row in rows if is_active_state(row["state"])]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("no sessions found")
        return 0
    print_table(rows)
    return 0


def job_name_for_profile(cfg: Dict[str, Any], profile_name: str) -> Optional[str]:
    profile = cfg["profiles"].get(profile_name)
    if not profile:
        return None
    for arg in profile.get("sbatch_args", []):
        if arg.startswith("--job-name="):
            return arg.split("=", 1)[1]
    return None


def resolve_proxy_job_name(cfg: Dict[str, Any], target: Optional[str]) -> str:
    name = target or cfg["default_profile"]
    return job_name_for_profile(cfg, name) or name


def cmd_proxy(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    job_name = resolve_proxy_job_name(cfg, args.target)
    for cmd in ("squeue", "scontrol"):
        if shutil.which(cmd) is None:
            die(f"{cmd} not found on this host", code=127)
    user = os.environ.get("USER", "")
    rc, out, err = run_capture(["squeue", "-h", "-u", user, "-n", job_name, "-t", "RUNNING", "-o", "%N"])
    if rc != 0:
        die(f"squeue failed:\n{err.strip() or out.strip()}", code=127)
    nodelist = out.splitlines()[0].strip() if out.strip() else ""
    if not nodelist:
        die(f"no running allocation found for job name: {job_name}", code=127)
    rc, out, err = run_capture(["scontrol", "show", "hostnames", nodelist])
    if rc != 0:
        die(f"scontrol failed:\n{err.strip() or out.strip()}", code=127)
    node = out.splitlines()[0].strip() if out.strip() else ""
    if not node:
        die(f"failed to resolve hostname from NodeList: {nodelist}", code=127)
    if shutil.which("nc"):
        os.execvp("nc", ["nc", node, "22"])
    if shutil.which("socat"):
        os.execvp("socat", ["socat", "-", f"TCP:{node}:22"])
    os.execvp("ssh", ["ssh", "-o", "BatchMode=yes", "-W", f"{node}:22", "localhost"])
    return 127


def cmd_profiles(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    print(f"default: {cfg['default_profile']}")
    for name in profile_names(cfg):
        profile = cfg["profiles"][name]
        enabled = "enabled" if profile["enabled"] else "disabled"
        job_name = job_name_for_profile(cfg, name) or "-"
        max_time = profile.get("max_time") or "sbatch/default"
        print(f"{name}: {enabled}, job={job_name}, max_time={max_time}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    root = expand_path(cfg["root_dir"])
    print(f"config:          {cfg['config_path']}")
    print(f"root dir:        {root}")
    print(f"default profile: {cfg['default_profile']}")
    print(f"code command:    {' '.join([cfg['code_bin']] + cfg['code_tunnel_args'])}")
    print(f"profiles:        {', '.join(profile_names(cfg))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cs", add_help=False, formatter_class=argparse.RawDescriptionHelpFormatter, description=TOP_HELP)
    parser.add_argument("-h", "--help", action="store_true", help=argparse.SUPPRESS)
    add_config_arg(parser)
    subparsers = parser.add_subparsers(dest="command")

    submit = subparsers.add_parser("submit", aliases=["s"], help="Submit a new tunnel session.")
    add_config_arg(submit, default=argparse.SUPPRESS)
    submit.add_argument("profile", nargs="?", help="Profile from codeserver.toml.")
    submit.add_argument("--dry-run", action="store_true", help="Write session files and show sbatch commands without submitting.")
    submit.add_argument("--time", help="Requested walltime, e.g. 72:00:00, 72h, 3d.")
    submit.add_argument("--relay-overlap", help="How long before expiry to start the next relay job.")
    submit.add_argument("--no-relay", action="store_true", help="Fail instead of splitting long requests.")
    submit.add_argument("--test-command", help="Developer/test command to run instead of code tunnel.")

    status = subparsers.add_parser("status", aliases=["stat", "i"], help="Show session status.")
    add_config_arg(status, default=argparse.SUPPRESS)
    status.add_argument("target", nargs="?", help="latest, profile name, session id, chain id, or job id.")

    stop = subparsers.add_parser("stop", aliases=["x"], help="Cancel a session or relay chain.")
    add_config_arg(stop, default=argparse.SUPPRESS)
    stop.add_argument("target", nargs="?", help="latest, profile name, session id, chain id, or job id.")

    list_parser = subparsers.add_parser("list", aliases=["l"], help="List known sessions.")
    add_config_arg(list_parser, default=argparse.SUPPRESS)
    list_parser.add_argument("-a", "--active", action="store_true", help="Show only active sessions.")
    list_parser.add_argument("--all", action="store_true", help="Include historical sessions.")
    list_parser.add_argument("--expand", action="store_true", help="Show relay chain segments.")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    proxy = subparsers.add_parser("proxy", aliases=["p"], help="Proxy SSH to the session node.")
    add_config_arg(proxy, default=argparse.SUPPRESS)
    proxy.add_argument("target", nargs="?", help="Profile name or explicit Slurm job name.")

    profiles = subparsers.add_parser("profiles", help="Show configured profiles.")
    add_config_arg(profiles, default=argparse.SUPPRESS)
    config = subparsers.add_parser("config", help="Show resolved config settings.")
    add_config_arg(config, default=argparse.SUPPRESS)
    return parser


def legacy_args(args: argparse.Namespace, value: Optional[str]) -> List[str]:
    out: List[str] = []
    if value:
        out.append(value)
    for name in ("dry_run", "no_relay"):
        if getattr(args, name, False):
            out.append("--" + name.replace("_", "-"))
    for name in ("time", "relay_overlap", "test_command"):
        value = getattr(args, name, None)
        if value:
            out.extend(["--" + name.replace("_", "-"), value])
    out.extend(["--config", args.config])
    return out


def main() -> int:
    argv = normalize_argv(sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.help or args.command is None:
        print(TOP_HELP)
        return 0
    command = args.command
    if command in ("submit", "s"):
        return call_legacy_main(codeserver_submit, "cs submit", legacy_args(args, args.profile))
    if command in ("status", "stat", "i"):
        return call_legacy_main(codeserver_status, "cs status", [*( [args.target] if args.target else [] ), "--config", args.config])
    if command in ("stop", "x"):
        return call_legacy_main(codeserver_stop, "cs stop", [*( [args.target] if args.target else [] ), "--config", args.config])
    if command in ("list", "l"):
        return cmd_list(args)
    if command in ("proxy", "p"):
        return cmd_proxy(args)
    if command == "profiles":
        return cmd_profiles(args)
    if command == "config":
        return cmd_config(args)
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
