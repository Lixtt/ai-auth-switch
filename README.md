# ai-auth-switch

Switch auth profiles for AI coding agents while keeping the app's normal
configuration, history, sessions, and cache layout unchanged.

The first provider is Codex. The design keeps Codex itself as the source of
truth for everything except the active auth file:

- `~/.codex/config.toml` is not rewritten.
- `~/.codex/history.jsonl`, `sessions/`, `skills/`, and other Codex state stay in place.
- `auth.json` is the only active Codex file switched.
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

- Hermes is switched to the independent Hermes Codex session saved for the
  active Codex profile.
- OpenClaw is pointed at its Codex CLI default profile.

You can run that step explicitly too:

```bash
ai-auth-switch auth sync codex
```

`ai-auth-switch auth login codex` also runs Hermes's own Codex device-code
login for the same profile. That creates a separate Hermes OAuth session under:

```text
~/.local/share/ai-auth-switch/dependent-auth/hermes/codex/<profile>.json
```

Hermes does not import or share the Codex CLI refresh token. Codex, Hermes, and
OpenClaw can refresh their own auth state without rotating the same refresh
token out from under another tool.

If a Codex profile was created before Hermes sync existed, add the Hermes
session once:

```bash
ai-auth-switch auth use codex <profile>
ai-auth-switch auth sync codex --hermes-login
```

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

Run Codex with a profile for the lifetime of one process, then restore the
previous active auth:

```bash
ai-auth-switch run codex someone@example.com -- codex -C ~/workspace/project
```

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
- Dependent sync: activate each profile's independent Hermes Codex session and
  point OpenClaw at the Codex CLI bridge profile.
- Wrapper: run a command under a selected profile without permanently changing
  the active profile after the command exits.

Provider support is intentionally small. A provider only needs to define where
its active auth file lives, how to infer a profile name, and which login command
should be run for interactive login.
