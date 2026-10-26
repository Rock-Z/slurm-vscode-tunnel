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
  and a 24 hour limit. CPU relay chains make each successor eligible every
  20 hours, providing a nominal 4 hour overlap for queue delays.
- `gpu`: submits to the `gpu_devel` partition with 1 GPU, 4 CPUs, 32 GB RAM,
  and a 6 hour limit.
- `relay_test`: short `day` partition profile for live relay validation. It uses
  1 CPU, 1 GB RAM, a 2 minute max time, and a 1 minute relay overlap.

The default `cpu` profile also starts the configured `paseo` sidecar. Sidecars
run in their own process groups, write separate logs in the session directory,
must pass their optional readiness check, and are stopped when the tunnel exits.
Sidecar commands must stay in the foreground. `paseo_runtime.py run` stages the
packaged application and Node binary into a private job-local `/tmp` directory,
then executes `paseo daemon run --home "$HOME/.paseo-slurm"` there. Persistent
settings, agent state, credentials, and workspaces retain their existing paths.
`paseo_ready.py` checks the supervisor PID, hostname and Slurm cgroup, then the
loopback health endpoint; it does not load another copy of the Node CLI. During relay rollover, profiles
with sidecars cancel the previous job and wait for it to exit before starting
the new services. This prevents daemons from overlapping on shared ports or
state directories, at the cost of a brief reconnect. Profiles without sidecars
retain the overlapping tunnel handoff.

The CPU profile also starts `paseo_diagnostics` before Paseo. It samples the
actual daemon every 0.5 seconds (CPU ticks, scheduler delay, RSS, kernel wait,
current syscall and blocked I/O file descriptor) and checks loopback HTTP health
once per second with a two-second timeout. A Node preload hook, active only in
`Paseo Daemon`, logs synchronous filesystem and process-launch calls taking at
least 100 ms, with duration and JavaScript stack. It also records timer gaps.
This diagnoses stalls; it does not fix them or trace asynchronous Git children.

Diagnostics spool privately under `/tmp/paseo-diagnostics-$UID-$SLURM_JOB_ID`.
A separate thread exports new records every ten seconds to
`<session_dir>/paseo-diagnostics/events.jsonl`; existing `paseo.log` supplies
request timings and relay events for correlation. Samples and exported events
are each capped at 64 MiB, and slow-operation logs at approximately 6 MiB.
Logs contain paths and stack locations, never file contents or command arguments.
Recent node-local records can be lost on abrupt node/job termination. A GPFS
export failure is recorded locally. Shutdown stops Paseo before the sampler so
it can export the final records. The next CPU job loads the diagnostics; no
running daemon is automatically restarted. Remove `paseo_diagnostics` from the
profile and unset `PASEO_DIAGNOSTIC_DIR` in Paseo's command to disable them.

The configured `python_bin = "/usr/bin/python3"` also keeps the job bootstrap
on the node-local Python installation instead of inheriting a GPFS module
interpreter from the submitting shell.

App storage exports have one source: the self-contained block between exact
`# app-storage:begin` and `# app-storage:end` lines in `~/.bashrc`.
`with_app_paths.sh COMMAND [ARG ...]` evaluates only that trusted block, then
executes the supplied command. Missing or malformed markers abort the launch.
It never sources the rest of the interactive configuration.

Coding agent providers need no command override: the Paseo daemon already runs
under the wrapper, so every agent it launches inherits the storage exports.
Edits to the block reach agents after the next daemon restart. Do not route
`agents.providers.codex.command` through a wrapper. Paseo probes only
`command[0] --version` with a 5 s timeout and caches a failure, and a slow
wrapper chain then hides Auto-review until the daemon restarts.

New batch scripts invoke the wrapper before `codeserver_inner.py`; older batch
scripts and direct launches enter it through the inner script once. Tunnel,
sidecars, readiness checks and descendants inherit the resulting exports.
The Paseo runtime then prepends its job-local `/tmp` tool directory to `PATH`.
There is no separate hardcoded Paseo PATH. Node and npm installation roots are
resolved before packaging and mounting, so the `~/Apps` and `~/.local/node24`
compatibility symlinks can point to relocated application storage without
putting unsafe absolute root links in the archive. Internal package links still
undergo the existing safe extraction checks.

Normal `run` and `stage` launches check the persistent installation and rebuild
the runtime archive automatically inside the new compute allocation. Ordinary
`npm install -g` updates outside the service namespace need no special wrapper.
To force a rebuild (for example, after editing a nested plugin source file):

```bash
/usr/bin/python3 ~/Apps/codeserver/paseo_runtime.py build
```

The build includes the Node installation, the global npm installation (including
Paseo and Codex), enabled plugin directories with their dependencies, installed
Claude/uv tools found on PATH, and the diagnostic hook. It rejects external package symlinks and publishes a
SHA-256-named archive plus `runs/paseo-runtime/current.json` atomically. Each job
copies the archive sequentially, verifies its checksum, and unpacks into
`/tmp/paseo-runtime-$UID-$SLURM_JOB_ID-<hash>`. Retries in the same job reuse that
copy. A shared build lock serializes concurrent startups and rechecks freshness
after waiting, so they publish one complete archive. Failed builds and detected
source changes during packaging/publication/staging abort the launch instead of
using an older version. Archives without freshness metadata rebuild once.
The Paseo sidecar allows 15 minutes for rebuilding, staging and daemon readiness;
the relay timeout separately bounds retirement of the previous allocation.
Application updates in a temporary runtime are not persistent: update the
shared installation outside the service namespace for future jobs. The generated
archives are ignored by Git. Startup logs record the version, hash, and staging
time. The temporary runtime contains no agent state and can be discarded after
the job; older archives remain available for reproducibility.

