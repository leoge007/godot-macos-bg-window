#!/usr/bin/env python3
"""Verify that a command (normally run.py) did not take focus and kept its windows hidden.

    python3 sample.py --output DIR -- python3 run.py -- --path example -- out.png

Every 0.25 s the frontmost application is queried with osascript, while the
patch records the real NSWindow properties from inside the Godot process
(GODOT_BG_SAMPLE_LOG). The summary passes when the command exited 0, the
frontmost application never changed while windows existed, and every sampled
window was off-screen, transparent, mouse-transparent and non-shareable.
Keep the foreground untouched while this runs. DIR must not exist yet.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def frontmost():
    result = subprocess.run(['osascript', '-e',
        'tell application "System Events" to get name of first application process whose frontmost is true'],
        capture_output=True, text=True, timeout=5, check=True)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command is required')
    args.output.mkdir(parents=True)
    focus_log, window_log = args.output / 'focus.jsonl', args.output / 'windows.jsonl'
    console, summary_path = args.output / 'console.log', args.output / 'summary.json'
    initial = frontmost()
    env = os.environ.copy()
    env['GODOT_BG_SAMPLE_LOG'] = str(window_log.resolve())
    samples = []
    proc = None
    with console.open('x') as log, focus_log.open('x') as focus:
        try:
            proc = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            tick = time.monotonic()
            while proc.poll() is None:
                row = {'epoch': time.time(), 'frontmost': frontmost()}
                samples.append(row)
                focus.write(json.dumps(row) + '\n')
                focus.flush()
                tick += .25
                time.sleep(max(0, tick - time.monotonic()))
        finally:
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
    windows = []
    if window_log.exists():
        windows = [json.loads(line) for line in window_log.read_text().splitlines() if line.strip()]
    populated = [row for row in windows if row['windows']]
    hidden = bool(populated) and all(row['policy'] == 2 and all(
        w['x'] == -30000 and w['y'] == -30000 and w['alpha'] == 0 and w['ignores_mouse']
        and w['sharing'] == 0 and w['occlusion'] & 2 and w['collection'] & 8 and w['collection'] & 64
        and not w['intersects_screen'] for w in row['windows']) for row in populated)
    # Only judge focus while Godot windows existed (lock waiting / start-up excluded).
    if populated:
        first, last = populated[0]['epoch'], populated[-1]['epoch']
        during = [r for r in samples if first <= r['epoch'] <= last]
    else:
        during = []
    summary = {'exit': proc.returncode, 'initial_frontmost': initial,
               'focus_samples': len(during), 'frontmost_seen': sorted({r['frontmost'] for r in during}),
               'focus_unchanged': bool(during) and all(r['frontmost'] == initial for r in during),
               'window_samples': len(populated), 'windows_hidden': hidden}
    summary['pass'] = proc.returncode == 0 and summary['focus_unchanged'] and hidden
    summary_path.write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary))
    if not summary['pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
