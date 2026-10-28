#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "click==8.4.2",
#     "requests==2.34.2",
#     "pycryptodome==3.23.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Where the account session comes from: setting, saved config, then the browser. No network."""

import sys
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import deezer_cli as cli


@pytest.fixture
def no_saved_session(monkeypatch):
    monkeypatch.delenv("DEEZER_ARL", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: {})


@pytest.fixture
def saved(monkeypatch):
    """Configs passed to save_config, in order."""
    configs: list[dict] = []
    monkeypatch.setattr(cli, "save_config", configs.append)
    return configs


@pytest.fixture
def browser_with(monkeypatch):
    """Make the browsers hold the given arls, of which only `valid` ones log in."""

    def install(arls: list[str], valid: set[str]):
        monkeypatch.setattr(cli, "iter_arls", lambda: iter([("chrome", a) for a in arls]))
        monkeypatch.setattr(
            cli.Deezer, "user_data", lambda self: {"USER_ID": 7} if self.arl in valid else {"USER_ID": 0}
        )

    return install


@pytest.mark.parametrize("value", ["", "  ", "${user_config.arl}"])
def test_env_arl_ignores_an_unfilled_setting(monkeypatch, value):
    monkeypatch.setenv("DEEZER_ARL", value)
    assert cli.env_arl() is None


def test_env_arl_reads_the_setting(monkeypatch):
    monkeypatch.setenv("DEEZER_ARL", "from-setting")
    assert cli.env_arl() == "from-setting"


def test_get_client_prefers_the_setting(monkeypatch):
    monkeypatch.setenv("DEEZER_ARL", "from-setting")
    monkeypatch.setattr(cli, "load_config", lambda: {"arl": "saved"})
    assert cli.get_client(require_account=True).arl == "from-setting"


def test_public_client_never_reads_the_browser(no_saved_session, monkeypatch):
    monkeypatch.setattr(cli, "iter_arls", lambda: pytest.fail("public commands must not touch the keychain"))
    assert cli.get_client().arl is None


def test_account_client_imports_the_browser_session(no_saved_session, saved, browser_with):
    browser_with(["good"], {"good"})
    assert cli.get_client(require_account=True).arl == "good"


def test_account_client_saves_the_browser_session(no_saved_session, saved, browser_with):
    browser_with(["good"], {"good"})
    cli.get_client(require_account=True)
    assert saved == [{"arl": "good", "source": "chrome"}]


def test_browser_session_skips_a_rejected_cookie(saved, browser_with):
    browser_with(["stale", "good"], {"good"})
    assert cli.browser_session()[1] == "good"


def test_account_client_without_any_session_says_where_to_log_in(no_saved_session, saved, browser_with):
    browser_with([], set())
    with pytest.raises(click.ClickException, match="Log in at https://www.deezer.com in Chrome"):
        cli.get_client(require_account=True)


def test_missing_session_names_the_extension_setting():
    assert "Deezer extension's settings" in cli.NO_SESSION_MESSAGE


def test_expired_saved_session_is_replaced_from_the_browser(monkeypatch, saved):
    monkeypatch.delenv("DEEZER_ARL", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: {"arl": "expired"})
    monkeypatch.setattr(cli, "iter_arls", lambda: iter([("chrome", "fresh")]))
    monkeypatch.setattr(
        cli.Deezer, "gw",
        lambda self, method, payload=None, need_token=True: {"checkForm": "t", "USER": {"USER_ID": 7 if self.arl == "fresh" else 0}},
    )
    client = cli.get_client(require_account=True)
    client._ensure_token()
    assert client.arl == "fresh"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
