#!/usr/bin/env python3
"""Run ``--strict --shell`` with no private workspace, then with one.

``$PREFIX`` in the transcript stands for the temporary prefix. Only the
first run is printed. The second creates ``~/.local/state/eessi-arch-guard``
with mode 700, as the batch-script guide does, and checks that the reload
then runs.
"""

from __future__ import annotations

import os
import sys
import tempfile

import support


def main() -> int:
    site, env = support.cpu_site()
    try:
        for name in ("TMPDIR", "XDG_STATE_HOME", "EESSI_ARCH_GUARD_WORKDIR"):
            env.pop(name, None)
        home = tempfile.mkdtemp(prefix="home-", dir=site.root)
        os.chmod(home, 0o755)
        env["HOME"] = home
        # /tmp is shared, so --strict does not take it as a workspace.
        env["TMPDIR"] = "/tmp"
        proc = support.run(env, ("--strict", "--shell"))
        print("case: strict-no-workspace")
        print("command: python3 -I src/eessi_arch_guard.py --strict --shell")
        support.show(proc, site.root)
        if proc.returncode != 78 or "->restore-failed'" not in proc.stdout:
            print("--strict did not fail the reload with exit 78", file=sys.stderr)
            return 1
        if "strict mode needs an existing private job or state workspace" not in proc.stderr:
            print("cause missing from stderr", file=sys.stderr)
            return 1
        state = os.path.join(home, ".local", "state", "eessi-arch-guard")
        os.makedirs(state)
        os.chmod(state, 0o700)
        proc = support.run(env, ("--strict", "--shell"))
        if proc.returncode != 0 or "EESSI_ARCH_GUARD_RELOADED=" not in proc.stdout:
            print("the state workspace did not let the reload run", file=sys.stderr)
            sys.stderr.write(proc.stderr)
            return 1
        return 0
    finally:
        site.close()


if __name__ == "__main__":
    sys.exit(main())
