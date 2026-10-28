# Design: apex-router as a standalone macOS app (Electron)

Status: proposal, not built. Owner goal: "move to an Electron app, and ship apex-router as a
standalone app, with installer, debugger, updater, uninstaller".
Scope: macOS (Apple Silicon) and Linux (D13). All data stays on the machine, except an opt-in crash upload (D14, D17).
Marks: **[check before build]** = a version, policy or API claim from memory. Confirm it before Phase 1.

## 1. Today: what an install creates and changes

Sources: `install.sh`, `src/apex_router/watch.py`, `mesh/launchd.py`, `scripts/az-token-keepwarm.sh`,
and the plists on the owner Mac (names and `ProgramArguments` only). This table is the uninstaller list.

### 1.1 launchd user agents (`~/Library/LaunchAgents/<label>.plist`)

| Label | Program | Run | Made by |
|---|---|---|---|
| `com.apex-router.serve` | `<venv>/python -m apex_router.cli serve` (:8788) | RunAtLoad, KeepAlive | `watch install` |
| `com.apex.local-qwen-proxy` | `<venv>/python3 -m apex_router.cli serve --port 8789 --host 127.0.0.1` | RunAtLoad, KeepAlive | by hand (owner) |
| `com.apex-router.daily` | `<venv>/python -m apex_router.watch --run-daily` | calendar | `watch install` |
| `com.apex-router.drain` | drain worker | KeepAlive | `watch install` (not with Ornith worker) |
| `com.apex-router.collect` | `<venv>/python -m apex_router.agent_runs collect` | every 1800 s | owner Mac |
| `com.apex-router.snapshot` | `<venv>/python -m apex_router.cli worldmodel snapshot` (transcript mirror) | daily 03:17 | `watch install-snapshot` |
| `com.apex-router.mesh-up` | `~/.local/bin/apex-router mesh up` (UDP 47777, HTTP 47778) | RunAtLoad, KeepAlive | `mesh/launchd.py` |
| `com.apex-router.mesh-worker` | `~/.local/bin/apex-router mesh worker` | RunAtLoad, KeepAlive | `mesh/launchd.py` |
| `com.ornith.worker`, `com.ornith.overnight` | `<venv>/python -m apex_router.ornith.*` | idle / 01:30 | `--ornith-serve` |
| `com.apex.ornith-drain`, `com.apex.ornith-daily` | other venv / `~/.apex-router/daily_report.sh` | KeepAlive / calendar | by hand (owner) |
| `com.apex.az-token-keepwarm` | `/bin/bash ~/.local/bin/az-token-keepwarm run` | every 60 s | `--az-keepwarm` |
| `com.apex.ollama-keepwarm` | `scripts/ollama-keepwarm.sh` | every 240 s | `--ollama-keepwarm` |

Finding: the owner plists point at three different Python homes (dev checkout venv, `~/.local/bin`,
`~/dev/ml/.venv`). There are also `*.plist.bak-*` files. Migration must handle all of them.

### 1.2 Files, settings, PATH, ports

| Item | Path | Change |
|---|---|---|
| Code + venv | `~/.apex-router` (git clone) + `.venv` | created |
| Install pointer | `~/.config/apex-router/install_dir` | created |
| CLI on PATH | `~/.local/bin/apex-router` (uv tool), `~/.local/share/uv/tools/apex-router` | created |
| Other shims | `~/.local/bin/az-token-cached`, `az-token-keepwarm`, `booksearch` | created (opt-in) |
| Data (router) | `~/.apex-router/` (tables, logs, route_log, widget, studio, mesh, queue, transcripts, ornith.env, models.json) | created |
| Data (proxy) | `~/.apex/` (telemetry, SQLite with request content), `~/.apex-local/` | created |
| pi extensions | `~/.pi/agent/settings.json` `packages[]`: apex-route, apex-nudges, apex-ste, apex-hangguard, apex-agents, apex-compact (+ booksearch, learn) | entries added |
| pi models | `~/.pi/agent/models.json` | seeded only if absent |
| Claude settings | `~/.claude/settings.json` hooks + proxy env (`.apex-bak` backup) | merged |
| Claude rules | `~/.claude/CLAUDE.md` block between `apex-router:ste:begin/end` | inserted |
| Claude commands | `~/.claude/commands/books.md` | created (opt-in) |
| Claude plugin | marketplace `datapce` (path in the clone), plugin `datapce@datapce` | registered |
| Git hooks | `<repo>/.git/hooks/post-commit` symlink | created (opt-in) |
| SwiftBar | user symlink to `integrations/swiftbar/apex.1m.sh` | by hand |
| Ollama models | `nomic-embed-text`, Ornith tiers (5.6 GB / 21 GB) | pulled |
| Brew / uv | `python@3.12`, `ollama`, uv | maybe installed |
| Ports | 8788 serve, 8789 local proxy, 8790+ studio (free port), 11434 ollama, 47777/47778 mesh | bound |

