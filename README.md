<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="branding/logo/logo-dark.svg">
    <img src="branding/logo/logo-light.svg" alt="eessi-arch-guard" width="460">
  </picture>
</p>

# eessi-arch-guard

eessi-arch-guard is a Slurm TaskProlog that reloads an EESSI module stack when the task runs on a CPU or GPU other than the target recorded at `module load`. The EESSI module chooses that target once. Lmod keeps the recorded `MODULEPATH` and the `EBROOT*` paths until the stack is purged and loaded again. The prolog does that purge and load on the compute node, before `execve`, and prints the environment delta for `slurmstepd` to apply.

## When you need it, and when you do not

You do not need it if your jobs load EESSI themselves: `sbatch --export=NONE`, or a batch script that loads the module, runs archdetect on the compute node.
You need it where a task inherits a stack loaded on another node type: `srun` from a login shell that loaded EESSI, and `sbatch --export=ALL`.
In a batch script, `module reload` at the top is enough for the batch step and the `srun` steps it starts: on a zen4 node, a job from a zen2 login node with `EESSI/2026.06` and `GSL/2.8-GCC-15.2.0` loaded went from `x86_64/amd/zen2` to `x86_64/amd/zen4` on `module reload`, because the EESSI module runs archdetect again.
The guard is for what no script covers: `srun ./a.out` from a login shell or an `salloc` shell that loaded EESSI, and batch scripts whose authors do not know to reload.

Measured on a Slurm 26.05.5 cluster with EESSI 2026.06 and `GSL/2.8-GCC-15.2.0` loaded on a zen2 login node, run on a zen4 node ([the how-to](docs/orgmode/howto/use-as-a-user-task-prolog.org) has all four cases):

| How the task started | `EESSI_SOFTWARE_SUBDIR` in the task |
|---|---|
| `srun` on a zen4 partition, no guard | `x86_64/amd/zen2` |
| `srun` on a zen4 partition, guard as task prolog | `x86_64/amd/zen4` |

The guard runs once per task, so its own cost is per rank.
`tests/measure_noop.py`, 50 runs on one x86_64 workstation with the test stub for `eessi_archdetect.sh`, the case where nothing needs reloading; wall time per process, not a production-node measurement:

| | cold, archdetect each run | warm, cached for the job step |
|---|---|---|
| Python prolog (`src/eessi_arch_guard.py`) | 46.3 ms | 37.8 ms |
| Zig binary, ReleaseSafe | 3.5 ms | 0.17 ms |

Python's own start-up is 5.6 ms of that (`python3 -I -c pass`).
At those times, 192 ranks on a node start the Python prolog 192 times, about 7 s of process time per step, against about 0.03 s for the binary.

Documentation: <https://stackhands.github.io/eessi-arch-guard/>, built from the Org pages in `docs/orgmode/`: a tutorial, how-to guides, explanation, and a reference, including the API generated from the Python and Zig code.

```console
pixi run -e docs docbld
```

The HTML is written to `docs/build/html`. `docs/orgmode/contributing.org` says how to write a page.

Release archives include bash and zsh completion files plus a man page generated from `build_arg_parser` with pinned `shtab` and `argparse-manpage`. Fish output stays out because that toolchain does not emit fish completion scripts.

## Install the prolog

The prolog target is `src/eessi_arch_guard.py` (shebang `#!/usr/bin/python3 -I`). Python 3.9 or newer, standard library only.

```bash
sudo install -m 755 src/eessi_arch_guard.py /usr/local/sbin/eessi-arch-guard
```

```
TaskProlog=/usr/local/sbin/eessi-arch-guard
```

`pip install .` also installs a console script for `eessi-arch-guard --shell` on a login node. That generated script is started without `-I`. Point `TaskProlog` at the file. The worked install, the config file, and the failure messages are in `docs/`.

## Flags and environment

```
eessi-arch-guard [--shell] [--strict] [--dry-run] [--json] [--chain PATH] [--timeout SEC] [--syslog TARGET] [--restore] [-h|--help] [--version]
eessi-arch-guard --check-submit --partition NAME [--constraint NAME] [--eb-stack PATH] [--json]
```

