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
is saved, logged in, switched, or temporarily activated, `ai-auth-switch`
syncs those dependent tool states:

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

The wrapper layer temporarily activates a profile, runs a command, and restores
the previous active auth after the process exits:

```bash
ai-auth-switch run codex person@example.com -- codex -C ~/workspace/project
```

This is useful when a user wants a one-off profile without changing the default
active account for later shells.

## Provider Boundary

A provider should only define:

- the active auth file path
- the login command
- how to infer a readable profile name from an auth file

Providers should not own application-wide config rewriting. If a future tool
needs provider/model/proxy switching, that should be a separate integration
layer rather than mixed into auth profile switching.
