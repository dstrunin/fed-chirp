"""Tests for the Hermes subprocess JSON backend."""

from __future__ import annotations

import json
import subprocess

import pytest

from fed_chirp.scoring.hermes_client import HermesClient, HermesError, parse_hermes_json


def test_parse_hermes_json_strips_session_id_line():
    parsed = parse_hermes_json(
        'session_id: 20260609_104904_ba58c7\n{"score": 0.5, "label": "hawkish"}'
    )

    assert parsed == {"score": 0.5, "label": "hawkish"}


def test_parse_hermes_json_accepts_markdown_fence_after_session_id():
    parsed = parse_hermes_json(
        'session_id: abc\n```json\n{"notes": ["one", "two"]}\n```\n'
    )

    assert parsed == {"notes": ["one", "two"]}


def test_hermes_client_builds_quiet_codex_command(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='session_id: abc\n{"ok": true}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = HermesClient(hermes_bin="/tmp/hermes", provider="openai-codex", model="gpt-5.5")

    assert client.complete_json("Return JSON") == {"ok": True}
    assert calls == [[
        "/tmp/hermes",
        "chat",
        "-Q",
        "--provider",
        "openai-codex",
        "-m",
        "gpt-5.5",
        "--ignore-rules",
        "--source",
        "fed-chirp",
        "-q",
        "Return JSON",
    ]]


def test_hermes_client_raises_useful_error_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="auth failed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = HermesClient(hermes_bin="hermes")

    with pytest.raises(HermesError, match="auth failed"):
        client.complete_json("Return JSON")


def test_hermes_client_applies_env_defaults(monkeypatch):
    monkeypatch.setenv("FED_CHIRP_HERMES_BIN", "/opt/bin/hermes")
    monkeypatch.setenv("FED_CHIRP_HERMES_PROVIDER", "openai-codex")
    monkeypatch.setenv("FED_CHIRP_HERMES_MODEL", "gpt-5.5")
    monkeypatch.setenv("FED_CHIRP_HERMES_TIMEOUT", "123")

    client = HermesClient.from_env()

    assert client.hermes_bin == "/opt/bin/hermes"
    assert client.provider == "openai-codex"
    assert client.model == "gpt-5.5"
    assert client.timeout_seconds == 123


def test_parse_hermes_json_reports_malformed_output():
    with pytest.raises(HermesError, match="non-JSON"):
        parse_hermes_json("session_id: abc\nnot json")
