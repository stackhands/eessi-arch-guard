# Changelog

<!-- towncrier release notes start -->

## [0.4.1] - 2026-11-27

### Bugfixes

- The release workflow ran the Python suite on the runner's system Python, whose setuptools rejects the SPDX `license` of `pyproject.toml`, so 0.4.0 shipped no GitHub release. The workflow now sets up Python 3.12, upgrades setuptools, installs Lua and jsonschema so the tests that need them run, and runs the same steps as CI, and the publish job checks for two binaries, six wheels and the share archive. 0.4.1 is the first release of the 0.4.0 changes with binaries and wheels.


## [0.4.0] - 2026-11-27

### Features

- The static binary is the prolog and the only implementation of the guard. The Python guard, the shadow comparison and the tests that compared the two are gone. `pip install` of the wheel gives a package whose `eessi-arch-guard` console script and `python3 -m eessi_arch_guard` start the bundled binary with the same arguments, else one on `PATH`. The wheel for a platform carries the binary, `share/sitepath.py`, `share/cvmfsrev.py` and `SitePackage.lua`, bash and zsh completions and a man page. COMPAT.md records what differs from the Python guard of releases before 0.4.
- The wheels declare free-threading support and carry a C extension, `eessi_arch_guard._launch`, built against the CPython stable ABI, that calls `execv(2)` for the launcher. A `cp39-abi3` wheel serves every CPython from 3.9 that has the GIL; the free-threaded 3.13t and 3.14t, which install no abi3 wheel, get a `cp313t` and a `cp314t` wheel. cibuildwheel builds each in the manylinux2014 image for x86_64 and aarch64, and `tools/check_wheel.py` checks the tag, the single `_launch` extension, the bundled binary and the free-threading classifier. CI imports `_launch` on 3.13t and 3.14t and checks the GIL stays off, and calls it from many threads.
- `eessi-arch-guard --check-mount` checks that the EESSI repository is on a cvmfs mount and that the site module directory precedes the EESSI one on `MODULEPATH`, and `eessi-arch-guard --check-revision --stat FILE [--min N]` reads and compares the cvmfs client revision. `share/sitepath.py` and `share/cvmfsrev.py` keep their command lines and run these. Messages of the two start with `eessi-arch-guard:` and a revision of more than 64 bits is not read.

### Bugfixes

- A cpuset walk whose directory starts with `//` stops after the filesystem root instead of reading `cpuset.cpus.effective` from the working directory.
- A failed close of the cache lock makes the Zig guard skip with the `OSError` of that close, as `os.close` raises it in Python. The guard used to ignore it and print the decision, cached or new, where Python printed the skip line. After a timeout, `SIGTERM` or another `OSError`, the Zig guard keeps that first stop and drops the failed close. Python's `finally` block reports the close in that case, and COMPAT.md lists the difference. An interrupted close of the lock is a failed close in both builds: `os.close` does not retry it, as `PEP 475` exempts it, so both skip with `InterruptedError`.
- A failed close of the cache temporary file, such as a lost write on the Network File System (NFS), no longer makes the Python guard close the descriptor a second time. The second close raised `OSError: [Errno 9] Bad file descriptor` out of the cache write, so the guard printed its skip line instead of its decision, or closed an unrelated descriptor that had reused the number. The temporary file is removed, the cache name is left as it was, and the guard prints its decision.
- A failed close of the scoped cpuinfo copy now makes the Zig guard skip with the `OSError` of that close, as `os.close` raises it in Python. The guard used to ignore it and run the CPU detection on a copy that might lack some of its data. The close error replaces a write error, as the `finally` block does in Python. An interrupted close is a failure in both, because `os.close` does not retry it and raises `InterruptedError`.
- Both guards now match the words processor and flags in a cpuinfo with the case folding of Python's regular expressions. A long s counts as an s, and both guards choose the same directory. A processor line is also read as a field of its block, as Python reads it, so a header spelled with a long s makes the processors differ in both guards.
- CPU affinity preparation skips FIFO paths and directories promptly instead of trying to read them as cpuinfo.
- Configuration files are reopened without following links and have their type and trust metadata checked again before reading.
- Intersect every `flags`, `features`, and `isa` line inside a processor record when cpuinfo is rewritten down to the processors a task may run, so a later line that drops `waitpkg` is not hidden by a richer first line.
- Make check-submit of the Python guard report an eb-stack failure whose output has bytes that are not UTF-8, with exit status 2 and the text of the failure, as the Zig guard does. Before, the Python guard stopped with a UnicodeDecodeError and exit status 0.
- Read a Slurm CPU bind value as Python does, so the Zig guard no longer ignores a value that the Python guard accepts.
- Read the output of a failing eb-stack in check-submit with the line ends that Python reads, so the message and the flagged module lines match the Python guard. The set is a carriage return with or without a line feed and the eight more characters that `str.splitlines` ends a line at.
- Split cgroup.controllers on Unicode whitespace, as the Python guard does, so both guards accept the same cpuset directories.
- The Zig guard builds against `zig-common` develop's widened lock timeout API without changing its lock timeout behavior.
- The Zig guard lower-cases a cpuinfo field name with the Unicode 13.0.0 tables of Python 3.9, the oldest supported Python. It used those of Unicode 15.0.0. A capital that a later Unicode added, such as U+10570, now stays as Python 3.9 and 3.10 leave it, and `COMPAT.md` names the newer Pythons that lower-case it. The generator `tools/gen_pylower.py` stops on any other Unicode version. On Python 3.9, `tests/test_pylower_table.py` checks every code point. It used to skip on a newer Python, and now checks the code points both versions assign. A capital sigma after U+1734, which Unicode 14.0.0 stopped counting as case-ignorable, stays final as on Python 3.9, and `COMPAT.md` lists it too.
- The Zig guard now lower-cases a cpuinfo field name as `str.lower()` does, for every script rather than plain letters only. Two processors that spell a field with letters of different case, such as the Kelvin sign and `k`, now share that field in both programs, and both make the scoped cpuinfo copy under the same conditions. `tools/gen_pylower.py` generates the mapping table from Python, and `tests/test_pylower_table.py` checks it against the interpreter over every code point.
- The Zig guard picks its temporary directory with the shared `fsx.TempDir` chooser, for the check-submit planner directory, the scoped cpuinfo copy and the last fallback of the restore workspace. That chooser probes each candidate with a file write, as Python's `tempfile.gettempdir` does. When no candidate takes a file, check-submit reports the same error text as the Python guard.
- The Zig guard reads the lines of a cpuinfo with Python's rules for whitespace and decimal digits. It scopes a cpuinfo as the Python guard does.
- The Zig guard reads the timeout option and setting with the decimal digits of any script. Python's `float()` reads them the same way. The Zig guard used to fall back to the default timeout.
- The Zig guard reports the `OSError` of a failed close of the restore scripts or of the check-submit planner inputs, as Python does. The restore helper fails with `restore failed: [Errno 5] Input/output error`, and check-submit exits 2 with `could not run eb-stack:` and the same text. The Zig guard used to drop the close and run the scripts or `eb-stack` on files that the Network File System (NFS) or a quota might have lost. The close error replaces a write error in both, and it comes before a failed chmod of `apply.sh`.
- The Zig restore helper reports a restore workspace that cannot be created with the OSError text Python gives, including the errno and the path. It does the same when it cannot make the directory for its shell scripts, where it used a fixed sentence before.
- The Zig restore helper reports the error Python prints when it cannot write the shell code it runs, instead of a fixed message.
- The Zig restore helper writes each restore script with one `write(2)`, as Python does, so a short write leaves a short script that bash runs instead of failing the restore.
- The `emeraldrapids` CPU subdirectory requires `waitpkg`, the same rule as `sapphirerapids`, because GCC's emeraldrapids march uses the Sapphire Rapids feature mask.
- The `wildcatlake` CPU subdirectory requires `waitpkg`, the same rule as `pantherlake`, because GCC's wildcatlake march uses the Panther Lake feature mask.
- The guard rejects Raptor Lake, Meteor Lake, Gracemont and Lunar Lake targets when
  the available CPUs lack the waitpkg flag.
