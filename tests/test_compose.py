from pathlib import Path

import yaml


def test_compose_keeps_private_mcp_network_and_allows_sync_egress() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    service_networks = set(compose["services"]["anki-mcp"]["networks"])

    assert service_networks == {"assistant", "egress"}
    assert compose["networks"]["assistant"]["internal"] is True
    assert compose["networks"]["egress"] == {}
    assert "ports" not in compose["services"]["anki-mcp"]
    environment = compose["services"]["anki-mcp"]["environment"]
    assert environment["ANKI_SYNC_USERNAME"] == "${ANKI_SYNC_USERNAME:-}"
    assert "ANKI_SYNC_PASSWORD" not in environment
    assert environment["ANKI_SYNC_PASSWORD_FILE"] == "/run/secrets/anki_sync_password"
    assert "anki_sync_password" in compose["services"]["anki-mcp"]["secrets"]
