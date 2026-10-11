"""Mapping proposal providers: live (Claude Agent SDK in an isolated child) and replay (recorded runs)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from .. import FIXTURES_DIR, ROOT
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, request_key, user_prompt
from .schema import MappingProposal

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5-5"
ALLOWED_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
RETRYABLE_WORDS = ("overloaded", "rate limit", "rate_limit", "429", "529", "503", "502", "500", "timeout", "temporarily")
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LANG", "TMPDIR", "ANTHROPIC_API_KEY")


class ProposalUnavailable(RuntimeError):
    """No proposal could be produced (no recording for this layout, or the live call failed)."""


class ProposalResult(BaseModel):
    proposal: MappingProposal
    mode: Literal["live", "replay"]
    model: str
    recorded_at: str
    duration_ms: int
    cost_usd: float | None = None
    num_turns: int = 0
    attempts: int = 1
    key: str
    prompt_version: str = PROMPT_VERSION
    fallback_reason: str | None = None


class MappingProvider(Protocol):
    mode: Literal["live", "replay"]

    def propose(self, headers: list[str], payload: dict[str, Any]) -> ProposalResult: ...


def child_env() -> dict[str, str]:
    """The child's whole environment, built from an allowlist (never os.environ as a base)."""
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if os.environ.get(k)}
    env.setdefault("LANG", "en_US.UTF-8")
    return env


def selected_model() -> str:
    model = os.environ.get("FARMCHECK_MODEL", DEFAULT_MODEL).strip()
    if model not in ALLOWED_MODELS:
        raise ValueError(f"FARMCHECK_MODEL must be one of {', '.join(ALLOWED_MODELS)}")
    return model


def _bundled_cli() -> Path | None:
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    p = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    return p if p.exists() else None


@lru_cache(maxsize=1)
def live_available() -> tuple[bool, str]:
    """True when a key is set or the local Claude Code login answers; checked once per process."""
    if os.environ.get("DEMO_REPLAY") == "1":
        return False, "DEMO_REPLAY=1"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True, "ANTHROPIC_API_KEY"
    cli = _bundled_cli()
    if cli is None:
        return False, "the Agent SDK is not installed"
    try:
        out = subprocess.run([str(cli), "auth", "status"], capture_output=True, text=True, timeout=20, env=child_env(), check=False)
        status = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False, "no Claude login found"
    if status.get("loggedIn"):
        return True, "local Claude Code login"
    return False, "no Claude login found"


def fixture_path(key: str, base: Path = FIXTURES_DIR) -> Path:
    return base / f"{key}.json"


class ReplayProvider:
    mode: Literal["live", "replay"] = "replay"

    def __init__(self, base: Path = FIXTURES_DIR) -> None:
        self.base = base

    def propose(self, headers: list[str], payload: dict[str, Any]) -> ProposalResult:
        key = request_key(payload)
        path = fixture_path(key, self.base)
        if not path.exists():
            raise ProposalUnavailable(f"no recorded proposal for this layout (key {key}); map the columns by hand")
        data = json.loads(path.read_text(encoding="utf-8"))
        result = ProposalResult.model_validate(data["result"])
        return result.model_copy(update={"mode": "replay"})


class LiveProvider:
    mode: Literal["live", "replay"] = "live"

    def __init__(self, model: str | None = None, max_attempts: int = 4, timeout_s: float = 150.0) -> None:
        self.model = model or selected_model()
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s

    def _call(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "farmcheck.mapping.live_worker"],
                input=json.dumps(req),
                capture_output=True,
                text=True,
                cwd=ROOT,
                env=child_env(),
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timeout after {self.timeout_s:.0f} s", "api_error_status": None}
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            return {"ok": False, "error": f"worker exited {proc.returncode}: {proc.stderr[-400:]}", "api_error_status": None}
        return json.loads(lines[-1])

    @staticmethod
    def retryable(res: dict[str, Any]) -> bool:
        if res.get("api_error_status") in RETRYABLE_STATUS:
            return True
        err = str(res.get("error") or "").lower()
        return any(w in err for w in RETRYABLE_WORDS)

    def propose(self, headers: list[str], payload: dict[str, Any]) -> ProposalResult:
        req = {
            "system": SYSTEM_PROMPT,
            "prompt": user_prompt(payload),
            "model": self.model,
            "headers": headers,
            "max_turns": 4,
        }
        res: dict[str, Any] = {}
        for attempt in range(1, self.max_attempts + 1):
            res = self._call(req)
            if res.get("ok"):
                return ProposalResult(
                    proposal=MappingProposal.model_validate(res["proposal"]),
                    mode="live",
                    model=res.get("model") or self.model,
                    recorded_at=dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
                    duration_ms=int(res.get("duration_ms") or 0),
                    cost_usd=res.get("cost_usd"),
                    num_turns=int(res.get("num_turns") or 0),
                    attempts=attempt,
                    key=request_key(payload),
                )
            if attempt < self.max_attempts and self.retryable(res):
                delay = min(30.0, 2.0 * 2 ** (attempt - 1)) + random.uniform(0, 1.0)
                log.warning("mapping call failed (%s), retry %s in %.1f s", res.get("error"), attempt, delay)
                time.sleep(delay)
                continue
            break
        raise ProposalUnavailable(f"live proposal failed: {res.get('error')}")


class FallbackProvider:
    """Live first; when the live call fails, the recorded replay, saying so."""

    mode: Literal["live", "replay"] = "live"

    def __init__(self, live: LiveProvider, replay: ReplayProvider) -> None:
        self.live, self.replay = live, replay

    def propose(self, headers: list[str], payload: dict[str, Any]) -> ProposalResult:
        try:
            return self.live.propose(headers, payload)
        except ProposalUnavailable as exc:
            result = self.replay.propose(headers, payload)
            return result.model_copy(update={"fallback_reason": str(exc)})


def get_provider() -> MappingProvider:
    ok, _why = live_available()
    if not ok:
        return ReplayProvider()
    return FallbackProvider(LiveProvider(), ReplayProvider())


def save_fixture(result: ProposalResult, payload: dict[str, Any], base: Path = FIXTURES_DIR) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = fixture_path(result.key, base)
    body = {"note": "Recorded from a real live run by scripts/record_fixtures.py", "payload": payload,
            "result": result.model_dump(mode="json")}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path
