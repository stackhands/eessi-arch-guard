# Run a program you built yourself on more than one node type

The guard reloads modules. It cannot change a program you compiled. A program linked against EESSI libraries records their directories as `DT_RPATH`, and those directories name the CPU target of the node it was linked on. A binary linked on zen4 loads zen4 libraries on every node, and can stop with an illegal instruction on zen2, whatever the guard has fixed in the environment. Build flags such as `-march=native` tie the program's own code to the build node as well.

So build once per node type and pick the build on the node that runs it.

## Build into a directory that names the target

Build inside an allocation on each node type, with EESSI loaded there:

```bash
salloc -p <partition> -n 4 -t 00:30:00
module load EESSI/2026.06 GSL/2.8-GCC-15.2.0 buildenv/default-foss-2026.1
p="$HOME/opt/prog/$EESSI_SOFTWARE_SUBDIR"
mkdir -p "$p/bin"
gcc -O2 prog.c -lgsl -lgslcblas -o "$p/bin/prog"
readelf -d "$p/bin/prog" | grep RPATH     # directories under .../software/linux/<target>/
```

The build environment loads last so its compiler wrappers record a run path for every loaded library, and the program runs without `LD_LIBRARY_PATH`. [eb-local](https://github.com/stackhands/eb-local) does the same with `eval "$(eb-local dev GSL/2.8-GCC-15.2.0)"`, exports `EB_LOCAL_DEV_PREFIX` as such a directory, and `eb-local check` reports run paths into another target's tree.

## Pick the build on the node

`EESSI_SOFTWARE_SUBDIR` has to expand in the task, after the guard has run on the task's node. In the batch script it holds the batch node's target:

```bash
srun "$HOME/opt/prog/$EESSI_SOFTWARE_SUBDIR/bin/prog"            # wrong: expanded on the batch node
srun bash -c 'exec "$HOME/opt/prog/$EESSI_SOFTWARE_SUBDIR/bin/prog"'   # expanded in each task
```

The single quotes keep the batch shell from expanding the path. With the guard as the site's `TaskProlog` or as `SLURM_TASK_PROLOG` ([Use the guard without changing the site TaskProlog](user_task_prolog.md)), each task's `EESSI_SOFTWARE_SUBDIR` names its own node, so a job across node types runs the matching build on each.

Without a task prolog, run the [shell mode](shell_batch.md) in the task:

```bash
srun bash -c 'eval "$(eessi-arch-guard --shell)" && exec "$HOME/opt/prog/$EESSI_SOFTWARE_SUBDIR/bin/prog"'
```

To fail fast on a node type you have not built for:

```bash
srun bash -c 'p="$HOME/opt/prog/$EESSI_SOFTWARE_SUBDIR/bin/prog"; [ -x "$p" ] || { echo "no build for $EESSI_SOFTWARE_SUBDIR" >&2; exit 1; }; exec "$p"'
```
