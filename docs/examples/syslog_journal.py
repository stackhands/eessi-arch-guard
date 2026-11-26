#!/usr/bin/env python3
"""Run a CPU mismatch reload with syslog enabled for journalctl checks.

From a checkout:

    python3 docs/examples/syslog_journal.py
    journalctl -t eessi-arch-guard -n 1 -o json-pretty

``$PREFIX`` in any transcript stands for the temporary prefix.
"""

from __future__ import annotations

import os
import sys

import support


def main() -> int:
    site, env = support.cpu_site()
    target = os.environ.get("EESSI_ARCH_GUARD_SYSLOG", "journald")
    env["EESSI_ARCH_GUARD_SYSLOG"] = target
    try:
        proc = support.run(env)
        print("case: syslog-reload")
        print("syslog: %s" % target)
        print("loaded: %s" % support.ZEN4)
        print("node cpuinfo: zen2")
        print("command: eessi-arch-guard")
        support.show(proc, site.root)
        if proc.returncode != 0:
            return proc.returncode
        if "EESSI_ARCH_GUARD_RELOADED=" not in proc.stdout:
            print("missing reload marker", file=sys.stderr)
            return 1
        return 0
    finally:
        site.close()


if __name__ == "__main__":
    sys.exit(main())