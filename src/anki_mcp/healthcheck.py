from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from anki_mcp.config import Settings


def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from environment
    url = f"http://127.0.0.1:{settings.port}/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:  # noqa: S310 (fixed localhost)
            body = json.load(response)
            if response.status != 200 or body != {"status": "ready"}:
                raise RuntimeError("service is not ready")
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        sys.exit(1)


if __name__ == "__main__":
    main()
