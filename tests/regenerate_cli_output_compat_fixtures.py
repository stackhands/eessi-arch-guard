from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_COMMIT = "b8f42a6df68de57b49f987af25e55154556b098a"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "cli_output_compat"


def stable_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("EESSI_", "SLURM_")):
            env.pop(key, None)
    for key in ("MODULEPATH", "LOADEDMODULES", "_LMFILES_", "_ModuleTable001_"):
        env.pop(key, None)
    return env


def run_script(script: Path, *args: str) -> tuple[int, bytes, bytes]:
    cp = subprocess.run(
        [sys.executable, "-I", str(script), *args],
        cwd=Path(__file__).resolve().parents[1],
        env=stable_env(),
        capture_output=True,
        check=False,
    )
    return cp.returncode, cp.stdout, cp.stderr


def write_fixture(stem: str, result: tuple[int, bytes, bytes]) -> None:
    rc, out, err = result
    (FIXTURES_DIR / f"{stem}.returncode").write_text(f"{rc}\n", encoding="utf-8")
    (FIXTURES_DIR / f"{stem}.stdout").write_bytes(out)
    (FIXTURES_DIR / f"{stem}.stderr").write_bytes(err)


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    cp = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:src/eessi_arch_guard.py"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="eag-base-cli-") as td:
        script = Path(td) / "eessi_arch_guard.py"
        script.write_bytes(cp.stdout)
        script.chmod(0o755)
        write_fixture("help", run_script(script, "--help"))
        write_fixture("run", run_script(script))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
