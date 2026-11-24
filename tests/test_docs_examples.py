"""Execute docs/examples commands embedded in Diataxis pages."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

import private_tmp  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIATAXIS_ROOT = ROOT / "docs" / "orgmode"
DOCS_EXAMPLES = ROOT / "docs" / "examples"


def _iter_blocks(lines: list[str]) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#+begin_src "):
            kind = "src"
            i += 1
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != "#+end_src":
                body.append(lines[i])
                i += 1
            blocks.append((kind, "\n".join(body)))
        elif line == "#+begin_example":
            kind = "example"
            i += 1
            body = []
            while i < len(lines) and lines[i].strip() != "#+end_example":
                body.append(lines[i])
                i += 1
            blocks.append((kind, "\n".join(body)))
        i += 1
    return blocks


def _example_commands() -> list[tuple[pathlib.Path, str, str | None]]:
    pairs: list[tuple[pathlib.Path, str, str | None]] = []
    for page in sorted(DIATAXIS_ROOT.rglob("*.org")):
        text = page.read_text(encoding="utf-8").splitlines()
        blocks = _iter_blocks(text)
        for idx, (kind, body) in enumerate(blocks):
            if kind != "src":
                continue
            lines = [line.strip() for line in body.splitlines() if line.strip()]
            if len(lines) != 1:
                continue
            command = lines[0]
            if "docs/examples/" not in command:
                continue
            if command.startswith("docs/examples/check_submit.sh"):
                # Checked by a dedicated harness test below.
                pairs.append((page, command, None))
                continue
            shown = None
            if idx + 1 < len(blocks) and blocks[idx + 1][0] == "example":
                shown = blocks[idx + 1][1]
            pairs.append((page, command, shown))
    return pairs


class DocsExamplesTests(unittest.TestCase):
    def test_diataxis_docs_examples_have_matching_transcripts(self):
        commands = _example_commands()
        self.assertTrue(commands, "no docs/examples commands discovered")
        for page, command, shown in commands:
            if command.startswith("docs/examples/check_submit.sh"):
                continue
            proc = subprocess.run(
                command,
                cwd=ROOT,
                shell=True,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"{page}: {command}\n{proc.stderr}")
            self.assertEqual(proc.stderr, "", f"{page}: {command}")
            self.assertIsNotNone(shown, f"{page}: missing transcript for `{command}`")
            self.assertEqual(
                proc.stdout.strip("\n"),
                shown.strip("\n"),
                f"{page}: transcript drift for `{command}`",
            )

    def test_check_submit_wrapper_invokes_guard_then_sbatch(self):
        script = DOCS_EXAMPLES / "check_submit.sh"
        with tempfile.TemporaryDirectory(prefix="eag-submit-wrapper-") as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            calls = tmp_path / "calls.log"
            job = tmp_path / "job.sh"
            job.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(job, 0o755)

            guard = bin_dir / "eessi-arch-guard"
            guard.write_text(
                "#!/bin/sh\n"
                "printf 'guard %s\\n' \"$*\" >> \"$CALLS_LOG\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            os.chmod(guard, 0o755)

            sbatch = bin_dir / "sbatch"
            sbatch.write_text(
                "#!/bin/sh\n"
                "printf 'sbatch %s\\n' \"$*\" >> \"$CALLS_LOG\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            os.chmod(sbatch, 0o755)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CALLS_LOG"] = str(calls)
            env["EESSI_SUBMIT_PARTITION"] = "cpu"
            env["EESSI_SUBMIT_CONSTRAINT"] = "zen4"
            proc = subprocess.run(
                [str(script), str(job)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertEqual(proc.stderr, "")
            lines = calls.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                lines,
                [
                    "guard --check-submit --partition cpu --constraint zen4",
                    f"sbatch --partition cpu --constraint zen4 {job}",
                ],
            )