- When the task may use only some of the processors of the node and the scoped cpuinfo copy cannot be made, because no temporary directory takes a file or its open or write fails, the Zig guard skips with the error Python raises instead of running the CPU detection on the unscoped cpuinfo. The copy is made with `fsx.mkstemp`, as `tempfile.mkstemp` makes it.
- `--restore` reports the same stderr as the Python guard for a request that is not UTF-8, not JSON, or not an object, and it reads an array or object nested 31, 32, or 33 deep.
- `read_cpu_flags` treats FIFO paths and directories as unreadable instead of waiting for input or reading the wrong file type.
- `sitepath.py --mountinfo` rejects FIFO paths and directories promptly with its existing read-error status.
- check-submit strips the failure text of eb-stack with Python's whitespace rules. A stderr of Unicode whitespace alone no longer hides the stdout. The Python guard reports the stdout in that case.
- pip install now installs share/cvmfsrev.py beside sitepath.py.

### Docs

- COMPAT.md states at its top that it describes v0.2.0 while the source is 0.3.x.
- The README Install section makes the Python guard the default prolog and the static binary opt-in, and links the how-tos for the Lmod hooks, the site path check and the cvmfs revision script.
- The how-tos for sitepath.py and cvmfsrev.py name the installed path under share/eessi-arch-guard.
- `COMPAT.md` lists U+1171E, the Ahom consonant sign medial ra, beside U+1734: Unicode 16.0.0 (Python 3.14) stopped counting it as case-ignorable, so a capital sigma after it lower-cases as medial `σ` there and as final `ς` in the guard, which keeps Unicode 13.0.0. `tests/test_pylower_table.py` expects that difference on 3.14.
- `COMPAT.md` no longer lists a timeout in non-ASCII digits or a `flags` name that only case folding matches as places where the Zig guard differs, since both now behave as in Python.

### Misc

- The Zig guard parses its command line with `zig-common`'s `argparse` module under `Policy.prolog_lenient`, in place of its own `parseArgs` loop.
- A parity test compares the Python and Zig guards on a restore under a file size limit. The limit cuts the scoped cpuinfo copy short after the restore scripts are written.
- Add a stub planner runner to the shared test runner module. It runs a guard in check-submit mode against a stub in place of eb-stack, so tests need no copy of that setup.
- Compare the Python guard and the program under test under limits on file size, each on a fresh site, so the fake Lmod log of one run no longer reaches the other.
- Give each test process, and each site a test builds, a private temporary directory, so two runs of the test suite at the same time no longer share the job cache files, restore workspaces and cpuinfo copies of their guard runs under `/tmp`.
- New `TOOL_BIN` tests in `tests/test_cpuinfo_field_case_parity.py` compare the two guards on cpuinfo fields that another processor spells with plain letters in another case, and check whether each guard places the scoped copy.
- Pin zig-common to the main commit that adds the decimal digit and temporary file helpers the guard needs.
- Pin zig-common to v0.14.0. Its `fsx.absPath` returns an error for a relative path when the working directory has been removed, and the guard handles it: a config path beside a relative program path ends the run with the `FileNotFoundError` skip line, a relative eb-stack path makes `--check-submit` report that it could not run eb-stack, and a relative program is not found when the guard runs it. Its `state.writePrivate` checks the close of the cache temporary file, so the Zig guard removes the file and leaves the cache name as it was after a failed close, as the Python guard does, and `COMPAT.md` no longer lists the difference.
- The tests run the binary only. Where a test compared the Python guard with the binary it compares the binary's output with a file under `tests/golden/`; `RECORD=1` rewrites those files.
- `src/eessi_arch_guard.py` has one non-blocking regular-file reader for paths that must not accept FIFOs or directories.
- `tests/test_cpuinfo_tempdir_parity.py` and `tests/test_restore_cpuinfo_limit_parity.py` now give the processors of their cpuinfo fixture the numbers of processors the host lets the test use. They pass on a host whose affinity mask has only processors past 7.
- `tests/test_locale_independence.py` checks that `harness.run_with_timeout` decodes UTF-8 output after the process locale is set to "C", also in an interpreter with UTF-8 mode off, that output that is not UTF-8 still raises, and that `test_unit` puts back the locale it found. `docs/examples/shell_demo.py` and `docs/tools/test_export.py` decode their subprocess output as UTF-8 whatever the locale.
- `tests/test_tracer_build.py` builds `tests/fs_fail.c` and `tests/close_fail.c` with gcc under `-Wall -Wextra -Werror` at `-O0` to `-O3` and `-Os`, and fails on any warning. It skips without gcc or off x86_64.


