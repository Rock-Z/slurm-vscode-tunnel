#!/usr/bin/env python3
import argparse
import pathlib
import shlex
import shutil
import subprocess
import tomllib
from typing import Any, Dict, List

from codeserver_lib import (
    ConfigError,
    default_config_path,
    die,
    dump_json,
    ensure_root_dirs,
    get_profile,
    load_config,
    profile_names,
    session_id_for,
)


def build_sbatch_cmd(
    profile: Dict[str, Any], run_log: pathlib.Path, batch_script: pathlib.Path
) -> List[str]:
    # User-owned sbatch args go through untouched.
    # Framework-owned logging flags are appended last so they win.
    return (
        ["sbatch", "--parsable"]
        + list(profile["sbatch_args"])
        + [f"--output={run_log}", f"--error={run_log}", str(batch_script)]
    )


def write_batch_script(
    batch_script: pathlib.Path,
    python_bin: str,
    inner_py: pathlib.Path,
    config_path: pathlib.Path,
    profile_name: str,
    session_dir: pathlib.Path,
    run_log: pathlib.Path,
    tunnel_log: pathlib.Path,
    pre_commands: List[str],
) -> None:
    preamble = ""
    if pre_commands:
        preamble = "\n".join(pre_commands) + "\n"

    body = f"""#!/usr/bin/env bash
set -euo pipefail

echo "[batch] host=$(hostname)"
echo "[batch] start=$(date --iso-8601=seconds)"
echo "[batch] profile={shlex.quote(profile_name)}"
echo "[batch] session_dir={shlex.quote(str(session_dir))}"
echo "[batch] run_log={shlex.quote(str(run_log))}"
echo "[batch] tunnel_log={shlex.quote(str(tunnel_log))}"

{preamble}exec {shlex.quote(python_bin)} {shlex.quote(str(inner_py))} \\
  --config {shlex.quote(str(config_path))} \\
  --profile {shlex.quote(profile_name)} \\
  --session-dir {shlex.quote(str(session_dir))} \\
  --run-log {shlex.quote(str(run_log))} \\
  --tunnel-log {shlex.quote(str(tunnel_log))}
"""
    batch_script.write_text(body, encoding="utf-8")
    batch_script.chmod(0o755)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Submit a new code tunnel session through Slurm.",
    )
    ap.add_argument(
        "profile",
        nargs="?",
        default=None,
        help="Profile name from codeserver.toml. Defaults to default_profile.",
    )
    ap.add_argument(
        "--config",
        default=str(default_config_path()),
        help="Path to the TOML config file.",
    )
    args = ap.parse_args()

    config_path = pathlib.Path(args.config).resolve()
    try:
        cfg = load_config(config_path)
    except (ConfigError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        die(f"{exc}. Use --help for usage.", code=2)
    root_dir = ensure_root_dirs(cfg)

    profile_name = args.profile or cfg["default_profile"]
    try:
        profile = get_profile(cfg, profile_name)
    except ConfigError as exc:
        names = ", ".join(profile_names(cfg))
        die(f"{exc}. available profiles: {names}. Use --help for usage.", code=2)

    session_id = session_id_for(profile_name)
    session_dir = root_dir / "logs" / session_id
    state_dir = root_dir / "state"
    run_log = session_dir / "run.log"
    tunnel_log = session_dir / "tunnel.log"
    meta_json = session_dir / "meta.json"
    batch_script = session_dir / "batch.sh"

    session_dir.mkdir(parents=True, exist_ok=True)

    python_bin = shutil.which("python3") or "python3"
    inner_py = pathlib.Path(__file__).resolve().parent / "codeserver_inner.py"

    write_batch_script(
        batch_script=batch_script,
        python_bin=python_bin,
        inner_py=inner_py,
        config_path=config_path,
        profile_name=profile_name,
        session_dir=session_dir,
        run_log=run_log,
        tunnel_log=tunnel_log,
        pre_commands=profile["pre_commands"],
    )

    sbatch_cmd = build_sbatch_cmd(profile, run_log, batch_script)

    meta = {
        "session_id": session_id,
        "profile": profile_name,
        "config_path": str(config_path),
        "session_dir": str(session_dir),
        "run_log": str(run_log),
        "tunnel_log": str(tunnel_log),
        "batch_script": str(batch_script),
        "sbatch_cmd": sbatch_cmd,
    }
    dump_json(meta_json, meta)

    proc = subprocess.run(
        sbatch_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        die(f"sbatch failed:\n{proc.stderr.strip() or proc.stdout.strip()}")

    raw_job = proc.stdout.strip()
    job_id = raw_job.split(";", 1)[0]
    meta["job_id"] = job_id
    dump_json(meta_json, meta)

    current_link = state_dir / "current"
    profile_link = state_dir / f"current-{profile_name}"
    relative_target = pathlib.Path("..") / "logs" / session_id

    for link in (current_link, profile_link):
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(relative_target)

    here = pathlib.Path(__file__).resolve().parent
    status_py = here / "codeserver_status.py"

    print(f"started session: {session_id}")
    print(f"profile:         {profile_name}")
    print(f"job id:          {job_id}")
    print(f"run log:         {run_log}")
    print(f"tunnel log:      {tunnel_log}")
    print()
    print("status:")
    print(f"  python3 {status_py}")
    print(f"  python3 {status_py} {profile_name}")
    print(f"  python3 {status_py} {session_id}")
    print("stop:")
    print(f"  python3 {here / 'codeserver_stop.py'} {session_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
