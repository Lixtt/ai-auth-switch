# ai-auth-switch

Switch auth profiles for AI coding agents while keeping the app's normal
configuration, history, sessions, and cache layout unchanged.

Codex and Claude Code are supported. The design keeps each agent as the source
of truth for everything except the active auth file:

- `~/.codex/config.toml` is not rewritten.
- `~/.codex/history.jsonl`, `sessions/`, `skills/`, and other Codex state stay in place.
- Permanent profile changes switch only `auth.json`.
- Profile-scoped runs isolate `auth.json` in a temporary `CODEX_HOME` while
  sharing the normal Codex configuration and state.
- Saved profiles live outside Codex under `~/.local/share/ai-auth-switch/`.
- Hermes and OpenClaw Codex-dependent auth state is synchronized after Codex
  auth changes.
- Claude Code profiles isolate `.credentials.json` while sharing the normal
  settings, history, sessions, skills, plugins, and cache layout.

## Installation

Install or upgrade the latest stable release from PyPI:

```bash
python -m pip install --upgrade ai-auth-switch
```

Python 3.10 or newer is required.

To install the newest source directly from the official GitHub repository:

```bash
python -m pip install --upgrade "ai-auth-switch @ git+https://github.com/Lixtt/ai-auth-switch.git"
```

`ais` is the short command name for `ai-auth-switch`; both accept exactly the
same arguments. Examples below use the long name for clarity.

For development, clone the official repository and use an editable install:

```bash
git clone https://github.com/Lixtt/ai-auth-switch.git
cd ai-auth-switch
python -m pip install -e .
```

## Codex Usage

Save the currently active Codex login:

```bash
ai-auth-switch auth save codex
```

The profile name is inferred from the email inside the Codex OAuth token when
available. If the token does not expose an email, the fallback is
`chatgpt-<account-id-prefix>`.

Login a new Codex account and save it:

```bash
ai-auth-switch auth login codex
```

Optionally force a profile name:

```bash
ai-auth-switch auth login codex work
```

List and switch profiles:

```bash
ai-auth-switch auth list
ai-auth-switch auth list codex
ai-auth-switch auth list codex --usage
ai-auth-switch auth use codex someone@example.com
ai-auth-switch auth current codex
```

Add `--usage` to query every saved Codex account's current rate-limit windows
in parallel. Each request uses that profile's own access token and explicit
ChatGPT account ID, so limits cannot be accidentally attributed to another
saved account. The normal list remains local and instant; usage lookup is
opt-in because it requires network access and may report an expired login.

```text
* someone@example.com [codex1] (plus, 5h 72% left, resets 2026-08-19T20:30:00+08:00 (in 2h 5m), 168h 41% left, resets 2026-08-26T06:00:00+08:00 (in 6d 23h))
  other@example.com [codex2] (team, 5h 18% left, resets 2026-08-19T19:15:00+08:00 (in 50m), 168h 83% left, resets 2026-08-25T18:00:00+08:00 (in 5d 23h))
```

Each rate-limit window shows its next reset in the local system timezone,
including its UTC offset, together with a relative countdown. JSON output keeps
the Unix `resets_at` value and also adds an ISO 8601 UTC `resets_at_iso` value
for stable programmatic use.

Results are cached for 60 seconds. Use `--refresh-usage` to bypass the cache,
`--usage-cache-ttl` to tune it, `--usage-timeout` for slow networks, and
`--usage-workers` to limit concurrency. A failure for one account is shown
inline without hiding results for the other accounts. The command deliberately
does not refresh expired OAuth tokens; run that profile through Codex or log in
again so rotating credentials remain coordinated safely.

For status bars, monitoring, or account schedulers, add `--json`. The JSON
contains profile identity, active/alias state, and structured usage windows
when `--usage` is also present:

```bash
ai-auth-switch auth list codex --usage --json
```

### Automatic Codex profile selection for CLI runs

Use `--auto` when starting Codex from a terminal to choose a saved account by
current quota instead of naming a profile:

```bash
ais run codex --auto
```

This runs the normal `codex` command with an isolated selected profile. To pass
an explicit child command or Codex arguments, place them after `--`:

```bash
ais run codex --auto -- codex -C ~/workspace/project
```

Automatic runs always exclude Free-plan profiles, as well as profiles whose
usage lookup reports expired authentication or no remaining quota. Selection
uses the lowest remaining percentage across returned rate-limit windows. It
also records a process lease for each running `--auto` command and divides
available capacity by the number of active leases, so several tasks started
together spread across accounts without changing the globally active desktop
account. The lease is removed when the child command exits; stale leases from
killed processes are pruned on the next selection.

