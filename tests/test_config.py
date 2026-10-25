import pytest

from bench.allowlist import main as allowlist_main
from bench.allowlist import render
from bench.config import ConfigError, load_agents, normalize_handle
from tests.conftest import FIXTURES


@pytest.mark.parametrize("raw, handle", [
    ("+1 (555) 010-0001", "+15550100001"),
    ("+44 20 7946 0958", "+442079460958"),
    ("  Boba@Example.com ", "boba@example.com"),
])
def test_normalize_handle(raw, handle):
    assert normalize_handle(raw) == handle


@pytest.mark.parametrize("raw", ["555-010-0001", "boba", 'a"b@example.com', "+1555\\0100001"])
def test_bad_handles_are_rejected(raw):
    with pytest.raises(ConfigError):
        normalize_handle(raw)


def test_blank_agents_are_skipped():
    agents = load_agents(FIXTURES / "agents.yaml")
    assert list(agents) == ["instinct", "boba"]
    assert agents["instinct"].quiet_period_seconds == 90


def test_duplicate_handles_are_rejected(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text("agents:\n  a:\n    handle: '+15550100001'\n  b:\n    handle: '+1 555 010 0001'\n")
    with pytest.raises(ConfigError, match="same handle"):
        load_agents(path)


def test_render_swift_allowlist():
    swift = render(load_agents(FIXTURES / "agents.yaml"))
    assert '(agent: "instinct", handle: "+15550100001"),' in swift
    assert '(agent: "boba", handle: "boba@example.com"),' in swift


def test_build_refuses_when_no_agent_has_a_handle(tmp_path, capsys):
    path = tmp_path / "agents.yaml"
    path.write_text("agents:\n  instinct:\n    handle: ''\n")
    assert allowlist_main([str(path)]) == 1
    assert "no iMessage agent" in capsys.readouterr().err


@pytest.mark.parametrize("raw, handle", [("Fo_Test_Bot", "@fo_test_bot"), ("@fo_test_bot", "@fo_test_bot")])
def test_telegram_usernames(raw, handle):
    assert normalize_handle(raw, "telegram") == handle


@pytest.mark.parametrize("raw", ["@abc", "fo-bot", "@1fo_bot", "+15550100001"])
def test_bad_telegram_usernames_are_rejected(raw):
    with pytest.raises(ConfigError):
        normalize_handle(raw, "telegram")


def test_whatsapp_needs_a_phone_number():
    assert normalize_handle("+1 555 010 0002", "whatsapp") == "+15550100002"
    with pytest.raises(ConfigError):
        normalize_handle("muse@example.com", "whatsapp")


def test_channels_per_agent(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text(
        "agents:\n"
        "  wajo: {channel: telegram, handle: Fo_Test_Bot}\n"
        "  muse: {channel: whatsapp, handle: '+1 555 010 0002', quiet_period_seconds: 180}\n"
        "  old: {handle: '+15550100003'}\n"
    )
    agents = load_agents(path)
    assert [(a.name, a.channel, a.handle) for a in agents.values()] == [
        ("wajo", "telegram", "@fo_test_bot"), ("muse", "whatsapp", "+15550100002"), ("old", "imessage", "+15550100003"),
    ]
    assert agents["muse"].quiet_period_seconds == 180
    assert allowlist_main([str(path)]) == 0  # only the iMessage agent goes into bench-reader


def test_unknown_channel_is_rejected(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text("agents:\n  boba: {channel: signal, handle: '+15550100004'}\n")
    with pytest.raises(ConfigError, match="channel must be one of"):
        load_agents(path)
