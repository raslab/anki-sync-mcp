from __future__ import annotations

import uvicorn

from anki_mcp.app import create_app
from anki_mcp.config import Settings


def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from environment
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        workers=1,
    )


if __name__ == "__main__":
    main()