Usage results are cached for 60 seconds by default. Tune or bypass that cache
when needed:

```bash
ais run codex --auto \
  --auto-usage-cache-ttl 15 \
  --auto-refresh-usage
```

`run --auto` is process-scoped and is intended for terminal tasks. ChatGPT
Desktop continues to use its globally active account; use the desktop idle
rotation feature below for that application.

### ChatGPT Desktop idle account rotation (Linux)

ChatGPT Desktop normally keeps one Codex app-server and one active account in
memory. Changing `auth.json` alone therefore does not reliably update a running
desktop session. On supported Linux desktop builds, install the managed-daemon
integration once:

```bash
ais desktop auto install
```

Then fully close and reopen ChatGPT Desktop once. The installer starts a local
managed app-server, adds a per-user `chatgpt.desktop` launcher override with
`CODEX_APP_SERVER_USE_LOCAL_DAEMON=1`, and enables a systemd user service. It
does not rewrite `~/.codex/config.toml`.

The worker polls app-server thread state and never switches while a turn is
active. After 60 continuous idle seconds, it queries every saved Codex
profile, excludes Free-plan, expired, or exhausted accounts, and switches only
when the current account has 10% or less remaining and another account improves
that by at least 5 percentage points. A 30-minute cooldown prevents flapping. The
worker checks for active turns again immediately before switching; if daemon
restart fails, it restores the previous profile.

```bash
ais desktop auto status
ais desktop auto status --json
ais desktop rotate --now
ais desktop auto disable
```

`desktop rotate --now` ignores the quota threshold and cooldown, but still
refuses to interrupt active desktop work. `desktop auto disable` restores a
pre-existing per-user ChatGPT launcher when one was present. Both install and
disable require one subsequent desktop restart to change connection mode.

Tune the policy during installation when needed:

```bash
ais desktop auto install \
  --idle-seconds 90 \
  --cooldown-seconds 3600 \
  --switch-below 15 \
  --min-improvement 10
```

This is global idle rotation, not per-thread account assignment. All turns in
one desktop app-server still share the selected account. The integration uses
the app-server Unix-socket transport documented by OpenAI and requires a
desktop build that exposes local-daemon mode.

After Codex auth is saved, logged in, or switched, `ai-auth-switch` also syncs
Codex-dependent local tools:

- Hermes is pointed at `openai-codex` and seeded with a Codex CLI access-token
  pool entry, so it follows the active Codex CLI account without handing turns
  to `codex app-server`. If `hermes-gateway.service` is active, it is restarted
  so Feishu and other messaging channels pick up the new auth immediately.
- Current OpenClaw installs are synchronized through the SQLite auth store by
  writing `openai:default` from the active Codex CLI OAuth token. Older JSON
  auth-state installs still use the legacy `openai-codex:default` bridge.

You can run that step explicitly too:

```bash
ai-auth-switch auth sync codex
```

Hermes does not import or share the Codex CLI refresh token. The sync clears
Hermes's old independent `openai-codex` OAuth state, installs the current Codex
CLI access token into Hermes's `openai-codex` credential pool, and leaves
Hermes's `openai_runtime` on `auto`. Current OpenClaw versions no longer import
Codex CLI auth from `~/.codex` at runtime, so the sync writes the active Codex
OAuth tokens into OpenClaw's own SQLite auth store as `openai:default` and
clears any failure cooldown for that profile. Older OpenClaw JSON auth-state
installs still fall back to the legacy `openai-codex:default` bridge profile.

The old Hermes login flag is kept only for command compatibility and is now a
no-op:

```bash
ai-auth-switch auth sync codex --hermes-login
```

Use `ai-auth-switch auth sync codex` normally. Before restarting active gateway
services, the current process's standard proxy variables (`http_proxy`,
`https_proxy`, and their uppercase variants) are imported into the systemd user
manager, so Hermes/OpenClaw do not need a hard-coded proxy env file. To leave a
running Hermes gateway untouched during an explicit sync, pass
`--no-hermes-restart`.

If Codex reports that a refresh token was already used after switching
profiles, that profile's stored refresh token has already been invalidated by
the server. Log in to that Codex account again and save it back into the same
profile name:

```bash
ai-auth-switch auth login codex <profile>
```