Keys: the proxy gets Azure tokens from `az-token-cached` and keys from env. `watch install`
copies an allow-list of serve env keys into the serve plist. The app must keep this rule.

## 2. Architectures

| Criterion | (a) Electron + bundled Python runtime | (b) Electron over separate uv/Homebrew install | (c) Swift menu-bar app + web studio |
|---|---|---|---|
| Size (download) | ~250-350 MB (Electron ~100 MB + CPython + proxy/tuner wheels) | ~100 MB app + uv env | ~5-15 MB app + uv env |
| Start time | 1-2 s window; tray at once | same | < 0.5 s |
| Update path | one signed update for app + runtime + pi extensions | two paths: app updater + `uv`/`brew` | Sparkle for app + `uv` for backend |
| Signing | every `.so`/`.dylib` in runtime must be signed + notarized | app only; backend not signed by us | app only |
| Security | big runtime; needs Electron hardening | same app surface; backend from PyPI/git, not pinned by us | smallest surface; WKWebView |
| Teammate install | no Python, no uv, no brew needed | needs uv + Python first | needs uv + Python first |
| Version skew | none: app and backend ship as one | app and backend can differ | can differ |
| Effort (person-days) | ~35 | ~20 | ~25 (Swift skill needed) |
| Fit with pi/codex | CLI shim to bundled runtime; pi extensions shipped and versioned | as today | as today |
| Matches owner goal | yes | partly (not standalone) | no Electron |

## 3. Recommendation: (a), with the runtime outside the `.app`

The app ships the Python backend and the pi extensions as one signed, versioned payload.
On first run and on each update, the app copies the payload to
`~/Library/Application Support/apex-router/runtime/<version>/` and points `runtime/current` at it.
launchd jobs, the PATH shim and pi `packages[]` use `runtime/current`, never the `.app` path.
Reasons: (1) an app update does not break the active jobs; (2) rollback is one link change;
(3) a deleted `.app` leaves the jobs active until the uninstaller runs; (4) dev mode can point
`current` at a dev venv.

### 3.1 Process model

| Process | Owner | Why |
|---|---|---|
| `serve` :8788, local proxy :8789 | launchd (KeepAlive) | on the request path of every agent; must live without the app |
| collect, daily, snapshot, ornith worker/overnight, az/ollama keepwarm | launchd (interval/calendar) | background; runs at login without the app |
| mesh-up, mesh-worker | launchd, off by default | opt-in LAN feature |
| studio server | app (child process, `apex-router studio --no-open --idle-exit 0`) | UI only; stops with the app |
| Tray data | app: runs `apex-router snapshot --json` each 60 s | replaces the SwiftBar plugin |
| ollama | not ours | brew or Ollama.app; the app only checks it |

Rules:
1. The app writes plists from one template module. It does not hand-edit plists.
2. Each app plist carries `AssociatedBundleIdentifiers` = the app bundle id. **[check before build]**
   This shows "apex-router" in System Settings > Login Items, not "bash" or "python".
3. Labels stay the same as today. An app-owned plist has the key `ApexOwner=app` (a custom key).
4. The app uses `launchctl bootstrap|bootout|kickstart gui/<uid>/<label>`. It does not use `launchctl load`.
5. Do not use SMAppService: it needs the program inside the bundle. **[check before build]**

