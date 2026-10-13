#!/usr/bin/env python3
"""Build the universal (arm64 + x86_64) godot_bg.dylib with Apple clang.

Nothing is installed and Godot is never touched. The build is skipped when the
library is newer than godot_bg.m and this script.
"""
import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'godot_bg.m'
LIBRARY = HERE / 'godot_bg.dylib'


def build(output=LIBRARY):
    if sys.platform != 'darwin':
        raise RuntimeError('godot_bg only supports macOS')
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # The lock serialises concurrent builds; the atomic replace below means
    # nobody ever loads a half-written library.
    with output.with_suffix('.build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if output.exists() and output.stat().st_mtime_ns >= max(
                SOURCE.stat().st_mtime_ns, Path(__file__).stat().st_mtime_ns):
            return output
        with tempfile.TemporaryDirectory(prefix='godot-bg-', dir=output.parent) as temporary:
            target = Path(temporary) / output.name
            subprocess.run([
                '/usr/bin/clang', '-dynamiclib', '-fobjc-arc', '-O2',
                '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                '-arch', 'arm64', '-arch', 'x86_64', '-mmacosx-version-min=12.0',
                '-framework', 'Cocoa', str(SOURCE), '-o', str(target),
            ], check=True)
            os.replace(target, output)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=LIBRARY)
    print(build(parser.parse_args().output))
