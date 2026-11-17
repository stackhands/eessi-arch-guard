#!/usr/bin/env python3
"""Check docs/export.el on a page of its own: code and verbatim text.

    python docs/tools/test_export.py

Exports one Org page with docs/export.el, as the pixi task mkrst does but in
a scratch directory, and reads the reStructuredText it writes with docutils.
Each ~code~, =verbatim= and inline source block of the page must come out as
a literal of the same text: a reStructuredText literal takes no backslash
escapes, so one that ox-rst writes as \\*.log shows as \\*.log.

Exit status: 0 when every literal matches, 1 when one does not or the export
fails.
"""

import pathlib
import subprocess
import sys
import tempfile

import docutils.core
import docutils.nodes

EXPORT = pathlib.Path(__file__).resolve().parent.parent / "export.el"

# Each item of the page, in Org, and the literal it must come out as.
CASES = [
    ("=*.log=", "*.log"),
    ("~*.txt~", "*.txt"),
    ("=**/*.org=", "**/*.org"),
    ("=*=", "*"),
    ("src_sh{*.log}", "*.log"),
    ("=GCC/*=", "GCC/*"),
    ("~a\\b~", "a\\b"),
]


def export(page):
    """The reStructuredText that export.el writes for PAGE, an Org text."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "orgmode").mkdir()
        (root / "source").mkdir()
        (root / "orgmode" / "index.org").write_text(page)
        run = subprocess.run(
            ["emacs", "-Q", "--batch", "--load", str(EXPORT)],
            cwd=root,
            capture_output=True,
            text=True,
        )
        rst = root / "source" / "index.rst"
        if run.returncode != 0 or not rst.is_file():
            sys.stdout.write(run.stdout + run.stderr)
            raise SystemExit(f"test_export: the export failed with exit status {run.returncode}")
        return rst.read_text()


def literals(rst):
    """The text of each literal in RST, in order."""
    doc = docutils.core.publish_doctree(rst, settings_overrides={"report_level": 5})
    return [node.astext() for node in doc.findall(docutils.nodes.literal)]


def main():
    page = "#+TITLE: Literals\n\n" + "".join(f"- {org}\n" for org, _ in CASES)
    rst = export(page)
    got = literals(rst)
    if len(got) != len(CASES):
        print(f"test_export: {len(got)} literals, expected {len(CASES)}, in:\n{rst}")
        return 1
    problems = 0
    for (org, want), literal in zip(CASES, got):
        if literal != want:
            print(f"test_export: {org} comes out as {literal}, expected {want}")
            problems += 1
    print(f"test_export: {len(CASES)} literals, {problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