The runtime uses `bwrap` private bind mounts: each packaged dependency appears at
its original absolute path inside Paseo and its children, backed by the job-local
copy. Persistent Paseo home, settings, credentials, agent histories, projects,
and session logs keep their original GPFS paths; only the configured plugin
source subdirectories and installed tools are overlaid. Outside that process
tree, the shared installations are unchanged. The launcher refuses automatic or
manual builds from an overlaid namespace. In-app terminals in such a daemon
can see job-local dependencies at the usual global paths: `npm install -g` there
may mutate the running job-local copy, without updating the persistent installation.
Use an SSH shell outside that process tree for persistent updates. Slurm batch
bootstrap starts outside the old daemon namespace, even when submitted from it.

Freshness checks inspect shallow metadata (inode, ctime, mtime, size and mode) for
installation roots, npm package roots including scopes, package manifests, Node,
resolved command executables, enabled plugin roots and their immediate entries,
and the diagnostic hook. They detect ordinary npm updates and same-version
reinstalls without walking recursive GPFS dependency trees on every launch.
Arbitrary edits to deeply nested package/plugin files can escape these checks;
force `build` after those edits. Package updaters do not share the build lock:
before/after checks reject detected concurrent changes, but cannot guarantee a
transactional snapshot during an update or eliminate a change after the final
check. Finish updates before launching jobs. Already-staged directories are
never refreshed by archive publication. Candidate archives remain explicitly
pinned and bypass the automatic freshness check.

For rollout validation, `build --candidate` writes `candidate.json` without
changing `current.json`, and `run --candidate` uses it. In a compute allocation,
`python3 -m unittest discover -s tests -p test_paseo_runtime.py -v` checks that
all dependency mounts use the local device, persistent writes are visible outside
the namespace, and both configured plugins reach `running`. Successful evidence
is written to `runs/paseo-runtime/candidate-validation.json` before activation.

The September 30 production run showed that `q5m-math` compilation still
synchronously read MathJax dependencies from GPFS through `checkSourceImports`
and `createPluginImportReader.read`. Plugin startup precedes relay creation;
the application-only bundle did not cover plugin dependencies. The expanded
bundle mounts those dependency trees locally. Historical observations remain in
`runs/paseo-local-benchmark-20260930/production.json`.

Sidecars can be attached to any profile in `codeserver.toml`:

```toml
[sidecars.example]
command = '''exec ~/bin/my-service'''
log = "example.log"
ready_command = '''~/bin/my-service status'''
ready_capture_command = '''~/bin/my-service connection-info --json'''
ready_output = "example-connection.json"
ready_timeout = "2m"

[profiles.cpu]
sidecars = ["example"]
```

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
For profiles without sidecars, a new segment waits for tunnel readiness before
canceling the previous segment. Production readiness comes from `code tunnel
status`; the deterministic test path uses `READY`. If readiness is not detected
within `relay_ready_timeout`, the previous job is left alive.

Profiles with sidecars instead retire the previous allocation first, as described
above, then start and check the sidecars before launching the tunnel. A startup
failure therefore leaves a gap until another segment starts; the nominal overlap
is queue-delay headroom, not a fallback after retirement.

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


### Deploying local Paseo plugins

Keep development checkouts in their existing Paseo projects. Run `npm run deploy`
on the daemon's Slurm node. The **deploy** workspace script runs it for
claude-progress and autoresearch; **deploy-slurm-gpus** runs from the ops project.
The math checkout also has the script but is not a separate registered project.

`paseo_plugin_deploy.py` snapshots current tracked and nonignored untracked files,
including uncommitted edits; installs locked dependencies locally; and runs the
normal build and typecheck. npm's `files` list supplies the package contents.
Production dependencies and generated assets are stored in one immutable archive
under `~/Apps/data/paseo-plugin-releases/<id>/<sha256>.tar.gz`. Publishing one file
avoids GPFS metadata traffic from thousands of dependency files.

The daemon loads the extracted code at `/tmp/paseo-plugin-releases-<uid>/<id>/<sha256>`.
`paseo_runtime.py` recreates missing local copies from the persistent archives
before every daemon start. Extraction verifies the checksum, uses a private cache
and lock, and publishes atomically. Changing the temporary cache or losing a node
does not change the release archive, development checkout, or persistent history.
Plugin updates do not require rebuilding the main Node/Paseo runtime archive.

Deployment updates that plugin through Paseo's config API and reloads it; failure
restores its previous source. It retains settings, projects, and old archives for
rollback and agents already using old CLI assets. No daemon restart is performed.
Editing the checkout or reloading alone does not update a deployed snapshot.
Commands inside a Paseo terminal re-enter the same compute node through SSH first
so publishing cannot accidentally write into inherited runtime overlays.

To prepare without activation:

```bash
python3 ~/Apps/codeserver/paseo_plugin_deploy.py --source /path/to/plugin --prepare-only
```

Rollback uses `paseo_plugin_activate.mjs` with the installed CLI package path,
daemon home, plugin ID, and a previously extracted release path. The archive must
remain available while that release is referenced. The helper requires a compute
allocation and a synchronized `package-lock.json`; it never patches Paseo or
copies mutable histories into `/tmp`.
