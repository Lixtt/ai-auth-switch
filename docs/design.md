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

The Codex provider does not modify:

- `config.toml`
- `history.jsonl`
- `sessions/`
- `skills/`
- SQLite/WAL locations
- proxy settings

This keeps Codex's own configuration layout intact.

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