## [0.3.1] - 2026-11-24

### Bugfixes

- The release publishes the completions and man page as `eessi-arch-guard-VERSION-share.tar.gz`; 0.3.0 failed to publish because a release asset cannot be a directory.


## [0.3.0] - 2026-11-24

### Features

- The Zig build takes `-Dcommit` and reads its version from `build.zig.zon`. Its `--version` prints the version the Python guard prints, then the target, build mode, Zig version and commit in parentheses, such as `VERSION (x86_64-linux-musl, safe, zig 0.17.1, COMMIT)`; the commit reads `unknown` without `-Dcommit`. Release binaries carry the tagged commit.
- Emit structured RFC 5424 syslog or native `systemd-journald` decision events (`--syslog`, `EESSI_ARCH_GUARD_SYSLOG`, and `syslog` in `eessi-arch-guard.conf`) with stable `MSGID` values (`EESSI_ARCH_RELOAD`, `EESSI_ARCH_FAIL`, `EESSI_ARCH_SKIP`, and `EESSI_ARCH_NOOP`) and configurable RFC 5424 `SD-ID` enterprise number (`syslog_pen` / `EESSI_ARCH_GUARD_SYSLOG_PEN`, defaulting to the RFC 5398 documentation PEN `32473`).
- `--check-submit --target TARGET` checks the loaded modules against an EESSI CPU target directly, so a user can run the check on a cluster whose site has not mapped its partitions.
- `--check-submit` also reads the user and project trees EESSI-extend installs into, so a module you built for the partition's target counts as available and one built only on another node type is named as absent.
- `--check-submit` maps an intended Slurm partition and optional constraint to an EESSI target, then asks `eb-stack` to report loaded modules absent from that target's module tree without reloading or provisioning anything. With `--json`, one stdout line carries `schema_version` 1, the target, the loaded count, any missing modules, and `exit` / `exit_semantic`.
- `--json` on a normal decision run prints one line of JSON on stdout with `schema_version` 1, the decision kind, `reload_needed`, and `exit` / `exit_semantic`, instead of prolog or shell lines. Prolog and shell output are unchanged without the option. Both the Python guard and the Zig build.
- `eessi-arch-guard --dry-run` (with optional `--json`) evaluates the site configuration, `--chain` executable, timeout, syslog settings, read-only cache state, CPU/accelerator targets, and reload preconditions without running the chained prolog, writing cache or lock files, sending syslog events, or modifying the environment. With `--json`, one line on stdout carries `schema_version` 1, the decision summary fields, and the diagnostic keys.
- `share/cvmfsrev.py` reads a `cvmfs_config stat` file and prints the client revision. With no `--min` it exits 0. `--min N` exits 1 when the client revision is older than N. It does not remount.
- `share/lmod/SitePackage.lua` adds opt-in Lmod module usage tracking under the `ModuleUsageTracking` syslog tag, off by default until `EESSI_USAGE_TRACKING` or `usage_tracking` in `eessi-arch-guard.conf` is set to `1`, `true`, `on`, or `yes`. Loads in `load` mode are buffered and flushed from `ExitHookA` via `luaposix` syslog or `logger -t ModuleUsageTracking -p local0.info` as `user=... jobid=... module=... path=... host=... time=...` records compatible with `eb-keep-logs`. The CPU subdirectory and accelerator mismatch checks are unchanged.
- `share/lmod/SitePackage.lua` can hide the EasyBuild toolchain modules from `module avail` with Lmod's `isVisibleHook`. Nothing is hidden while `EESSI_HIDE_TOOLCHAINS` and the `hide_toolchains` key in `eessi-arch-guard.conf` are unset. `1`, `true` or `on` hides `GCC`, `GCCcore`, `foss`, `gfbf`, `gomkl`, `gompi`, `iimpi`, `intel` and `intel-compilers`; any other value replaces that list; `0`, `false` or `off` shows them. `module load` of the full name still works. The environment wins over the key.
- `share/lmod/SitePackage.lua` can warn when a module under `accel/nvidia/cc<N>` is loaded on a node whose `eessi_archdetect.sh accelpath` names a different accelerator. Nothing is checked while `EESSI_WARN_ACCEL_MISMATCH` and `warn_accel_mismatch` in `eessi-arch-guard.conf` are unset. `1`, `true` or `on` turns the check on; `0`, `false` or `off` leaves it off. The environment wins over the key.
- `share/lmod/SitePackage.lua` warns on stderr when a module file under `software/linux/SUBDIR/modules` is loaded and `EESSI_SOFTWARE_SUBDIR` names a different subdirectory. The load continues. An inherited job environment does not run the hook; the TaskProlog is unchanged.
- `share/sitepath.py` reports whether the EESSI repository is on a CVMFS mount and whether the site module directory precedes the EESSI one on `MODULEPATH`. A path that only exists on the root disk is not the client mount. The script changes neither the mount nor `MODULEPATH`.
- eessi-arch-guard now generates bash and zsh completion scripts and a man page from the live parser through pinned shtab and argparse-manpage calls in CI and release builds.

### Bugfixes

