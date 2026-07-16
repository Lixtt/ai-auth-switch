# ai-auth-switch

Switch auth profiles for AI coding agents while keeping the app's normal
configuration, history, sessions, and cache layout unchanged.

The first provider is Codex. The design keeps Codex itself as the source of
truth for everything except the active auth file:

- `~/.codex/config.toml` is not rewritten.
- `~/.codex/history.jsonl`, `sessions/`, `skills/`, and other Codex state stay in place.
- Permanent profile changes switch only `auth.json`.
- Profile-scoped runs isolate `auth.json` in a temporary `CODEX_HOME` while
  sharing the normal Codex configuration and state.
- Saved profiles live outside Codex under `~/.local/share/ai-auth-switch/`.
- Hermes and OpenClaw Codex-dependent auth state is synchronized after Codex
  auth changes.

## Install From Checkout

```bash
python -m pip install -e .
```

You can also run without installation:

```bash
./bin/ai-auth-switch --help
```

If the checkout and home directory are shared by multiple machines but
`/usr/local` is machine-local, install the shared launcher on `PATH` instead:

```bash
mkdir -p ~/.local/bin
ln -sfn "$PWD/bin/ai-auth-switch" ~/.local/bin/ai-auth-switch
hash -r
ai-auth-switch alias sync codex
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
ai-auth-switch auth use codex someone@example.com
ai-auth-switch auth current codex
```

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

Names matching `codex1`, `codex2`, and so on are reserved for automatic
management. Other alias names can still be created manually with
`ai-auth-switch alias set` and `ai-auth-switch alias install`.

When `--store-dir` is passed, automatic command-link installation is skipped
to avoid changing the user's global bin directory. Pass `--bin-dir` to
`alias sync`, or set `AI_AUTH_SWITCH_ALIAS_BIN_DIR`, to opt into a specific
directory. Editable installs prefer the checkout's shared `bin/ai-auth-switch`
launcher, which keeps aliases portable when the home directory is mounted on
multiple machines. Set `AI_AUTH_SWITCH_ALIAS_TARGET` or pass `--target` to
choose another launcher explicitly.

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

## Architecture

`ai-auth-switch` has three separate layers:

- Auth management: save, list, activate, rename, remove, and inspect profiles.
- Dependent sync: point Hermes and OpenClaw at the active Codex CLI auth.
- Wrapper: run a command in a profile-scoped Codex home without changing the
  default active profile or blocking runs of other accounts.

Provider support is intentionally small. A provider only needs to define where
its active auth file lives, how to infer a profile name, and which login command
should be run for interactive login.
