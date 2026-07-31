# Design

`ai-auth-switch` separates auth profile management from command wrapping.

## Auth Management

The auth layer stores per-provider profiles under:

```text
~/.local/share/ai-auth-switch/profiles/<provider>/<profile>.json
```

For Codex, activating a profile replaces only:

```text
$CODEX_HOME/auth.json
```

with a symlink to the selected profile. The symlink matters because Codex can
refresh OAuth tokens while running; refresh writes then update the selected
profile instead of a detached copy.

Some Codex versions refresh by atomically replacing `auth.json`, which can
break the symlink. Before switching away, `ai-auth-switch` matches the active
auth file back to a saved profile by stable Codex account identity and syncs
the replacement file into that profile.

A profile can be selected implicitly:

- `defaults.json` stores the per-provider default profile (`auth default`).
- `.ai-auth-switch.json` in a directory binds a profile to that project tree
  (`auth bind`); bindings resolve from the nearest ancestor directory.
- `run` without an explicit profile falls back to the default, then to the
  nearest directory binding, then reports both options.

Profiles can be migrated between machines with `auth export` / `auth import`.
The export is a JSON object (`{"version": 1, "providers": {...}}`) that import
re-serializes with private permissions. Import skips existing names unless
`--force` is given, and reads `-` from stdin so
`auth export | auth import -` works without an intermediate file.

The Codex provider does not modify:

- `config.toml`
- `history.jsonl`
- `sessions/`
- `skills/`
- SQLite/WAL locations
- proxy settings

This keeps Codex's own configuration layout intact.

## Dependent Tool Sync

Some local tools intentionally consume the active Codex auth. After Codex auth
is saved, logged in, or permanently switched, `ai-auth-switch` syncs those
dependent tool states:

- Hermes is pointed at `openai-codex` and switched to Hermes's
  `openai_runtime=auto` path with a credential-pool entry seeded from the
  active Codex CLI access token. If `hermes-gateway.service` is active, it is
  restarted so messaging channels such as Feishu reload auth and environment.
- Current OpenClaw installs are synchronized through the SQLite auth store by
  writing `openai:default` from the active Codex CLI OAuth token. Older JSON
  auth-state installs fall back to `openai-codex:default`, the legacy Codex CLI
  bridge profile.
- If `openclaw-gateway.service` is active, it is restarted so the new auth is
  picked up immediately.

OpenAI Codex refresh tokens are single-use/rotating credentials. Sharing one
refresh token between Codex CLI and Hermes can trigger refresh-token-reuse
errors. For that reason, Hermes sync does not copy Codex CLI OAuth tokens into
Hermes and no longer runs Hermes's own Codex login flow. Instead it removes the
old Hermes-owned `openai-codex` OAuth state, installs only the current Codex CLI
access token as a pool entry, sets Hermes's active provider to `openai-codex`,
and keeps `openai_runtime=auto` so Hermes handles the turn itself.

The same operation can be run explicitly:

```bash
ai-auth-switch auth sync codex
```

Before restarting active systemd user gateway services, the sync imports the
current process's standard proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY`, and lowercase variants) into the systemd user manager
and unsets absent proxy variables there. This lets Hermes/OpenClaw inherit the
caller's proxy environment instead of depending on a fixed `EnvironmentFile`.

For OpenClaw, the sync prefers `~/.openclaw/agents/main/agent/openclaw-agent.sqlite`
when it exists. It updates `auth_profile_store` and `auth_profile_state`, sets
`order.openai` and `lastGood.openai` to `openai:default`, and removes stale
`usageStats` for that profile so a previous auth failure cooldown does not keep
blocking the freshly synced token. If the SQLite store is absent, the sync uses
the older `auth-profiles.json` / `auth-state.json` files instead.

The deprecated `--hermes-login` flag is accepted for compatibility but does not
start a separate Hermes device-code login. `--no-hermes-restart` can be used on
manual syncs when the gateway should be left running:

```bash
ai-auth-switch auth sync codex --hermes-login
ai-auth-switch auth sync codex --no-hermes-restart
```

## Wrapper

The wrapper layer does not replace the normal active auth. It creates a private
temporary `CODEX_HOME`, links every existing non-auth entry from the normal
Codex home into it, and links the selected profile as that temporary home's
`auth.json`:

```bash
ai-auth-switch run codex person@example.com -- codex -C ~/workspace/project
```

The child process receives the temporary `CODEX_HOME`. It retains an existing
`CODEX_SQLITE_HOME`, or points SQLite state back to the normal Codex home when
that variable is unset. Consequently config, history, sessions, skills, logs,
caches, plugins, and SQLite state remain shared while credentials are isolated.
The temporary home itself uses the machine-local per-user runtime directory by
default (`XDG_RUNTIME_DIR`, with a `/var/tmp` fallback);
`AI_AUTH_SWITCH_RUNTIME_DIR` can select a different parent. This keeps
per-process symlink creation and cleanup off a cross-worker shared filesystem
without placing `CODEX_HOME` below the system temporary directory, where Codex
refuses to install helper binaries.

Codex can refresh OAuth credentials by atomically replacing `auth.json`. The
wrapper copies that replacement back to the selected saved profile before
removing the temporary home. A profile-scoped lock protects only wrapper-side
credential installation and reconciliation. It is released while the child
runs, so processes using either the same or different profiles can run
concurrently. Each session has a private home but same-account sessions link to
the same saved credential file, preventing them from blocking one another while
keeping their rotating credential state shared. If Codex atomically replaces a
session's auth symlink,
reconciliation skips an unchanged stale credential and requires the provider
identity to still match the destination profile. A mismatched candidate is
preserved under `backups/<provider>/rejected/` and the overwrite is refused.

Profile command dispatch and other read-only commands only read atomically
written state and do not take the global auth-management lock. Permanent
profile mutations still use the global lock, but no profile-scoped Codex
process holds it for its lifetime.

For editable installs, numbered aliases point at the checkout's bundled
launcher instead of a machine-local `/usr/local/bin` entry point. Existing
managed links are atomically migrated when their target changes. This matters
when `~/.local/bin` is shared by multiple workers but `/usr/local` is not.

Dependent Hermes/OpenClaw synchronization remains attached to permanent active
auth changes. A profile-scoped run does not change those global integrations.

## Provider Boundary

A provider should only define:

- the active auth file path
- the login command
- how to infer a readable profile name from an auth file

Providers should not own application-wide config rewriting. If a future tool
needs provider/model/proxy switching, that should be a separate integration
layer rather than mixed into auth profile switching.