- The decision cache key includes the processors the task may run on and the accelerator devices named by `CUDA_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES`, `GPU_DEVICE_ORDINAL`, `SLURM_STEP_GPUS`, and `SLURM_GPUS_ON_NODE`. Two tasks of one job that do not share a cpuset or a GPU no longer reuse each other's result.
- The Zig build started by name through `PATH`, by a symbolic link in another directory than its target, reads `eessi-arch-guard.conf` beside the link, as the Python guard does, instead of beside the link's target. Started by a relative path, it names the file as the Python guard does, without the `.` and `..` parts of that path.
- The Python module drops `render`, `configured_chain` and `parse_chain_config`, which the guard never called. `render` lacked the 4094-byte prolog line check of `render_bytes`, the renderer the guard uses; the tests now go through `render_bytes`, and the site config chain is read only by `main`.
- When the time limit passes and the cache directory does not open for the last cache read, as when `TMPDIR` lost its permissions during the decision, the Zig build prints the skip line with that error, such as `skipped, PermissionError: [Errno 13] Permission denied: 'DIR'`, as the Python guard does, instead of reporting a timeout.
- The skip line after an unexpected failure comes from local task 0 only, as the reload message does, so a step with many tasks on a node shows it once. Both builds read `SLURM_LOCALID` for it; under `--shell` it still goes to stderr.
- When loading the other modules in one call fails, the restore loads them again one call each, in `LOADEDMODULES` order, from the environment after the EESSI load. A module from a tree an earlier module adds, such as the user's installations under EESSI-extend, is restored instead of reported as not available for the target. The message names every module that fails, up to 32, wherever it is in the list.
- In the Zig build, a SIGTERM that arrives as the decision starts a command, before the command has made its process group, kills the command: the handler and the decision's own kill signalled only the group, which did not exist yet, so the decision waited for the command to exit by itself. It builds on zig-common 0.9.0.
- `--chain` and `--timeout` no longer take a next argument that starts with `-` as their value: that argument is read as an option of its own, and the value is empty, as at the end of the arguments. In both builds `--chain --strict` used to take `--strict` as the chained path, so a failed decision exited 0 while the restore still used the strict workspace; it is now a strict run, with the warning about the empty `--chain`. `--chain=PATH` and `--timeout=SEC` take the rest of the argument as it is.
- A reload pinned by the waitpkg fallback purges with the task's `EESSI_ARCHDETECT_OPTIONS` and pins only the loads after the purge. The EESSI module's unload works out the tree to remove from that variable, so the pinned purge left the loaded tree on `MODULEPATH` behind the pinned one, with an Lmod warning. The restore request carries the pin as a new `pin` key.
- A restore that cannot run says why, with the `restore-failed` token: no private workspace (an `EESSI_ARCH_GUARD_WORKDIR` that is not a private directory owned by the job's user, or `--strict` with neither a private `TMPDIR` nor a state directory), a restore helper that does not start, or one that gives no usable reply. It used to print "module load failed" with the node's target, the token of a module that has no build there.
- Both builds refuse a `--restore` request whose `env` holds a value that is not a string with `restore request is malformed`; the guard's own requests hold only strings. The Python guard used to take `str()` of any value, so that `null` became `None`, and the Zig build did the same for `null`, `true`, `false` and numbers but refused a list or an object. `NaN`, `Infinity` and `-Infinity` anywhere in a request are refused as not JSON, with exit 1, as the Zig build did; the Python guard used to read them.
- `zig build suite` started in a subdirectory such as `src/` runs the Python suite: the step runs in the build root, where `-s tests` finds it, instead of where `zig build` was started, where unittest stopped with `Start directory is not importable: 'tests'`.
- CPU detection follows the processors the task may run on. A cpuset, an affinity mask, or a Slurm bind list (`0x2`, `0-3,8`, or a `cpu[0-3]` hostlist) drops the other processors before archdetect and the flag check, so a task bound to one microarchitecture is not given another's software subdirectory.
- A `--timeout` or configured `timeout` above 2147483.647 seconds, the longest wait for a command that Python's `subprocess` can make, is refused in both builds with the timeout warning, which names that limit instead of 9223372036.0, and the earlier value is kept. The Python guard used to accept it and then skip every decision that ran a command with `OverflowError: timeout is too large`.
- - Emit invalid syslog target and PEN warnings as separate ordered lines in `syslog.resolve` and `realMain`, matching Python `resolve_syslog` and `resolve_syslog_pen`.
- - Fall back to configured `eb_stack` or `"eb-stack"` when `--eb-stack` is empty in `checkSubmit` in `zig/main.zig`, matching Python `check_submit`.
- A JSON decision from the Zig build keeps its real result for a long `EESSI_SOFTWARE_SUBDIR` instead of a `ValueError` from the prolog line limit, as in the Python build.
- Both guards map the processor blocks of an `lxcfs`-renumbered `/proc/cpuinfo` onto the cgroup cpuset. A task whose sched affinity is narrower than its cpuset is then judged on the processors it runs on.
- CI installs the Lua interpreter required by the SitePackage regression test, so the Python and Zig jobs exercise the hook instead of failing before it runs.
- Check `--help` and `--version` before `--restore` in the Zig CLI and update `cli.org` and `COMPAT.md` to match.
- Check `command_succeeded` on `exec_cmd` in `share/lmod/SitePackage.lua` `emit_syslog` so missing or failing `logger` commands return `false` and report `0` flushed records from `flush_usage_records`.
- Check-submit now validates architecture mapping values, duplicate loaded-module entries, and empty target inventories, and it reports invalid inputs with exit code 2 and named values.
- Collapse repeated slashes and match the final `/software/linux/` prefix before `/modules/` in `share/lmod/SitePackage.lua` `path_parts`, so paths with an earlier `/software/linux/` segment or doubled slashes extract the true CPU and accelerator subdirectories.
- Collapse repeated slashes in `share/sitepath.py` `_norm` and `_modules` so paths such as `/cvmfs//software.eessi.io` match mount and `MODULEPATH` entries.
- Compare per-key first values in `zig/guard.zig` `sameFields` like Python `value_map`, so identical processor blocks with repeated `flags` lines are not collapsed to a single line.
- Decision `--json` output now has identical one-line formatting in the Python and Zig front ends.
- Delegate `guard.mkdtemp` in the Zig build to `zig-common` `fsx.mkdtemp` instead of carrying a local `getrandom` and `mkdirat` loop.
- Delegate `submitWorkDir` in `zig/main.zig` to `zig-common` `fsx.mkdtemp` instead of carrying a local `getrandom` and `mkdirat` loop.
- Dry-run text from the Zig build writes `?` for invalid UTF-8 bytes instead of the raw bytes, as the Python build does.
- Emergency skip lines from the Zig build stop at 500 characters as the Python ones do, and keep their error detail past 1024 bytes.
- Emit `"was": null` instead of `""` in Zig `--json` decision output when `EESSI_SOFTWARE_SUBDIR` is empty, matching Python `decision_view`.
- Fall back to `fn` and sanitize control characters in `share/lmod/SitePackage.lua` `warn_mismatch` and `warn_accel` when `modFullName` or `EESSI_SOFTWARE_SUBDIR` is blank or holds control characters.
- For a run that ends as an OSError skip, the Zig guard prints a null `detail` in the JSON object. It used to repeat the message there, where the Python guard prints null.
- Format `osErrorText` in `zig/main.zig` through `zig-common` `pytext.osErrorErrno` so config `lstat` error paths use Python `repr` quoting instead of literal single quotes.
- Format configuration file open and read failures in `zig/main.zig` with `sys.describe` and `text.pyRepr` so unreadable root-owned files emit `[Errno 13] Permission denied: 'PATH'` like Python.
- Handle `SIGINT` in `zig/main.zig` via the Python `KeyboardInterrupt` skip path (`0`, `78` with `--strict`, or decision JSON with `--json`) instead of terminating with signal `-2`.
- Help and version options now take precedence over restore mode and exit successfully.
- Keep oversized decimal `processor` entries as out-of-range blocks in `zig/guard.zig` `processorId` and `scopeCpuinfo` so they scope like Python arbitrary-precision integers instead of bypassing scoping.
- Malformed guard environment values now fail with an explicit invalid-environment error instead of being treated as a silent skip.
- Map `EESSI_SOFTWARE_SUBDIR` values containing line breaks or NUL bytes to `unrecognized` in `zig/syslog.zig` `emitEmergency`, matching Python `_side`.
- Map cpuinfo blocks that the Linux Containers file system (LXCFS) renumbered from zero onto the host processor IDs of the task's affinity and cgroup set, so the guard keeps the right processor flags inside LXCFS containers.
- On a cgroup v1 host the cpuset directory is the cpuset mount from `/proc/self/mountinfo`, including a mount outside `/sys/fs/cgroup`. The walk stops at that mount. A hierarchy that does not list the cpuset controller contributes no processors. A mount root that contains `..` keeps the historical `/sys/fs/cgroup` paths.
- Pass `.exec_error = true` and format `OSError` errno details in `zig/main.zig` when executing `--chain` or configured `chain` fails `execve`, so the guard takes the Python skip path instead of exiting `127`.
- Pin `zig-common` to `v0.10.0` in `build.zig.zon` so `eessi-arch-guard` can use `fsx.mkdtemp` plus `fsx.normPath` and `fsx.absPath`.
- Port Python `_selector_hint` to `selectorHint` in `zig/main.zig` `submitTarget` so unmapped partition and constraint errors append the nearest known mapping hint.
- Preserve colons in cgroup paths and match comma-separated `cpuset` controller names exactly in Zig `cgroupCpus`, matching Python `cgroup_cpus`.
- Python and Zig now agree on check-submit usage-error output when a partition is given without a matching site mapping.
- Python mount-table readers preserve malformed and out-of-range octal escapes as literal text, matching the native guard.
- Quote invalid `submit_target.*` values with `text.pyRepr` in `zig/main.zig` `submitTarget` so `--check-submit` matches Python `%r` formatting.
- Reject blank or `/` install paths under `--require-input` in `share/sitepath.py` instead of expanding them to `/modules/all`.
- Reject duplicate, multiple, or nested `EESSI/...` entries in `LOADEDMODULES` with exit status `2` in both Python and Zig `--check-submit`.
- Reject root-relative links in `docs/tools/check_site.py` instead of resolving them from the host filesystem root.
- Reject space-separated CPU IDs like `"0 1"` in `parse_cpu_spec` instead of concatenating them into a single CPU number, and allow spaces after commas inside `cpu[...]` hostlists to match the Zig parser.
- Reject trailing commas in Zig `parseCpuSpec` instead of accepting malformed CPU bind lists like `0,`, matching Python `parse_cpu_spec`.
- Replace invalid UTF-8 bytes with `?` when encoding RFC 5424 syslog and journald payloads in Zig `syslog`, matching Python `encode("utf-8", "replace")`.
- Report `both_missing` distinctly in `share/sitepath.py` instead of falling into the `same path` error message.
- Return `null` for `loaded` in Zig `--check-submit --json` on early invalid-input errors (`EESSI_PREFIX`, `EESSI_OS_TYPE`, and non-executable `eb-stack`), matching Python.
- Scope `EESSI_PROC_CPUINFO` to the task's allowed processors during the `--restore` helper's `EESSI` module load and restore the original `EESSI_PROC_CPUINFO` afterwards, so reloads on heterogeneous CPU bindings select the same target that `probe_cpu` detected.
- Skip unreadable or directory paths in `SitePackage.lua` `site_conf` instead of returning `nil` before checking the fallback config path.
- Strip Unicode whitespace in `zig/syslog.zig` `parseTarget` and `parsePen` with `text.pyStrip` so `--syslog`, `EESSI_ARCH_GUARD_SYSLOG`, and `EESSI_ARCH_GUARD_SYSLOG_PEN` match Python `str.strip`.
- Strip trailing slashes before checking `/modules/all` in `share/sitepath.py`, so `EESSI_SITE_SOFTWARE_PATH` and `EESSI_SOFTWARE_PATH` ending in `/modules/all/` do not get `/modules/all` appended twice.
- Surface `proc.run` `exec_error` descriptions and Python-style `exit N` details when `eb-stack` fails in Zig `--check-submit`.
- Switch `zig/main.zig` in `eessi-arch-guard` to call `zig-common` `fsx.normPath` and `fsx.absPath` instead of carrying local path normalization helpers.
- The Python guard accepts only ASCII digits in the port of a syslog target, as the Zig guard does, so a port written in other digits is reported and ignored instead of being used or ending the decision with an exception.
- The Python guard reads a processor number with more digits than its int limit. The Zig guard already read it. The Python guard used to end in a failed decision.
- The Python guard stops a cpuset parent walk at the filesystem root instead of consulting a path relative to its working directory.
- The Zig build of `eessi-arch-guard` uses `zig-common`'s errno table for `osErrorText` instead of a partial local lookup table.
- The Zig guard accepts syslog enterprise numbers of up to twenty digits that exceed 18446744073709551615, as the Python guard does, instead of warning and using the default.
- The Zig guard decides that a check-submit target has a module tree by a status check, as the Python guard does, so a module directory it may not list is reported as an empty inventory and not as an unavailable tree.
- The Zig guard now ends an eb-stack exec error with the program name in repr form. Python does the same.
- The Zig guard prints the dry-run "would reload" message on one line, as Python does: a control character in the loaded target becomes a space in the `message` of `--dry-run --json`.
- The Zig guard rejects a timeout padded with the ASCII separator characters U+001C to U+001F, as the Python guard does, instead of reading it as a number.
- The Zig guard reports a failed write of the check-submit planner inputs, or a failed creation of their directory, with the error Python prints, instead of a fixed message.
- The Zig guard reports a program that exists but cannot be executed with the error text Python prints and the program name in repr form, where it used an exit status of 127.
- The Zig guard splits the cpuinfo it scopes to a task's processors at every line break `str.splitlines` knows, as Python does, not only at a newline.
- Treat a bare or empty `--target` option as direct target mode in both Python and Zig `check-submit` instead of falling back to `--partition` selection.
- Treat empty `DOCS_SOURCE_REF` and `GITHUB_SHA` values as unset in `docs/tools/zig_api.zig`, so `{ref}` falls back to `main` instead of producing `blob//` source links.
- Trim leading and trailing whitespace or control bytes and fall back when a usage-tracking field holds only control bytes in `share/lmod/SitePackage.lua` `sanitize_field`.
- Validate octal digits explicitly in `share/sitepath.py` `_unescape` so non-octal `\8xx` or `\9xx` sequences do not raise `ValueError`.
- When a program cannot be executed, the Zig guard tries the next PATH entry. Python does the same. When no entry runs, it reports the error Python reports.
- When the chained prolog cannot be started and `--json` is on the command line, the Zig guard prints the exception text as the `message` of its failed decision, as the Python guard does, instead of an empty string.
- Write scoped cpuinfo files using the `TMPDIR` temporary directory lookup in Zig `prepareCpuinfo`, matching Python `tempfile.mkstemp`.
- `--check-submit` requires `eb-stack` to resolve to a regular file so FIFO paths are rejected as `eb-stack is not executable` in both front ends.
- `--dry-run` now preserves square brackets around IPv6 hosts in formatted `udp://` and `tcp://` syslog targets in both the Python guard and the Zig build.
- `SLURM_CPU_BIND_LIST` accepts a comma-separated list of hex masks, the form Slurm stores for `--cpu-bind=mask_cpu`, and CPU detection uses the union of those masks.
- `checkSubmit` in `zig/main.zig` uses `guard.resolvePath` for bare `eb-stack` lookups and keeps `guard.lmodInvocable` for slash paths, matching Python `shutil.which` when `PATH` is unset or empty.
- `eessi-arch-guard --check-submit` in the Zig build honors `TMPDIR`, strips shell startup variables before invoking `eb-stack`, and deduplicates module inventory entries.
- `share/cvmfsrev.py` caps `--stat` reads at `1 MiB` and exits `2` on larger files.
- `share/cvmfsrev.py` decodes `--stat` input with UTF-8 replacement characters so non-UTF-8 bytes in surrounding lines are replaced instead of aborting with `UnicodeDecodeError`.
- `share/cvmfsrev.py` opens `--stat` paths with `O_NONBLOCK` and checks `stat.S_ISREG` so FIFO files, directories, and dangling symlinks exit `2` promptly.
- `share/cvmfsrev.py` strips a leading UTF-8 BOM before scanning `--stat` lines for the revision header.
- `share/lmod/SitePackage.lua` reads the CPU subdirectory of a module under `software/linux/<cpu>/accel/nvidia/cc<N>/` as `<cpu>` alone, so a load no longer warns about a CPU mismatch when `EESSI_SOFTWARE_SUBDIR` is that CPU.
- `submitTarget` in `zig/main.zig` treats blank `submit_target.partition.*` and `submit_target.constraint.*` configuration values as unmapped like Python.
- `tests/test_site_package.py` skips when `lua` is not installed, instead of failing because the interpreter is missing.
- `towncrier build` keeps every fragment of `changelog.d` in the release notes: `*.misc.md` fragments go under a new Misc section, and the two fragments named `*.docs.md` are renamed to the `doc` type.
- check-submit now reports a dangling site config symlink as ignored config input instead of silently treating it as missing.
- check-submit now validates the selected target module tree path and returns invalid input when that path cannot be read.
- check-submit of the Zig guard runs a relative eb-stack path from the current directory. It used to refuse the path as not absolute, where Python runs it.
- dry-run JSON now has a published schema and CI validation against real guard output in both Python and Zig lanes.
- eessi-arch-guard now ignores unsafe EESSI_SOFTWARE_SUBDIR_OVERRIDE values so they do not block a needed reload.
- eessi-arch-guard now resolves the beside-program configuration from the invoked command path, so wrapper commands in PATH read their neighboring config file.

### Docs

- `DESIGN.md` refers to the rejected prototype by its section, "What was rejected".
- The manual says what EESSI 2026.06, Lmod 9.4.5 and Slurm 23.11.4 were seen to do: loading the EESSI module again runs its detection but leaves the other loaded modules inactive, the batch step gets a site `TaskProlog` and no per-job prolog, `srun --export=ALL,EESSI_ARCH_GUARD=0` keeps a value already set, and verbose reasons from a prolog need a run by hand. The command-line reference lists exit 78 in its summary, the batch-script how-to names the private workspace `--strict` needs, the cache and lock files are documented as staying after the job, and `COMPAT.md` points at the 0.2.0 source.
- The command-line reference matches the program: `--help` includes `--strict`, and `--version` prints 0.2.0. The install and tutorial transcripts show that version.
- `DESIGN.md`, the design topic and the user TaskProlog how-to are not tied to one site: the questions left open are any site's, sized by tasks per core rather than one cluster's node, and the measured cross-target run names the zen4 target, not a partition.
- Added black-box CLI tests that track help and version precedence with restore mode.
- COMPAT.md says that the second configuration file is looked for beside `__file__`, which for the console script `pip` installs is the module in `site-packages`. DESIGN.md lists the helper's environment so that its last three variables are the ones that name the helper's private directory, as the text says.
- Code and verbatim text that starts with `*` shows as written. ox-rst wrote each leading `*` of `~code~`, `=verbatim=` and an inline source block as `\*`, and a reStructuredText literal shows the backslash, so `=*.log=` came out as `\*.log`; no page here had such text yet. `docs/export.el` writes such text as a plain literal, and `pixi run -e docs docbld` runs `docs/tools/test_export.py`, which exports a page of literals and checks each one.
- Document `--check-submit --target` in `cli.org` and `explanation/pre-submit-checks.org`, add `invalid-environment` to `failures.org`, and add `--check-submit --json` exit statuses to `json-output.org`.
- Document `EESSI_VERSION`, `EESSI_USER_INSTALL`, `EESSI_PROJECT_INSTALL`, and the `--check-submit` `HOME/eessi` fallback in `docs/orgmode/reference/environment.org`.
- Document opt-in Lmod module usage tracking (`EESSI_USAGE_TRACKING` and `usage_tracking` in `eessi-arch-guard.conf`) in `docs/orgmode/howto/track-module-usage.org`, `docs/orgmode/howto/warn-at-module-load.org`, `docs/orgmode/howto/index.org`, and `docs/orgmode/reference/configuration.org`.
- Document the `--modulepath` and `--mountinfo` options of `share/sitepath.py` in `check-the-client-mount.org`.
- Document the environment variables read by `share/lmod/SitePackage.lua` and `share/sitepath.py` in `reference/environment.org`.
- Every `pub` declaration of the Zig build, and every public function, class and method of `src/eessi_arch_guard.py`, has a doc comment or docstring, whose first sentence is the summary the API pages list. The comment on `eessiModule` in `zig/text.zig` writes its pattern as code, so its API page shows `^EESSI/<segment>$` instead of passing `<segment>` through as an HTML tag.
- How-to: run a program you built yourself on more than one node type: build per target, and expand `EESSI_SOFTWARE_SUBDIR` in the task, where the guard has set it.
- Match the exact selector conflict error message emitted by `--check-submit` in `check-before-submit.org`.
- The README installs the release binary as the TaskProlog, with the Python file as the fallback.
- The README says when the guard is not needed, shows the measured wrong-tree case, and gives its cost per task for the Python and Zig builds.
- The cache row of COMPAT.md names everything the digest covers, the task's processors and visible GPUs included.
- The check-before-submit how-to now documents direct target checks with `--target` for workflows that do not use site selector mappings.
- The client-mount how-to matches `share/sitepath.py` exit codes (1 for a failed check, 2 for missing input or an unreadable mount table), documents unset shell variables expanding to `/modules/all`, and adds optional `--require-input` to reject that placeholder without changing the default.
- The documentation site is built from Org pages in `docs/orgmode/` with Sphinx, in four parts: the tutorial "See the guard reload EESSI for another CPU", how-to guides, explanation and reference. The API pages are generated from the doc comments of `zig/main.zig` and the files it imports and from the docstrings of `src/eessi_arch_guard.py`, and `zig build docs` installs the Zig autodoc site. `pixi run -e docs docbld` builds it as CI does and fails on a warning, an unresolved name or a dead internal link; `pixi run -e docs linkcheck` checks the external links. The `docs` workflow publishes the site to GitHub Pages; the Markdown pages, `docs/requirements.txt` and `pages.yml` are gone, and `CHANGELOG.md` has the marker under which `towncrier build` adds a release.
- The environment variables page says what it means by an absolute path, as both builds check it: one that starts with `/`, has no `..` part, and holds no newline, carriage return or NUL. A `TMPDIR` with a `..` part is not used, and an `EESSI_PREFIX` or `EESSI_INIT_PREFIX` with one ends in `archdetect-failed`, which the page did not say.
- The first tutorial pins to `main` instead of an old detached commit, and its Next steps return to a branch before install how-tos that expect a normal checkout.
- The install how-to names Ubuntu's `python3-venv` package before the optional login-node virtual environment step.
- The install how-to states that the default `TaskProlog` is fail-open on a failed reload and that `--strict` exit 78 is opt-in through a site-owned wrapper.
- The module-load how-to says that an `accel/nvidia/cc<N>` or `accel/amd/gfx<target>` path is compared as its CPU subdirectory.
- The site has the rest of its pages. How-to guides configure the guard, use it without changing the site TaskProlog, run it in a batch script, run a program you built yourself on more than one node type, and correct a failed reload. Explanation pages say why a task prolog runs the reload, and why the guard prints prolog lines or shell lines and shares one decision per step. Reference pages describe the command, the configuration file, the environment variables, the messages and markers, and the CPU flags. Their transcripts come from the scripts in `docs/examples/` and from runs of the guard.
- The syslog how-to now includes a verified command transcript with exit status, and CI runs the syslog_journal docs example.
- The syslog how-to runs `docs/examples/syslog_journal.py` to create a CPU mismatch reload before reading `journalctl`, so the verification step sees an emitted record.
- Updated the configuration file reference and compatibility table to document that dangling configuration symlinks are treated as existing symlinks and ignored without falling through.
- `DESIGN.md` says what the guard does now: detection on the task's processors, the full helper command and environment, the 4094-byte prolog line, four-digit year modules, exit 78 under `--strict`, the cache per output mode with the task's processors and GPUs in its key and the strict workspace, the cache trust rule, the prolog line from local task 0 only, and the Zig build beside the Python file.
- `build.zig` and `docs/tools/zig_api.zig`, which writes the Zig API pages, have a doc comment on their `pub` functions, `build` and `main`, like every other `pub` declaration in the repository.
- `docs/examples/strict_workspace.py` prints the `--strict --shell` failure that "Run the guard in a batch script" quotes, then checks that the reload runs once `~/.local/state/eessi-arch-guard` exists. The page shows its command, and CI's docs job runs it with the other examples, so every output block in the documentation comes from a script in the repository.
- `pip install` now installs `share/sitepath.py` and `share/lmod/SitePackage.lua` under `share/eessi-arch-guard/`, and the README stackhands table lists eb-pipeline.

### Misc

- A release is cut with `cog bump`: `cog.toml` runs `tools/release-prepare`, which builds the changelog and sets the version wherever the release workflow or a download command names it. `RELEASING.md` has the steps.
- A table of about two hundred cases runs the Python guard and the program named by `TOOL_BIN` on the same generated tree and compares exit status, standard output, and standard error. The cases cover cpupath output, the override and switch variables, install layouts, and accelerator answers. The tests skip when `TOOL_BIN` is unset.
- Add Python and `TOOL_BIN` tests in `tests/test_strict_status.py` covering `cpupath` multiline, `cpupath` unsafe candidate, and `accelpath` invalid subdirectory `archdetect-failed` refusals in default and `--strict` modes.
- Add Python and `TOOL_BIN` tests in `tests/test_syslog.py` covering invalid `--syslog`, `EESSI_ARCH_GUARD_SYSLOG`, and `config syslog_pen` warnings and fallback defaults.
- Add Python and `TOOL_BIN` tests in `tests/test_workspaces.py` covering the four `_restore_mismatch` `restore-failed` refusal messages when Lmod exits `0` without selecting the expected CPU, accelerator, `EESSI`, or application module.
- Add Python and `TOOL_BIN` tests in `tests/test_year_modules.py` verifying that `--syslog` ignores directory or FIFO paths and oversized Unix socket paths cleanly.
- Add `tests/differential.py`, the shared module that runs the Python guard and the program under test each on a fresh site, under a file size limit when asked, with the random workspace names normalized.
- Extract `default_path` and `resolvePath` as public helpers in `zig/guard.zig` so `resolve` and downstream callers share one `PATH` lookup rule.
- Format `OSError` path diagnostics in `zig/guard.zig` and `zig/main.zig` through `zig-common` `pytext.osErrorErrno` and `pytext.osError`.
- Give the killed helper test a private temporary directory, so test runs in parallel no longer see the sites of each other.
- Parity tests in `tests/test_integration.py` skip through `tests/harness.py` when `TOOL_BIN` is unset, instead of passing without a binary comparison. The rest of the Python suite still runs. With `TOOL_BIN` set, the dry-run, invalid-environment, and usage-error comparisons run as before.
- The Emerald Rapids test binds to fixture processors in the live affinity mask and skips when none exist. A Zig test passes LXCFS-renumbered blocks through `scopeCpuinfoWithBase` with affinity narrower than the cgroup cpuset.
- The Zig build sends its syslog and journald events through `zig-common` 0.12.0 (`syslog.send`): the unix datagram and stream sends, UDP, and TCP with octet counting are the code eb-keep-logs uses. The events and their formatting are unchanged.
- The ci workflow runs check-submit against the eb-stack 0.8.0 release, with the Python and the Zig guard.
- The dead local `maximalSubpart` helper is removed from `zig/text.zig` because `decodeReplace` already delegates to `zig-common` `pytext.decodeReplace`.
- The documentation examples `check_config.sh` and `check_install.sh` clear `CDPATH` with an explicit empty string, so ShellCheck reports no SC1007 warning for them.
- The test harness can run a program with its streams and environment as bytes, and a parity test uses it to compare the Python and Zig guards on input that is not valid UTF-8.
- `envName` in `zig/text.zig` now delegates to `zig-common` `env.validName` and adds hostile variable-name assertions in `test "validators"`.
- `lockExclusive` in `zig/guard.zig` delegates deadline and cancellation waiting to `zig-common` `state.lockExclusiveCancel` instead of maintaining a local `flock` polling loop.
- `oneLine` and `messageBytes` in `zig/text.zig` share one bounded formatter that writes into a caller buffer.
- `pyRepr` in `zig/text.zig` delegates to `zig-common` `pytext.reprStrAlloc` instead of carrying a local implementation.
- `tests/harness.py` adds `run_with_timeout` so subprocess tests against hostile inputs finish within a bounded timeout.


## [0.2.0] - 2026-11-14

### Features

- Add opt-in strict shell and task-prolog status for user job wrappers while retaining the default decision failure policy.
- Accept four-digit ASCII year frontends alongside validated versioned module names during a restore.
- A static Zig build of the guard, `zig build -Drelease=true`, runs the same decision as the Python script and passes the same test suite (`TOOL_BIN=zig-out/bin/eessi-arch-guard python -m unittest discover -s tests`). It is not yet the default prolog; it replaces the Python guard only after the release gate in the port plan (shadow comparison, real Lmod, a second review of the validators).

### Bugfixes

- The decision cache is kept per output mode. A batch script that runs `--shell` after the task prolog no longer gets the prolog's cached result, such as a failure for a value with a newline that `--shell` can carry, and a prolog no longer reuses a `--shell` result.
- The cache and its lock are trusted only when the open file is a regular file owned by the caller, with one link and no group or other bits. A file planted at the cache name in a shared `/tmp`, such as a FIFO or another user's file, no longer feeds the decision or blocks the prolog, and a cached entry that changes `LD_*`, `SLURM_*`, `BASH_FUNC_*` or another denylisted name is ignored.
- Honor existing private job workspaces for restore temporaries and provide a strict-mode state-directory fallback without creating or widening configured bases.
- The Zig build exits 0 with the one skip line on every failure path it can reach: out of memory, a cache lock error other than would-block (reported instead of waited out), and a crash, whose handlers now run on an alternate stack; an emergency stops the chained prolog's child first. It builds on zig-common 0.8.1 and targets the baseline CPU by default.

### Docs

- This release ships the Zig build without the shadow comparison beside the Python guard that the port plan's release gate names; the maintainer waived it on 2026-11-14. The Python guard stays the default prolog, and a site switching to the binary should run the comparison itself first (`RELEASING.md`, item 5).


## [0.1.2] - 2026-11-12

### Features

- Opt-in strict mode returns status 78 for failed decisions and unexpected exceptions.
- Restore numeric year-module frontends alongside EESSI module names.
- Strict restores and caches use caller-owned private job or state directories.

### Docs

- Describe user-space Slurm task prologs without changing the site TaskProlog.
- Verify the supported Python 3.9 and 3.12 interfaces and documented examples.

## [0.1.1] - 2026-11-08

### Docs

- The WAITPKG march list is checked against GCC 16 (releases/gcc-16): the same set GCC 15.2.0 carried plus novalake, and znver6, the other new mask, has no WAITPKG; the comment names that source.

## [0.1.0] - 2026-11-05

### Features

- The decision stops at one deadline, a killed helper loses its temporary directory, and config `timeout` applies with `--chain` and `--shell`.
- The root-owned config file is read as a map of settings.

### Bugfixes

- A GCC march that includes waitpkg is skipped when the CPU flag is missing, and the next installed archdetect candidate is loaded.
- A config value that is not UTF-8 is kept with surrogateescape and the other keys still apply.
- An interrupted cache write removes its temporary file.
- Export values keep their bytes, a failed write exits 0, and each prolog line stays within 4094 bytes.
- The fake Lmod writes each script line as the environment's own bytes.
- The package metadata keeps the license expression and omits the license classifier that current setuptools rejects.
- The restore helper is `python3 -I -X utf8` and carries each name and value as those same bytes.
- The sdist includes CHANGELOG.md, DESIGN.md, and the test files the default manifest left out.

### Docs

- An Intel A100 node uses the first archdetect candidate whose software directory exists.