### 3.2 Python runtime and pinning

1. Use a relocatable CPython (python-build-standalone, 3.12.x, arm64). **[check before build]**
2. Add a `uv.lock` to the repo. Today there is none. Build with `uv sync --frozen` and hashes.
3. Ship extras `proxy,tuner`. Keep `worldmodel`, `books` as in-app opt-in downloads (decision D5).
4. Record CPython version, lock hash and git commit in `runtime/<v>/BUILD.json`.
5. Sign every Mach-O in the payload with Developer ID + hardened runtime. Notarize the whole app.
6. The CLI shim `~/.local/bin/apex-router` execs `runtime/current/bin/apex-router`. Same for `az-token-cached`.

### 3.3 Tray (replaces SwiftBar)

- One dot: pressure colour + active agent count. This is the same data as `snapshot --menubar`.
- Menu: Agents, Quality (24 h), System, Proxy, "Open studio", "Diagnostics", "Check for updates", "Quit".
- Use `snapshot --json`. Do not parse SwiftBar text. Keep the 1.2 s refresh deadline.
- The app does not delete the SwiftBar plugin. The wizard offers to remove the symlink (decision D10).

### 3.4 Studio as the main window

- The BrowserWindow loads `http://127.0.0.1:<port>/` from the child studio.
- The app reads port and token from `instance.json` (mode 0600). It reuses a live studio (`/api/ping`).
- The app locks window navigation to that origin. Other links open in the default browser.
- Studio keeps its own rules: Host/Origin check, CSP nonce, 64 KiB JSON, secret names show "set".

### 3.5 Electron security

| Control | Setting |
|---|---|
| Renderer | `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`, `webviewTag: false` |
| Navigation | `will-navigate` and `setWindowOpenHandler`: deny all but the studio origin |
| Permissions | `setPermissionRequestHandler`: deny all (camera, mic, geo, notifications) |
| CSP | studio CSP stays; app pages use `default-src 'self'`, no remote content |
| Fuses | RunAsNode off, NodeOptions off, CLI inspect off, asar integrity on, only-load-from-asar on **[check before build]** |
| Token | the main process injects `X-Studio-Token` for the studio origin only (`webRequest.onBeforeSendHeaders`); later remove the token from the URL |
| Preload API | `app.version()`, `diag.run()`, `diag.bundle()`, `logs.tail(job)`, `update.check()`, `update.apply()`, `jobs.restart(label)` |
| IPC | each handler checks sender origin and argument schema; `label` must be in the app label list |
| Not exposed | no file read/write, no shell, no `exec`, no env, no keys |
| Single instance | `app.requestSingleInstanceLock()` |
| DevTools | off in release builds; on in safe/debug mode only |

## 4. Installer

Form: signed + notarized DMG with drag to Applications. **[check before build]** for `notarytool` and
the staple step. Nothing needs root, so no PKG scripts. A PKG for MDM is decision D2.

### 4.1 First-run wizard

1. Detect: an old `~/.apex-router` clone, uv tool, every label from 1.1, used ports, `pi`,
   `claude`, `codex`, `ollama`, `az`. Show what it found.
2. Ask: components (each a checkbox, defaults in bold):
   **proxy serve**, local proxy :8789, **pi extensions**, **Claude plugin**, **STE block in CLAUDE.md**,
   **watchers**, az keepwarm, ollama keepwarm, Ornith tier (none/small/large), mesh (off).
3. Ask: serve env. Read the allow-listed key names from a login shell (`zsh -lc`).
   Show only "set / not set". Never show values. Never ask for a key; keys come from Azure and env.
4. Preview (dry run): a list of every file write, plist, settings key, PATH shim and port.
   Show a diff for each JSON/Markdown file. "Copy as text" is available.
5. Apply: back up each changed file to `Application Support/apex-router/backups/<ts>/`.
   Write a manifest `install-manifest.json`: item, path, sha256 before/after, owner.
6. Check: run `apex-router verify`, `/healthz` on 8788, pi sees the extensions. Show the result.

### 4.2 Migration from an `install.sh` setup