`--shell` prints bash assignments. `--strict` returns status 78 for a failed decision or unexpected exception; successful reloads, no-ops and intentional skips return zero. The default TaskProlog failure policy is unchanged. `--dry-run` loads the configuration, validates `--chain`, inspects the cache read-only, probes CPU and accelerator targets, and prints the decision it would make for the current node and job environment without executing `--chain`, writing cache or state, sending syslog events, or changing the environment. `--check-submit` maps `--partition NAME` and optional `--constraint NAME` through the site configuration, then uses `--eb-stack PATH` (or configured `eb_stack`) to check `LOADEDMODULES` against the target module tree without changing it. With `--json`, that check prints one object with `schema_version` 1 instead of the text summary. `--json` prints one line of JSON with the decision summary on stdout instead of prolog or shell lines on a normal run; with `--dry-run`, it emits the same decision fields plus the diagnostic keys; with `--check-submit`, it emits the check document. `--chain PATH` runs an existing task prolog first. `--timeout SEC` bounds the decision. `--syslog TARGET` (or `EESSI_ARCH_GUARD_SYSLOG` / config `syslog`) emits structured RFC 5424 or native `systemd-journald` events (`journald`, `syslog`, `unix:///path`, `journal+unix:///path`, `udp://host:port`, `tcp://host:port`) with stable `MSGID` values (`EESSI_ARCH_RELOAD` on reload, `EESSI_ARCH_FAIL` on failure, and `EESSI_ARCH_SKIP` / `EESSI_ARCH_NOOP` when `EESSI_ARCH_GUARD_SYSLOG_ALL=1` or config `syslog_all = 1`). RFC 5424 messages use `[archGuard@<PEN> ...]` structured data (`DEFAULT_SYSLOG_PEN` / `32473`, the IANA RFC 5398 / RFC 5424 section 6.3.2 documentation placeholder until `EESSI_ARCH_GUARD_SYSLOG_PEN` or config `syslog_pen` sets a site or project PEN); native journald records emit `MESSAGE`, `PRIORITY`, `SYSLOG_IDENTIFIER`, `SYSLOG_FACILITY`, `MSGID`, `ARCH_GUARD_KIND`, `ARCH_GUARD_CACHED`, `ARCH_GUARD_STRICT`, `ARCH_GUARD_WAS`, `ARCH_GUARD_NOW`, `ARCH_GUARD_CAUSE`, `SLURM_JOB_ID`, `SLURM_STEP_ID`, and `SLURM_LOCALID`. `--restore` is the helper the prolog execs. `-h` and `--help` print the usage text. `--version` prints the version.

