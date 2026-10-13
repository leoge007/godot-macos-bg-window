#!/usr/bin/env python3
"""Run Godot on macOS without stealing focus or showing a window.

    python3 run.py [options] -- <godot arguments...>

Builds godot_bg.dylib when needed, injects it into this one Godot process and
waits for the patch to confirm it is active. If it cannot be confirmed the
Godot process is killed and run.py exits non-zero; it never silently falls back
to a normal (focus-stealing) launch unless --allow-fallback is given.
"""
import argparse
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build  # noqa: E402

DEFAULT_APP_BINARY = '/Applications/Godot.app/Contents/MacOS/Godot'


def find_godot(explicit):
    candidate = explicit or os.environ.get('GODOT') or shutil.which('godot') or DEFAULT_APP_BINARY
    path = shutil.which(candidate) or candidate
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise SystemExit(f'run.py: Godot executable not found: {candidate} (use --godot=PATH or $GODOT)')
    return path


@contextmanager
def directory_lock(path, timeout):
    """Atomic mkdir lock, for serialising windowed runs between processes."""
    lock = Path(path)
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SystemExit(f'run.py: still waiting for lock {lock} after {timeout:g}s')
            if not announced:
                print(f'run.py: waiting for lock {lock}', file=sys.stderr, flush=True)
                announced = True
            time.sleep(0.5)
    owner = lock / 'owner'
    try:
        owner.write_text(str(os.getpid()))
        yield
    finally:
        owner.unlink(missing_ok=True)
        try:
            lock.rmdir()
        except OSError:
            pass


def read_status(path):
    try:
        marker = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def kill(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGCONT)
        proc.kill()
        proc.wait()


def launch(command, env, status, pid_check_timeout, allow_fallback, fallback_command, timeout):
    """Start Godot and return its exit code. `status` is None for an unpatched launch."""
    proc = subprocess.Popen(command, env=env)
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if status is not None:
            deadline = time.monotonic() + pid_check_timeout
            failure = None
            while True:
                marker = read_status(status)
                if marker and marker.get('pid') == proc.pid:
                    if marker.get('active') is True:
                        break
                    failure = marker.get('error', 'patch reported inactive')
                    break
                if proc.poll() is not None:
                    failure = f'Godot exited with code {proc.returncode} before the patch was confirmed'
                    break
                if time.monotonic() > deadline:
                    failure = (f'patch not confirmed within {pid_check_timeout:g}s '
                               '(SIP-protected or hardened binary ignoring DYLD_INSERT_LIBRARIES?)')
                    break
                time.sleep(0.005)
            if failure:
                kill(proc)
                print(f'run.py: background patch NOT active: {failure}', file=sys.stderr)
                if not allow_fallback:
                    return 3
                print('run.py: --allow-fallback given, launching normally (may steal focus)', file=sys.stderr)
                return launch(fallback_command, env, None, 0, False, None, timeout)
        try:
            return proc.wait(timeout=timeout or None)
        except subprocess.TimeoutExpired:
            print(f'run.py: timeout after {timeout:g}s, terminating Godot', file=sys.stderr)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill(proc)
            return 124
    except KeyboardInterrupt:
        kill(proc)
        return 130
    finally:
        kill(proc)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    argv = sys.argv[1:]
    if '--' in argv:
        split = argv.index('--')
        own, godot_args = argv[:split], argv[split + 1:]
    else:
        own, godot_args = argv, []
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     usage='run.py [options] -- <godot arguments...>')
    parser.add_argument('--godot', help='Godot executable (default: $GODOT, then `godot` on PATH, then /Applications/Godot.app)')
    parser.add_argument('--allow-fallback', action='store_true',
                        help='if the patch cannot be confirmed, launch Godot normally instead of failing (may steal focus)')
    parser.add_argument('--timeout', type=float, default=0, help='kill Godot after this many seconds (default: no limit)')
    parser.add_argument('--confirm-timeout', type=float, default=5, help='seconds to wait for the patch to confirm (default 5)')
    parser.add_argument('--no-nice', action='store_true', help='do not lower the process priority (nice -n 10)')
    parser.add_argument('--lock', metavar='DIR',
                        help='hold this mkdir-style lock while Godot runs, to serialise windowed runs between processes')
    parser.add_argument('--lock-timeout', type=float, default=1200, help='seconds to wait for --lock (default 1200)')
    options = parser.parse_args(own)
    if sys.platform != 'darwin':
        raise SystemExit('run.py: macOS only')
    if not godot_args:
        parser.error('no Godot arguments given; put them after `--`')
    godot = find_godot(options.godot)
    try:
        library = build.build()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'run.py: building godot_bg.dylib failed: {error}', file=sys.stderr)
        if not options.allow_fallback:
            return 3
        library = None

    plain = ([] if options.no_nice else ['/usr/bin/nice', '-n', '10']) + [godot, *godot_args]
    with tempfile.TemporaryDirectory(prefix='godot-bg-') as temporary:
        env = os.environ.copy()
        env.pop('GODOT_BG_STATUS', None)
        status = None
        command = plain
        if library:
            status = Path(temporary) / 'active.json'
            env['GODOT_BG_STATUS'] = str(status)
            inserted = str(library) + (':' + env['DYLD_INSERT_LIBRARIES'] if env.get('DYLD_INSERT_LIBRARIES') else '')
            # /usr/bin/nice is SIP-protected and strips DYLD_* from its environment.
            # Setting the variable with `env` *after* nice has started keeps both
            # the priority change and the injection.
            prefix = [] if options.no_nice else ['/usr/bin/nice', '-n', '10']
            command = prefix + ['/usr/bin/env', 'DYLD_INSERT_LIBRARIES=' + inserted, godot, *godot_args]
        guard = directory_lock(options.lock, options.lock_timeout) if options.lock else nullcontext()
        with guard:
            return launch(command, env, status, options.confirm_timeout, options.allow_fallback,
                          plain, options.timeout)


if __name__ == '__main__':
    sys.exit(main())