1. Find plists by label prefix (`com.apex`, `com.apex-router`, `com.ornith`) and by
   `ProgramArguments` that contain `apex_router` or `apex-router`.
2. Show each one: label, current program, proposed program, "adopt" or "leave".
3. Adopt: bootout, rewrite `ProgramArguments` to `runtime/current`, keep env keys, bootstrap, health check.
4. Leave: the app shows the job but does not change it. Use this for a dev checkout.
5. pi: rewrite `packages[]` paths from the clone to `runtime/current/pi/<ext>.ts`. Keep order and other entries.
6. Keep `~/.apex-router` data. Do not delete the clone. Offer "remove old clone" later.
7. Replace the uv tool shim only after the new shim passes `apex-router status`.

Owner Mac: use "dev mode": `runtime/current` -> the dev checkout venv (decision D7).

## 5. Updater

| Topic | Design |
|---|---|
| Tool | electron-builder + electron-updater (Squirrel.Mac under it) **[check before build]** |
| Feed | GitHub Releases or a private static feed (decision D4); `latest-mac.yml` with sha512 |
| Channels | `stable`, `beta`; owner on `beta` |
| Check | at start + each 6 h; never in safe mode |
| Signature | Squirrel checks the code signature (same Team ID); we also check an ed25519 signature of the runtime payload |
| Payload | one release = app + runtime + pi extensions + Claude plugin; versions move together |
| Rollback | keep the last 2 `runtime/<v>/` dirs; "Roll back" moves `current` and restarts jobs; app downgrade needs `allowDowngrade` on the feed **[check before build]** |
| Policy | download in background; apply on user OK (decision D12) |

Update steps while agents run:
1. Download and check signatures. Unpack to `runtime/<new>/`. Run `runtime/<new>/bin/apex-router verify`.
2. Copy pi extensions to `runtime/<new>/pi/`. Copy the Claude plugin to `runtime/<new>/plugin/`.
3. Wait for the restart gate of each proxy: 0 in flight, idle 120 s (max wait 30 min). Use `studio/restart.py`.
4. Drain: `kill -USR1 <pid>` (pid from `launchctl print`). Wait for `inflight` = 0 (max 30 s).
5. Move `current` to `<new>`. Bootout + bootstrap serve, then the local proxy.
6. If `/healthz` fails in 25 s: move `current` back, restart, show the error.
7. Restart the interval jobs at their next run (no action). Restart mesh jobs if enabled.
8. Tell the user: "run `/reload` in pi". pi extension paths do not change, so pi settings stay.
9. Restart the app last. The app is not on the request path.

The app does not restart a proxy without `activity` in `/stats`. It asks the user to restart it when quiet.

## 6. Uninstaller

Start points: app menu "Uninstall…", and `apex-router uninstall` (copied to `Application Support`, so it
runs after the user deletes the `.app`).

1. Read `install-manifest.json`. Also scan by label prefix for items not in the manifest; mark them "found".
2. Show the list with a checkbox per item. "Found" items are off by default.
3. Ask about data: keep all (default) / delete / export to a zip, then delete (decision D8).
   WARNING: `~/.apex/` holds request content. `~/.apex-router/transcripts` holds prompt text.
   `~/.apex-router/mesh` holds the mesh secret.
4. Drain the proxies (same gate as updates). Ask before you stop a proxy with requests in flight.
5. `launchctl bootout gui/<uid>/<label>` for each label. Delete the plists. Keep `*.bak-*` unless chosen.
6. pi `settings.json`: remove only our `packages[]` entries. Do not restore a whole old file.
7. `~/.claude/settings.json`: remove only our hooks and proxy env keys. `CLAUDE.md`: remove the marked block.
8. `models.json` (pi): delete only if the sha256 is still the seeded one; else list it.
9. Remove shims in `~/.local/bin` only if they are ours (symlink target or header check).
10. Remove our git `post-commit` symlinks. Run `claude plugin uninstall datapce@datapce` and remove the marketplace.
11. Ollama models: list name and size. Delete only on explicit choice.
12. Do not touch uv, brew, Python, ollama, pi, claude, codex, az or SwiftBar.
13. Delete `runtime/`, the CLI shim, then the `.app` (move to Trash).
14. Write a report: each item, action (removed / kept / failed), path. Save it to `~/Desktop` and show it.