The prolog compares `EESSI_SOFTWARE_SUBDIR` and `EESSI_ACCEL_SUBDIR` with `eessi_archdetect.sh`, walking `<EESSI_PREFIX>/software/<EESSI_OS_TYPE>/<candidate>/software`. It may set `EESSI_SOFTWARE_SUBDIR_OVERRIDE` and drop a stale `EESSI_ARCHDETECT_OPTIONS`. Opt-out and pins: `EESSI_ARCH_GUARD=0`, `EESSI_ARCHDETECT_OPTIONS_OVERRIDE`, `EESSI_ACCELERATOR_TARGET_OVERRIDE`. Loud results set `EESSI_ARCH_GUARD_FAILED`. `EESSI_ARCH_GUARD_MISSING=ignore` restores silence when no software directory exists. `EESSI_ARCH_GUARD_VERBOSE=1` writes skip reasons to stderr; Slurm discards a TaskProlog's stderr, so read them by running the guard by hand inside a task. `EESSI_ARCH_GUARD_RELOADED` is `was->now`. The cache sits under `TMPDIR` if that directory exists, otherwise `/tmp`, and is opened with `O_NOFOLLOW`. The cache file and its `.lock` stay there after the job. The cache key includes `LOADEDMODULES`, `MODULEPATH`, `EESSI_INIT_PREFIX`, `EESSI_PROC_CPUINFO`, `PATH`, the task's allowed processors, and `CUDA_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES`, `GPU_DEVICE_ORDINAL`, `SLURM_STEP_GPUS`, and `SLURM_GPUS_ON_NODE` when those are set. `LMOD_CMD` is the module command the helper execs. `SLURM_CPU_BIND_LIST` and `SLURM_CPU_BIND` name the processors the task will run on, as a mask, a CPU list, or a `cpu[0-3]` hostlist; together with the cpuset and the affinity mask they limit the cpuinfo archdetect sees. `SLURM_JOB_ID`, `SLURM_STEP_ID`, and `SLURM_LOCALID` (local task 0) scope the cache and the `print` line. The helper process itself is `python3 -I -X utf8` with `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, and `EESSI_ARCH_GUARD_WORKDIR` in its environment.

Strict restores and caches use an existing private directory owned by the caller: `EESSI_ARCH_GUARD_WORKDIR`, then `TMPDIR`, then `XDG_STATE_HOME/eessi-arch-guard` (or `HOME/.local/state/eessi-arch-guard`). The base must exist and is never created or widened by the guard. Strict mode has no shared temporary-directory fallback.

Outside strict mode the cache falls back to `/tmp`, which other users can write. The guard trusts a cache or lock file only when it is a regular file owned by the caller, with one link and no group or other permission bits, checked on the open descriptor; a FIFO at the name does not block it. A cached entry that changes `LD_*`, `SLURM_*`, `BASH_FUNC_*` or any other denylisted name is ignored and the decision is made again.

The helper never emits `SLURM_*`, `BASH_FUNC_*`, `TMPDIR`, `BASH_ENV`, or shell bookkeeping from the task environment. The full denylist is `_`, `SHLVL`, `PWD`, `OLDPWD`, `TMPDIR`, `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`, `BASH_XTRACEFD`, `PS4`, `BASH_ARGC`, `BASH_ARGV`, `BASH_LINENO`, `BASH_SOURCE`, `BASH_VERSINFO`, `BASH_VERSION`, `DIRSTACK`, `EPOCHREALTIME`, `EPOCHSECONDS`, `FUNCNAME`, `GROUPS`, `HOSTNAME`, `HOSTTYPE`, `IFS`, `MACHTYPE`, `OPTERR`, `OPTIND`, `OSTYPE`, `PIPESTATUS`, `PPID`, plus any `BASH_FUNC_*` or `SLURM_*` name. `PS1`, `PS2`, `PS3`, and `PROMPT_COMMAND` are the exception: an unsafe new value is left unchanged. Bash for the restore is `bash --noprofile --norc` with `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`, `BASH_XTRACEFD`, and `PS4` removed.

The worked install, the config file, and the failure messages are in `docs/`.

## Develop

```console
python3 -m pip install ruff
ruff check src tests
python3 -m unittest discover -s tests -v
```

The Zig build (`zig/`, Zig 0.17.1) is a static binary with the same interface; [COMPAT.md](COMPAT.md) lists that interface.
Build it and run the same suite against it:

```console
zig build test
zig build -Drelease=true
TOOL_BIN="$PWD/zig-out/bin/eessi-arch-guard" python3 -m unittest discover -s tests -v
```

The Python guard stays the default prolog until the binary has passed a shadow comparison on a partition, the real-Lmod tests, and a second review of its validators.

Copyright (c) eessi-arch-guard developers

<!-- stackhands:begin -->
## Part of stackhands

eessi-arch-guard is one of the [stackhands](https://github.com/stackhands) tools for building, installing and maintaining an HPC software stack with EasyBuild on top of EESSI.

| Tool | Job | Docs |
|---|---|---|
| [eb-stack](https://github.com/stackhands/eb-stack) | Turn conda-forge, Spack, PyPI, CRAN and Cargo recipes into easyconfigs; solve, shard and plan an EasyBuild build list | [docs](https://eb-stack.rgoswami.me/) |
| [eb-staged-rebuild](https://github.com/stackhands/eb-staged-rebuild) | Rebuild an installation beside the live one and swap it in, for a stack outside CernVM-FS that jobs read during the rebuild | [docs](https://stackhands.github.io/eb-staged-rebuild/) |
| **eessi-arch-guard** | Reload EESSI in a Slurm task that inherited a stack loaded for another CPU or GPU, as srun from a login shell does | [docs](https://stackhands.github.io/eessi-arch-guard/) |
| [eb-local](https://github.com/stackhands/eb-local) | Build on EESSI-extend inside a Slurm job's node-local disk, compile your own code with RPATH, list and uninstall per CPU target | [docs](https://stackhands.github.io/eb-local/) |
| [eb-keep-logs](https://github.com/stackhands/eb-keep-logs) | Keep a failed EasyBuild command's own output, which --tmp-logdir does not; rotate a pipeline's log tree | [docs](https://stackhands.github.io/eb-keep-logs/) |
| [eb-pipeline](https://github.com/stackhands/eb-pipeline) | Drive a site's EasyBuild builds from one site file, through eb-stack, eb-staged-rebuild and eb-keep-logs | [docs](https://stackhands.github.io/eb-pipeline/) |
| [rfc9162](https://github.com/stackhands/rfc9162) | Append-only Merkle logs hashed as RFC 9162, with tiled storage and signed checkpoints | [docs](https://stackhands.github.io/rfc9162/) |
| [zig-common](https://github.com/stackhands/zig-common) | The shared Zig library under the Zig tools: system calls, checked files, child processes | [docs](https://stackhands.github.io/zig-common/) |
<!-- stackhands:end -->
