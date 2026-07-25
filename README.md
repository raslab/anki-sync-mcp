# anki-sync-mcp

**A headless, sync-native Anki MCP sidecar for always-on AI agents.**

`anki-sync-mcp` lets an MCP client safely manage a real Anki collection without Anki
Desktop or AnkiConnect. It runs as an authenticated Streamable HTTP service, uses Anki's
official collection and sync APIs, and synchronizes with AnkiWeb or a self-hosted Anki sync
server.

Unlike desktop bridges, it is designed to run unattended in Docker. Writes are serialized,
idempotent, persisted across restarts, and synchronized before and after mutation. Destructive
and schema-changing tools are disabled by default and use preview tokens plus verified backups
when enabled.

It supports decks, notes, cards, tags, note types, templates, media, scheduling controls,
backups, and explicit full-sync recovery.

## Quick start with Docker Compose

Requirements: Docker Compose, Python 3 for token generation, and an AnkiWeb or self-hosted
sync account.

```sh
git clone https://github.com/YOUR-USERNAME/anki-sync-mcp.git
cd anki-sync-mcp

mkdir -p secrets
python -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/anki_mcp_token
read -rsp "Anki sync password: " ANKI_PASSWORD
printf '%s' "$ANKI_PASSWORD" > secrets/anki_sync_password
unset ANKI_PASSWORD
chmod 600 secrets/*

export ANKI_SYNC_USERNAME="your-sync-username"
export ANKI_SYNC_HOST="" # empty for AnkiWeb; otherwise an HTTPS sync-server URL

docker compose up -d --build
docker compose exec anki-mcp anki-mcp-healthcheck
```

The included Compose configuration exposes MCP only to containers on its private `assistant`
network. Connect your agent container to that network and configure:

- URL: `http://anki-mcp:8000/mcp`
- Header: `Authorization: Bearer <contents of secrets/anki_mcp_token>`

For a client running directly on the host, add this to the `anki-mcp` service in
`compose.yaml`, then run `docker compose up -d` again:

```yaml
ports:
  - "127.0.0.1:8000:8000"
```

Use `http://127.0.0.1:8000/mcp` as the MCP URL. After connecting, call
`anki_sync_login` once. The resulting sync host key is stored in the persistent Docker volume,
so you can later remove `ANKI_SYNC_PASSWORD_FILE` from `compose.yaml` and delete the password
secret when automatic reauthentication is not required.

Do not run two sidecars, Anki Desktop, or any other process against the same collection file.
Keep destructive, schema, and full-sync feature flags disabled except during deliberate
maintenance.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen --all-groups
uv run pytest
uv run ruff check .
uv run pyright
```

## License

[MIT](LICENSE) — free to use, modify, and distribute under the license terms.

## Trademark disclaimer

This is an independent, unofficial project. It is not affiliated with, endorsed by, or
sponsored by Anki, AnkiWeb, Ankitects Pty Ltd, or their maintainers. Anki and AnkiWeb are
trademarks of their respective owners.