Recent versions sync Codex's atomically replaced `auth.json` back into the
managed profile before switching away, which prevents reactivating a stale
refresh token after Codex refreshes it.

On a fresh install, `auth list` can be empty even when Codex is already logged
in. Import the active Codex auth first:

```bash
ai-auth-switch auth save codex
```

If you run as another Unix user, make sure `CODEX_HOME` points at the Codex
config directory you actually use, or pass `--codex-home /path/to/.codex`.

Run Codex with isolated auth for the lifetime of one process. The default
active auth is never changed:

```bash
ai-auth-switch run codex someone@example.com -- codex -C ~/workspace/project
```

Numbered command aliases are managed automatically for every saved Codex
account. On the first sync, existing profiles are numbered in saved order;
later accounts are appended. Removing an account compacts the sequence, and
renaming an account keeps its number:

```bash
ai-auth-switch auth list codex
#   someone@example.com [codex1]
#   other@example.com [codex2]
```

The profile name is normally the authenticated email. If a credential file's
actual account differs, `auth list` shows it explicitly as
`(actual auth: ...)` instead of silently presenting a misleading `codexN`
mapping.

Saving, logging in, switching, renaming, or removing profiles updates the alias
records. For the default profile store, matching command links are also created
under `~/.local/bin` and stale links are removed. Run an explicit sync to
backfill existing accounts or to choose a different command directory:

```bash
ai-auth-switch alias sync codex
ai-auth-switch alias sync codex --bin-dir /path/on/PATH
```

After installation, `codex1 -C ~/workspace/project` runs the Codex CLI under
the corresponding profile without changing the account used by `codex2` or by
the default `codex` command. Numbered aliases can run concurrently, including
multiple processes using the same saved account. A per-profile lock is held
only for the wrapper's short credential installation and reconciliation
steps, not for the lifetime of the Codex process.

Each run gets a private temporary `CODEX_HOME` containing only its selected
`auth.json`. Existing entries from the normal Codex home—including
`config.toml`, `history.jsonl`, `sessions/`, `skills/`, logs, caches, and
plugins—are linked into that temporary home. An existing `CODEX_SQLITE_HOME`
is preserved; when it is unset, SQLite state points back to the normal Codex
home. If Codex refreshes and atomically replaces its isolated `auth.json`, the
new credentials are written back to that saved profile when the process exits.
Same-account processes reference the same saved profile file so they can
observe a refresh-token rotation performed by another Codex process. If Codex
atomically replaces a session's auth symlink, wrapper-side reconciliation back
to the profile is serialized, skips unchanged stale credentials, and refuses a
write-back whose actual account differs from the saved profile. Rejected
credentials are preserved under the profile store's `backups/codex/rejected/`
directory for inspection.

Temporary homes use the machine-local per-user runtime directory by default
(`XDG_RUNTIME_DIR`, with a `/var/tmp` fallback), so workers sharing the profile
store do not contend on `/mnt` for per-process symlink creation and cleanup.
Set `AI_AUTH_SWITCH_RUNTIME_DIR` to override the runtime parent when needed.

Names matching `codex1`, `codex2`, `claude1`, `claude2`, and so on are reserved
for automatic management. Other alias names can still be created manually with
`ai-auth-switch alias set` and `ai-auth-switch alias install`.

When `--store-dir` is passed, automatic command-link installation is skipped
to avoid changing the user's global bin directory. Pass `--bin-dir` to
`alias sync`, or set `AI_AUTH_SWITCH_ALIAS_BIN_DIR`, to opt into a specific
directory. Editable installs prefer the checkout's shared `bin/ai-auth-switch`
launcher, which keeps aliases portable when the home directory is mounted on
multiple machines. Set `AI_AUTH_SWITCH_ALIAS_TARGET` or pass `--target` to
choose another launcher explicitly.

## Claude Code Usage

Claude Code OAuth profiles support the same save, list, switch, default,
directory-binding, rename, remove, export, and import operations as Codex.
On Linux, Claude Code stores OAuth credentials in
`~/.claude/.credentials.json`; the official `CLAUDE_CONFIG_DIR` override is
also supported.

Import the currently stored Claude Code login:

```bash
ai-auth-switch auth save claude
```

The profile name is inferred from Claude Code's account metadata when an email
is available. You can always provide an explicit name:

```bash
ai-auth-switch auth save claude work
```

Log in to another Claude account and save it without disturbing existing
profiles:

```bash
ai-auth-switch auth login claude
ai-auth-switch auth login claude work -- --email someone@example.com
```

