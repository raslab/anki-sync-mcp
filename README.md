# anki-sync-mcp

**A headless, sync-native Anki MCP sidecar for always-on AI agents.**

`anki-sync-mcp` lets MCP clients manage a real Anki collection without Anki Desktop or
AnkiConnect. It runs as an authenticated Streamable HTTP service, uses Anki's native collection
and synchronization APIs, and connects to AnkiWeb or a self-hosted Anki sync server.

The project is designed for unattended, containerized deployments where an AI agent needs safe,
durable access to Anki. It serializes collection access, persists mutation receipts across
restarts, supports idempotent writes, and coordinates synchronization around changes.

## Capabilities

The server exposes MCP tools for:

- decks, notes, cards, tags, and note types;
- templates and collection media;
- scheduling controls, review history, and analytics;
- native FSRS simulation, optimization, and rescheduling;
- collection backups and explicit full-sync recovery.

## Safety model

Authentication is required for MCP access. Destructive and schema-changing capabilities are
disabled by default and must be explicitly enabled. Guarded operations use preview/apply flows,
stable confirmation tokens, verified backups, bounded resource usage, and durable idempotency
receipts.

Only one process may access a collection file. The service is therefore intended to own its
local collection and synchronize changes with other Anki clients through the configured sync
server.

## Deployment and compatibility

The repository includes a Docker image and Compose configuration for running the server as a
private sidecar alongside an AI agent. Configuration is supplied through environment variables
and Docker secrets, while collection data, authentication state, receipts, and backups live in
persistent storage.

Native collection and FSRS behavior is validated against the pinned `anki==26.5` backend. Other
Anki versions are unsupported until their APIs and behavior have been tested.

## License

[MIT](LICENSE) — free to use, modify, and distribute under the license terms.

## Trademark disclaimer

This is an independent, unofficial project. It is not affiliated with, endorsed by, or
sponsored by Anki, AnkiWeb, Ankitects Pty Ltd, or their maintainers. Anki and AnkiWeb are
trademarks of their respective owners.