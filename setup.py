#!/usr/bin/env python3
"""Setup script for pw-bridge.

Compiles the C wrapper at install time.
"""
import subprocess
import sys
from pathlib import Path
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithCWrapper(build_py):
    """Custom build command that also compiles the C wrapper."""

    def run(self):
        native_dir = Path(__file__).parent / "native"
        if native_dir.exists():
            print("Building C wrapper...")
            try:
                subprocess.check_call(["make", "clean"], cwd=native_dir)
                subprocess.check_call(["make"], cwd=native_dir)
                print("C wrapper built successfully.")
            except subprocess.CalledProcessError as e:
                print(f"Warning: failed to build C wrapper: {e}",
                      file=sys.stderr)
                print("The wrapper must be built manually:",
                      file=sys.stderr)
                print(f"  cd {native_dir} && make", file=sys.stderr)
        else:
            print(f"Warning: {native_dir} not found", file=sys.stderr)

        super().run()


setup(cmdclass={"build_py": BuildWithCWrapper})