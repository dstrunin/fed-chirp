"""Hermes CLI backend for Fed Chirp scoring.

Fed Chirp calls Hermes as a local subprocess so it can use the user's
configured provider/auth (for example OpenAI Codex OAuth) instead of a paid
model API key owned by this project.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass


class HermesError(RuntimeError):
    """Raised when the Hermes subprocess fails or returns malformed output."""


def parse_hermes_json(output: str) -> dict:
    """Parse JSON from quiet Hermes output.

    Quiet CLI versions emit session metadata before or after the response.
    Remove only boundary metadata/blank lines, then an optional JSON fence;
    unexpected prose still fails closed instead of guessing at a JSON substring.
    """
    lines = output.strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].startswith("session_id:")):
        lines.pop(0)
    while lines and (not lines[-1].strip() or lines[-1].startswith("session_id:")):
        lines.pop()
    text = "\n".join(lines).strip()

    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HermesError(f"Hermes returned non-JSON output: {text[:300]!r}") from exc
    if not isinstance(parsed, dict):
        raise HermesError(f"Hermes returned JSON that is not an object: {text[:300]!r}")
    return parsed


@dataclass(frozen=True)
class HermesClient:
    hermes_bin: str = "hermes"
    provider: str = "openai-codex"
    model: str = "gpt-6-astra"
    source: str = "tool"
    timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "HermesClient":
        return cls(
            hermes_bin=os.environ.get("FED_CHIRP_HERMES_BIN", "hermes"),
            provider=os.environ.get("FED_CHIRP_HERMES_PROVIDER", "openai-codex"),
            model=os.environ.get("FED_CHIRP_HERMES_MODEL", "gpt-6-astra"),
            source=os.environ.get("FED_CHIRP_HERMES_SOURCE", "tool"),
            timeout_seconds=int(os.environ.get("FED_CHIRP_HERMES_TIMEOUT", "300")),
        )

    def complete_json(self, prompt: str) -> dict:
        cmd = [
            self.hermes_bin,
            "chat",
            "-Q",
            "--provider",
            self.provider,
            "-m",
            self.model,
            "--ignore-rules",
            "--source",
            self.source,
            "-q",
            prompt,
        ]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise HermesError(
                f"Hermes executable not found: {self.hermes_bin!r}. "
                "Set FED_CHIRP_HERMES_BIN to the full hermes path."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HermesError(
                f"Hermes timed out after {self.timeout_seconds}s while scoring."
            ) from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise HermesError(
                f"Hermes exited with status {proc.returncode}: {detail[:500]}"
            )
        return parse_hermes_json(proc.stdout)
