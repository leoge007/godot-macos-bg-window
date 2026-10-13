# godot-macos-bg-window

[简体中文](README.zh-CN.md)

Run Godot 4 **silently** on macOS: no Dock icon, no focus stealing, no window
popping up on your screen — while it keeps rendering normally and can still
save screenshots from the viewport.

Handy for automated tests, CI-style QA runs and screenshot jobs on a laptop you
are working on at the same time. Every `godot --path ...` that opens a window
normally yanks focus away from whatever you are typing into; this makes that
stop.

It is a tiny Objective-C library injected into **one** Godot process with
`DYLD_INSERT_LIBRARIES`. It does not modify, patch on disk or re-sign Godot.

![example capture](docs/example.png)

## How it works

Godot's macOS platform layer activates the app and shows a window on start-up.
The injected library swizzles a handful of AppKit / Godot methods before `main`:

| Goal | What is patched |
| --- | --- |
| Never become the active app | `activateApplication`, `activateIgnoringOtherApps:`, `activate`, `-[NSRunningApplication activateWithOptions:]` become no-ops; the activation policy is pinned to `NSApplicationActivationPolicyProhibited` (no Dock icon, no menu bar) |
| Never be visible | window frames are forced to `(-30000, -30000)` (also in `constrainFrameRect:toScreen:`, so AppKit cannot pull it back on screen); `alphaValue` is 0; `ignoresMouseEvents` is `YES`; `sharingType` is `NSWindowSharingNone`; collection behavior gets `Transient | IgnoresCycle` |
| Never take keyboard focus | `canBecomeKeyWindow` / `canBecomeMainWindow` return `NO` |
| Keep rendering | `-[NSWindow occlusionState]` reports *visible*, so Godot does not throttle drawing for a "hidden" window |
| Screenshots keep working | viewport texture read-back is not touched, so `get_viewport().get_texture().get_image()` returns the same pixels as an unpatched run |

Before changing anything the library verifies that **every** method it needs
exists. If one is missing (a future macOS or Godot changed), nothing is patched
and the library reports `active: false` — it never leaves a half-applied patch.

On success it atomically writes `{"active": true, "pid": <pid>}` to the file
named by `GODOT_BG_STATUS`. `run.py` waits for that file, so "patch loaded" is
confirmed, not assumed.

## Requirements

* macOS 12+ (Apple silicon or Intel)
* Apple clang (`/usr/bin/clang`, from the Xcode Command Line Tools)
* Godot 4.x for macOS. Developed and verified with 4.7.2 + Metal; the Godot
  class names `GodotApplication` / `GodotWindow` and the methods above must
  exist
* Python 3.8+ for the helper scripts (standard library only)

## Build

```sh
python3 build.py        # writes godot_bg.dylib (arm64 + x86_64)
```

`run.py` builds it automatically when it is missing or older than the source.

## Usage

```sh
python3 run.py [--godot=/path/to/godot] [options] -- <godot arguments...>
```

Everything after `--` goes to Godot unchanged. The Godot executable is, in
order: `--godot`, `$GODOT`, `godot` on `PATH`,
`/Applications/Godot.app/Contents/MacOS/Godot`.

```sh
# run a project in the background
python3 run.py -- --path ~/my-game

# run a script / scene and pass user arguments after a second `--`
python3 run.py --timeout 120 -- --path ~/my-game res://tests/shot.tscn -- /tmp/shot.png
```

Options:

| Option | Meaning |
| --- | --- |
| `--godot=PATH` | Godot executable |
| `--allow-fallback` | if the patch cannot be built or confirmed, launch Godot normally instead of failing (it **may steal focus**) |
| `--timeout=SEC` | terminate Godot after this long (exit code 124) |
| `--confirm-timeout=SEC` | how long to wait for the patch to confirm (default 5) |
| `--no-nice` | do not lower the priority (`nice -n 10`) |
| `--lock=DIR` | hold an atomic `mkdir` lock while Godot runs, to serialise GPU/windowed runs between several scripts |

Exit codes: Godot's own code; `3` patch not active (Godot is killed
immediately, nothing is silently relaunched); `124` timeout; `130` interrupted.

Notes on the launch line, because it is easy to get wrong:

```
nice -n 10 /usr/bin/env DYLD_INSERT_LIBRARIES=<lib> godot ...
```

`/usr/bin/nice` is SIP-protected, and SIP-protected binaries strip `DYLD_*`
variables from their environment. Setting the variable through `env` *after*
`nice` has started keeps both the priority and the injection. The same reason
means you cannot inject into SIP-protected binaries at all.

### Try the example

```sh
python3 run.py -- --path example -- "$PWD/shot.png"
```

`example/` is a minimal Godot 4 project: three colour blocks and a line of
text. It saves a 640×360 screenshot of its viewport and quits. You should see
`[godot_bg] background patch loaded` and no window, Dock icon or focus change.

### Check that the patch does not change rendering

Capture the same scene with the patch on and off, then compare pixel by pixel:

```sh
python3 run.py -- --path example -- "$PWD/on.png"          # patch on
godot --path example -- "$PWD/off.png"                      # plain launch: WILL show a window and take focus
python3 compare.py on.png off.png --output compare.json     # exit 0 and "pass": true when identical
```

`compare.py` uses only the Python standard library: it validates the PNG CRCs,
supports non-interlaced 8-bit RGB/RGBA, and compares size and every RGBA pixel
(compression and metadata differences do not matter). The scene scales to a
fixed 640×360 viewport, so different display DPIs do not matter either.

### Verify focus and window state

```sh
python3 sample.py --output out-dir -- python3 run.py -- --path example -- "$PWD/shot.png"
```

`sample.py` polls the frontmost application every 0.25 s with `osascript`, and
the patch logs the real `NSWindow` properties of the Godot process from the
inside (`GODOT_BG_SAMPLE_LOG`; non-shareable windows do not show up in external
window lists). `summary.json` passes only if the command exited 0, the
frontmost app never changed while windows existed, and every sampled window was
off-screen, transparent, mouse-transparent and non-shareable. Keep the
foreground still while it runs. `out-dir` must not exist yet.

## Limitations

* Affects only the process it is injected into. Other Godot instances, and the
  editor you already have open, are unaffected.
* Godot is neither modified nor re-signed. Binaries protected by SIP, or built
  with the hardened runtime without the
  `com.apple.security.cs.allow-dyld-environment-variables` entitlement, ignore
  `DYLD_INSERT_LIBRARIES`; `run.py` then reports the patch as not active and
  exits (a stock Godot download works).
* It relies on private-ish details of Godot's macOS platform layer (class and
  method names). A future Godot or macOS may break it; the failure mode is
  "not active", not a crash.
* The window is never visible and can never be clicked or typed into. Do not
  use it for anything that needs a human at the window, or for features that
  need a real, visible, focused window (native dialogs, some input paths).
* Verified with the Metal renderer. Other renderers are untested.
* Window capture by external tools (`screencapture`, screen sharing) will not
  see the window by design; use viewport read-back as the example does.
* The windows are hidden by moving them far off-screen with alpha 0. In very
  rare cases the window server may flash a window while a process starts, if
  the injection is ignored — `run.py` kills such a process as soon as it knows.

## Credits

Extracted from an indie game project's QA tooling.

## License

[MIT](LICENSE)