List, activate, and inspect profiles:

```bash
ai-auth-switch auth list claude
ai-auth-switch auth use claude someone@example.com
ai-auth-switch auth current claude
```

Numbered `claude1`, `claude2`, and so on aliases are installed and maintained
automatically. Each alias runs with a private temporary `CLAUDE_CONFIG_DIR`, so
multiple Claude accounts can run concurrently without changing the default
Claude login:

```bash
claude1 -p "review this repository"
claude2 --continue
ai-auth-switch run claude someone@example.com -- claude -p "summarize the tests"
```

The temporary config shares normal Claude Code state but isolates
`.credentials.json` and account metadata. Refreshed OAuth credentials are
written back to the selected profile with an account-identity check, preventing
an accidental `/login` from overwriting another saved account.

Claude Code gives environment credentials such as `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, and cloud-provider modes higher priority than saved
OAuth credentials. `claudeN` and `run claude` remove those overrides in the
child process so the selected OAuth profile wins. For permanent `auth use`
switches, unset those variables in your shell. API-key, Bedrock, Vertex,
Foundry, and `apiKeyHelper` profiles are not copied or managed.

Use a non-default config directory when needed:

```bash
ai-auth-switch --claude-config-dir /path/to/.claude auth list claude
```

File-based Claude OAuth profile management currently targets Linux. Claude Code
uses the encrypted macOS Keychain on macOS, which is intentionally not copied
by this tool; Windows isolated-run support has not yet been validated.

## Directory Overrides

By default Codex auth is read from:

```text
$CODEX_HOME/auth.json
```

or, when `CODEX_HOME` is unset:

```text
~/.codex/auth.json
```

Override it explicitly:

```bash
ai-auth-switch --codex-home /path/to/.codex auth list codex
```

The profile store can be moved with:

```bash
AI_AUTH_SWITCH_HOME=/secure/path ai-auth-switch auth list codex
```

Claude Code's config directory can be selected with `CLAUDE_CONFIG_DIR` or the
global `--claude-config-dir` option.

## Default Profile and Directory Binding

`run` normally needs an explicit profile name. When you omit it, the profile is
resolved from the provider's default profile first, then from the nearest
directory binding:

```bash
ai-auth-switch run codex -- codex -C ~/workspace/project   # still explicit
ai-auth-switch run codex                                    # uses default/binding
```

Set, show, and clear the default profile per provider:

```bash
ai-auth-switch auth default codex someone@example.com
ai-auth-switch auth default codex
#   default profile -> someone@example.com
ai-auth-switch auth default codex --clear
```

Directory bindings select a profile automatically for every `run` inside a
project tree. The binding is stored in `.ai-auth-switch.json` in the target
directory and resolved from the nearest ancestor:

```bash
cd ~/workspace/project
ai-auth-switch auth bind codex someone@example.com
ai-auth-switch auth bind codex
#   bound profile -> someone@example.com (resolved from /home/me/workspace/project)
ai-auth-switch auth bind codex --clear
```

Use `--dir` to bind a directory other than the current one. Bindings take
precedence over the default profile when both exist. If neither is set, `run`
without a profile prints a message explaining both options.

## Migrating Profiles Between Machines

Saved profiles (including their OAuth credentials) can be exported as JSON and
imported on another machine. This is useful when the checkout and home are not
shared:

```bash
# On the source machine.
ai-auth-switch auth export codex -o codex-profiles.json
ai-auth-switch auth export              # all providers, to stdout
```

The export file is written with private permissions (`0600`) and contains
credentials; keep it secure and delete it after migrating. Import it on the
target machine, or pipe it directly when the two machines can talk over SSH:

```bash
ai-auth-switch auth import codex-profiles.json
ai-auth-switch auth export | ai-auth-switch auth import -   # pipe, no file
```

Existing profiles with the same name are skipped to avoid clobbering local
state; pass `--force` to overwrite them. Imported profiles automatically get
their numbered aliases (`codex1`, `codex2`, `claude1`, `claude2`, ...) on the
target machine.

## Architecture

`ai-auth-switch` has three separate layers:

- Auth management: save, list, activate, rename, remove, and inspect profiles.
- Dependent sync: point Hermes and OpenClaw at the active Codex CLI auth.
- Wrapper: run a command in a profile-scoped Codex or Claude config directory
  without changing the default active profile or blocking other accounts.

Provider support is intentionally small. A provider only needs to define where
its active auth file lives, how to infer a profile name, and which login command
should be run for interactive login.
