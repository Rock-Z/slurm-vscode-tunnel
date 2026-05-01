Code Server Slurm Helpers
=========================

This folder contains helper scripts for running `code tunnel` inside a Slurm
allocation and inspecting or stopping that session from the login node.

Layout
------

- `codeserver_submit.py`: submit a new VS Code tunnel job through Slurm.
- `codeserver_inner.py`: run inside the Slurm job and execute `code tunnel`.
- `codeserver_status.py`: show Slurm status, session metadata, auth prompts,
  and recent logs.
- `codeserver_stop.py`: cancel a running session by Slurm job id.
- `codeserver_lib.py`: shared config, session, log, and Slurm helpers.
- `codeserver-proxy`: proxy stdin/stdout to SSH port 22 on the node running a
  matching Slurm job.
- `codeserver.toml`: profiles and runtime configuration.

Runtime files are written under `runs/` by default. That directory contains
generated logs, session metadata, and `state/current*` symlinks.

Requirements
------------

- Python 3.11 or newer.
- Slurm commands available on the login node: `sbatch`, `squeue`, `sacct`,
  and `scancel`.
- VS Code CLI available as `code` on the compute node.
- For `codeserver-proxy`, one of `nc`, `socat`, or an SSH setup that supports
  `ssh -W`.

Profiles
--------

Profiles live in `codeserver.toml`.

- `cpu`: default profile, submits to the `day` partition with 8 CPUs, 32 GB RAM,
  and a 24 hour limit.
- `gpu`: submits to the `gpu_devel` partition with 1 GPU, 4 CPUs, 32 GB RAM,
  and a 6 hour limit.

Edit `codeserver.toml` if partitions, resource limits, or environment variables
need to change.

Usage
-----

Submit the default CPU tunnel:

```bash
codeserver_submit.py
```

Submit a GPU tunnel:

```bash
codeserver_submit.py gpu
```

Check the most recent session:

```bash
codeserver_status.py
```

Check a specific profile or session id:

```bash
codeserver_status.py cpu
codeserver_status.py gpu
codeserver_status.py 20260501-120000-cpu
```

Stop the latest session:

```bash
codeserver_stop.py
```

Stop a specific profile or session id:

```bash
codeserver_stop.py gpu
codeserver_stop.py 20260501-120000-gpu
```

Proxy to the SSH port of the node running a matching Slurm job:

```bash
codeserver-proxy codeserver-cpu
codeserver-proxy codeserver-gpu
```

Authentication
--------------

The first run may require VS Code tunnel authentication. Run
`codeserver_status.py` after submitting a job. If the logs contain a device-login
prompt, the status command prints the relevant block and reports
`NEEDS_REAUTH=yes`.

Command Setup
-------------

The scripts are executable. To run them as commands from any directory, make
sure this folder is on `PATH`:

```bash
export PATH="$HOME/Apps/codeserver:$PATH"
```

The current shell setup in `~/.bashrc` includes that path.
