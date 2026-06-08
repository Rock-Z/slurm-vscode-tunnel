Slurm VS Code Tunnel
=====================

`slurm-vscode-tunnel` is a small Python CLI for running VS Code `code tunnel` inside Slurm allocations. It submits CPU or GPU tunnel jobs, persists session metadata and logs, detects device-login prompts, reports Slurm state, cancels sessions, and can proxy SSH to the allocated compute node.

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

- `cpu`: default profile, submits to the `day` partition with 4 CPUs, 32 GB RAM,
  and a 24 hour limit.
- `gpu`: submits to the `gpu_devel` partition with 1 GPU, 4 CPUs, 32 GB RAM,
  and a 6 hour limit.
- `relay_test`: short `day` partition profile for live relay validation. It uses
  1 CPU, 1 GB RAM, a 2 minute max time, and a 1 minute relay overlap.

Edit `codeserver.toml` if partitions, resource limits, or environment variables
need to change.

Usage
-----

`cs` is the preferred interface:

```text
usage: cs [command] [options] [target]

Manage VS Code tunnel sessions on Slurm.

commands:
  submit, s [profile]        Submit a new code tunnel session.
  status, stat, i [target]   Show status for latest, profile, session id, chain id, or job id.
  list, l                    List known sessions/chains and Slurm state.
  stop, x [target]           Cancel a session or relay chain.
  extend [target] DURATION   Add time to a running/pending Slurm job.
  continue, c [target]       Continue/connect to a running session node.
  proxy, p [target]          Proxy stdin/stdout to SSH on the session node.
  profiles                   Show configured profiles.
  config                     Show resolved config path and key settings.
  completion [bash|zsh]      Print shell completion setup.

global options:
  -h, --help                 Show this help message and exit.
  -c, --config PATH          Use a custom codeserver.toml.

short action flags:
  -s [profile]               Alias for: submit [profile]
  -i [target]                Alias for: status [target]
  -l                         Alias for: list
  -x [target]                Alias for: stop [target]
  -p [target]                Alias for: proxy [target]
```

Submit the default CPU tunnel:

```bash
cs submit
cs s
```

Submit a GPU tunnel:

```bash
cs submit gpu
cs -s gpu
```

Submit a long session. If requested time exceeds the profile limit, `cs` splits
it into a relay chain and prints the exact segment plan before submitting:

```bash
cs submit cpu --time 72h
cs s cpu --time 72:00:00
cs s cpu --time 72h --relay-overlap 30m
```

Disable relay splitting and fail instead if the request is too long:

```bash
cs s cpu --time 72h --no-relay
```

Check the most recent session:

```bash
cs status
cs -i
```

Check a specific profile, session id, or Slurm job id:

```bash
cs status cpu
cs status gpu
cs status 20260501-120000-cpu
cs status 1234567
```

List known sessions and relay chains:

```bash
cs list
cs list --active
cs list --expand
cs list --json
```

Stop the latest session:

```bash
cs stop
cs -x
```

Stop a specific profile, session id, or Slurm job id:

```bash
cs stop gpu
cs stop 20260501-120000-gpu
cs stop 1234567
```

Extend a pending or running Slurm job by adding time to its current time limit:

```bash
cs extend 10h
cs extend cpu 10h
cs extend gpu 90m
cs extend 20260501-120000-cpu 02:00:00
cs extend 1234567 1-00:00:00
```

`extend` resolves targets the same way as `continue`: default profile, profile
name, session id, Slurm job id, or explicit job name. It computes a new absolute
Slurm `TimeLimit` and runs `scontrol update`. Slurm may reject the update if the
new limit exceeds partition, account, or QOS policy.

Continue/connect to the SSH port of the node running an existing tunnel session:

```bash
cs continue
cs continue cpu
cs continue 20260501-120000-cpu
cs continue 1234567
cs c gpu
```

Proxy to the SSH port of the node running a matching Slurm job:

```bash
cs proxy cpu
cs proxy gpu
cs proxy codeserver-cpu
```

If a profile or job name matches multiple running Slurm jobs, `continue` and
`proxy` warn and use only the first node listed by Slurm. Use a specific Slurm
job id or session id to avoid ambiguity.

Show configuration:

```bash
cs profiles
cs config
```

The older `codeserver_submit.py`, `codeserver_status.py`, `codeserver_stop.py`,
and `codeserver-proxy` commands are kept as compatibility wrappers/tools.

Shell completion candidates come from configured profiles plus known session
metadata:

```bash
cs completion bash
cs completion zsh
```

Relay Behavior
--------------

Profiles define `max_time`, `default_time`, `relay_overlap`,
`relay_ready_timeout`, and `relay_enabled`. `cs submit --time ...` compares the
requested duration with the profile max. Requests within the max submit one job.
Longer requests create a relay chain under `runs/logs/<chain-id>/` with one
subdirectory per segment and a top-level `chain.json`.

Future segments are submitted immediately with Slurm `--begin=now+...` offsets.
When a new segment starts, it waits for tunnel readiness before canceling the
previous segment. Readiness is detected from tunnel output; the deterministic
test path uses `READY`. If readiness is not detected within
`relay_ready_timeout`, the previous job is left alive.

Live relay test pattern:

```bash
cs s relay_test --time 00:04:00 --test-command 'echo READY; sleep 600'
cs status relay_test
cs list --expand
cs stop relay_test
```

Authentication
--------------

The first run may require VS Code tunnel authentication. Run `cs status` after
submitting a job. If the logs contain a device-login prompt, the status command
prints the relevant block and reports `NEEDS_REAUTH=yes`.

Command Setup
-------------

The scripts are executable. To run them as commands from any directory, make
sure this folder is on `PATH`:

```bash
export PATH="$HOME/Apps/slurm-vscode-tunnel:$PATH"
```

The current shell setup in `~/.bashrc` includes that path.
