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
  active Codex CLI access token.
- OpenClaw is pointed at `openai-codex:default`, which is the Codex CLI auth
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

The deprecated `--hermes-login` flag is accepted for compatibility but does not
start a separate Hermes device-code login:

```bash
ai-auth-switch auth sync codex --hermes-login
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
