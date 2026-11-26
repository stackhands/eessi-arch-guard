"""Build hook: a wheel that carries the binary is a platform wheel.

CI copies the static binary to src/eessi_arch_guard/bin/eessi-arch-guard
before it builds the wheel. The binary has no dynamic dependencies, so the
wheel is tagged py3-none-manylinux2014_ARCH. Without the binary the wheel is
pure Python and the launcher finds a binary on PATH.
"""

from pathlib import Path

from setuptools import setup

try:
    from setuptools.command.bdist_wheel import bdist_wheel
except ImportError:  # setuptools older than 70.1
    from wheel.bdist_wheel import bdist_wheel

BINARY = Path(__file__).resolve().parent / "src" / "eessi_arch_guard" / "bin" / "eessi-arch-guard"


class PlatformWheel(bdist_wheel):
    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = not BINARY.is_file()

    def get_tag(self):
        python, abi, plat = super().get_tag()
        if self.root_is_pure:
            return python, abi, plat
        if plat.startswith("linux_"):
            plat = "manylinux2014_" + plat[len("linux_") :]
        return "py3", "none", plat


setup(cmdclass={"bdist_wheel": PlatformWheel})