The app offers a whole-file restore from backup only when the current file hash equals the post-install hash.

## 7. Debugger

### 7.1 Diagnostics view

| Panel | Content |
|---|---|
| Jobs | label, loaded, pid, last exit code, port, `/healthz`, owner (app/found) |
| Versions | app, runtime, CPython, lock hash, pi extensions, Claude plugin, pi, claude, codex, ollama, macOS |
| Health checks | port 8788/8789 owner; `apex-router` on PATH resolves to `current`; plist `plutil -lint`; plist program exists; `az-token-cached` fresh (age only); ollama `/api/tags`; pi `packages[]` paths exist; disk free for `~/.apex` |
| Live log | tail of `~/.apex-router/logs/<label>.{out,err,log}`; job filter; pause; redaction on |
| Actions | restart job (with gate), run `apex-router verify`, `proxy doctor`, open logs folder |

### 7.2 Support bundle

1. Collect: last 2000 log lines per job, `status --json`, `verify`, `proxy doctor`, versions,
   plists with env values replaced by "set", key names of each settings file, the manifest.
2. Never collect: prompt or answer text, telemetry rows, `~/.apex` SQLite, `~/.apex-router/transcripts`, keys, tokens.
3. Redact: values of names with KEY, TOKEN, SECRET, PASSWORD, AUTH; bearer and JWT patterns;
   `$HOME` -> `~`; host names from mesh peers.
4. Show the bundle file list and a text preview. Save a zip on user OK. No upload.

### 7.3 Safe mode

Start: hold Option at app start, or `--safe-mode`.
- No updater, no job restart, no plist write. Studio does not apply edits (`apply` off).
- The app shows the jobs, logs and diagnostics. DevTools is on.
- A button prints the pi/claude settings change to bypass the proxy. The app does not apply it (decision D11).

## 8. Phases

| Phase | Work | Effort (pd) | Exit criteria |
|---|---|---|---|
| P0 Spike | Electron shell, studio window, bundled CPython, sign + notarize one test build | 4 | notarized DMG opens on a clean Mac; `runtime/current/bin/apex-router verify` passes |
| P1 Runtime | `uv.lock`, payload build script, `runtime/<v>` + `current`, CLI shim, plist template | 5 | serve runs from `current`; tests pass in the bundled runtime |
| P2 Tray + window | tray from `snapshot --json`, studio window, preload API, Electron hardening | 5 | tray matches SwiftBar data; security checklist 3.5 all green |
| P3 Installer | wizard, dry run, manifest, backups, migration of `install.sh` setups | 7 | fresh install < 10 min; owner Mac migrates with dry run first, no agent error |
| P4 Updater | feed, signatures, staged runtime, restart gate, rollback | 5 | update under load: 0 failed requests; rollback in < 1 min |
| P5 Uninstaller | manifest walk, scan, data choice, report | 3 | clean Mac after uninstall = before install (file + launchd diff empty) |
| P6 Debugger | diagnostics, log tail, support bundle, safe mode | 4 | secret scan of a bundle finds 0 values; safe mode writes 0 files |
| P7 Beta | 3 teammates, owner on beta channel, docs | 3 | 2 weeks, no P1 bug; RUNBOOK-update.md replaced |
| Total | | ~36 | |

## 9. Risks

| Risk | Effect | Treatment |
|---|---|---|
| Notarization of the Python runtime (many `.so`, hardened runtime, JIT/dyld entitlements) | build blocked | P0 spike first **[check before build]** |
| Finder-started app has no shell env | serve misses keys | read allow-listed names via login shell; show "not set" |
| Update while agents run | failed agent turns | restart gate + drain + health rollback |
| Two owners of one label (`install.sh` and app) | jobs fight | `ApexOwner` key; `install.sh` refuses app-owned labels |
| User deletes the `.app` only | jobs run without UI | runtime lives outside the bundle; CLI uninstaller stays |
| macOS "background item added" notices | user confusion | `AssociatedBundleIdentifiers`; fewer plists **[check before build]** |
| Writable runtime dir under `~/Library` | code swap by local malware | same trust as today; check payload hash at app start |
| Electron CVEs | attack surface | no remote content; update Electron each minor **[check before build]** |
| Download size ~300 MB | slow first install | optional extras as later downloads |
| Owner dev setup has 3 Python homes | migration breaks jobs | "leave" option; dev mode |

