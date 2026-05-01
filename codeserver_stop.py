#!/usr/bin/env python3
import argparse
import pathlib
import subprocess
import tomllib

from codeserver_lib import ConfigError, default_config_path, die, load_config, load_json, resolve_session_dir


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stop a session by resolving latest, a profile, or a session id to a Slurm job.",
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

    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        die(f"missing metadata: {meta_path}")

    meta = load_json(meta_path)
    job_id = meta.get("job_id")
    if not job_id:
        die(f"session '{meta.get('session_id', args.target)}' has no job_id to stop")

    proc = subprocess.run(
        ["scancel", str(job_id)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        die(f"scancel failed:\n{proc.stderr.strip() or proc.stdout.strip()}")

    print(f"stopped session: {meta.get('session_id', args.target)}")
    print(f"profile:         {meta.get('profile', '-')}")
    print(f"job id:          {job_id}")
    print(f"session dir:     {session_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