## 10. Owner decisions (answered 2026-10-27)

| ID | Question | Answer |
|---|---|---|
| D1 | Architecture | **(a)** Electron + bundled Python runtime |
| D2 | Package | **DMG only** |
| D3 | Signing | **Not signed; beta only** |
| D4 | Update feed | **Public GitHub Releases** |
| D5 | Default runtime extras | **`proxy,tuner,worldmodel`**; `books` as a later download |
| D6 | `install.sh` | **Keep supported for one release, then freeze** (suggested answer) |
| D7 | Owner Mac | **Dev mode**: `runtime/current` -> the dev checkout |
| D8 | Uninstall, user data | **Ask, with no default** |
| D9 | Mesh in the wizard | **Advanced option** |
| D10 | SwiftBar | **Remove at P2** |
| D11 | Safe mode, proxy bypass | **Only show the change** |
| D12 | Updates | **Always ask** |
| D13 | Platforms | **Apple Silicon + Linux** |
| D14 | Crash reports | **Opt-in upload** |

### Effects on this note

1. D3 "not signed": remove sign and notarize from P0 and section 4. The P0 exit is "the DMG opens on a clean
   Mac after the user allows it in System Settings > Privacy & Security". Gatekeeper shows a warning on
   first open of a download.
2. D3 + D12: Squirrel.Mac needs a signed app, so electron-updater cannot install updates by itself on an
   unsigned Mac app. The updater checks the feed, makes sure the ed25519 signature of the runtime payload is correct,
   and asks the user (D12). The `.app` shell itself updates by a new DMG download (see D15).
3. D4 public feed: release notes and payloads are public. Do not put internal hostnames, model ids or
   telemetry samples in a release. Add a release check for this to P4.
4. D5: the runtime is bigger (`worldmodel` adds the torch-free parts only; **[check before build]** size).
5. D13 Linux: the scope line "macOS only" no longer holds. Linux needs systemd user units in place of
   launchd, its own package and its own tray (see D16). The phase plan grows.
6. D10: SwiftBar goes at P2, so the app menu bar must reach parity with `snapshot --menubar` in P2.
7. D14 opt-in upload needs a destination (see D17).

### Second round (answered 2026-10-27)

| ID | Question | Answer |
|---|---|---|
| D15 | Unsigned Mac shell update | **(a)** a new DMG download when the shell changes; runtime updates stay in the app |
| D16 | Linux package | **openSUSE**: an `.rpm` (zypper), systemd user units, after the Mac beta (suggested timing; owner did not set it) |
| D17 | Crash report destination | **A cleaned GitHub issue on the public repo + a local zip** |

### Effects of the second round

1. D15: the app checks the GitHub Releases feed for a new shell version and shows "Download the new
   app". It does not replace the `.app` by itself.
2. D16: electron-builder target `rpm`. Test on openSUSE Tumbleweed and Leap. Tray: the KDE system tray
   (StatusNotifierItem). **[check before build]** the GNOME tray needs an extension.
3. D17: the public issue is the riskier path. Rules:
   1. The app makes the report from an allow-list of fields (app version, OS, crash type, stack frames of our
      own code with repo-relative paths). Nothing else goes in: no hostnames, model ids, usernames, home
      paths, telemetry rows, prompt text or env values.
   2. The cleaner runs the `test_public_hygiene` digest list and a secret pattern scan. Any hit blocks
      the upload; the local zip is still made.
   3. The user sees the full issue text and must press "Open issue". The app opens a prefilled GitHub
      page in the browser; it holds no GitHub token.
   4. The local zip holds the full report and stays on the machine until the user sends it.
