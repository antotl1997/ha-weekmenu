"""Diagnose for the app ("Kopieer diagnose"): what is needed to see why something broke,
and nothing secret.

The last log lines come from an in-memory ring buffer on the root logger. No AH call,
no lock. Never in the output: a token, a login code, an Authorization header or an AH
response body. The log lines of this add-on carry none of those to begin with; as a
second line of defence the whole output is scrubbed of the stored token values, of
anything after "Bearer " and of a "code=" value.
"""
from __future__ import annotations

import json
import logging
import re
from collections import deque

from app import __version__

RING_SIZE = 50
WEEKS_SHOWN = 8
_BEARER = re.compile(r"(?i)bearer\s+[^\s\"']+")
_CODE = re.compile(r"code=[A-Za-z0-9._~-]+")
_INGRESS = re.compile(r"hassio_ingress/[^/\s\"'?#]+")
# only the add-on's own loggers: an access log or httpx line may carry an ingress
# token (Referer) or a URL with a query
OWN_LOGGERS = ("weekmenu", "app")


class OwnLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        return any(name == p or name.startswith(p + ".") for p in OWN_LOGGERS)


class RingHandler(logging.Handler):
    """Keeps the last RING_SIZE formatted log lines in memory."""

    def __init__(self, size: int = RING_SIZE):
        super().__init__(level=logging.INFO)
        self.addFilter(OwnLogs())
        self.lines: deque[str] = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # a log line must never break the add-on
            pass


RING = RingHandler()


def install_ring() -> RingHandler:
    """Hang the ring buffer on the root logger, once."""
    root = logging.getLogger()
    if RING not in root.handlers:
        root.addHandler(RING)
    return RING


def _secrets(ctx) -> list[str]:
    try:
        t = ctx.store.load()
    except Exception:
        return []
    if t is None:
        return []
    return [v for v in (t.access_token, t.refresh_token) if isinstance(v, str) and len(v) >= 4]


def scrub(text: str, secrets: list[str]) -> str:
    for v in secrets:
        text = text.replace(v, "***")
    text = _BEARER.sub("Bearer ***", text)
    text = _INGRESS.sub("hassio_ingress/***", text)
    return _CODE.sub("code=***", text)


def _weeks(ctx) -> list[dict]:
    out = []
    for w in ctx.ledger.weeks()[-WEEKS_SHOWN:]:
        out.append({
            "week": w.week.isoformat(),
            "added": len(w.added), "pending": len(w.pending), "unknown": len(w.unknown),
            "held": w.held is not None, "round_done": bool(w.round_done),
            "too_new": bool(w.too_new),
        })
    return out


def diagnose(ctx, ring: RingHandler = RING) -> dict:
    """Everything for api/diagnose, scrubbed."""
    from app.hass import AWAY_SCHEMA, TOTAL_SCHEMA
    from app.ledger import LEDGER_SCHEMA
    from app.main import STATE_SCHEMA
    from app.tokens import TOKENS_SCHEMA

    try:
        weeks = _weeks(ctx)
    except Exception as e:
        weeks = [{"error": type(e).__name__}]
    out = {
        "addon_version": __version__,
        "ah_app_version": ctx.app_version,
        "writes_paused": ctx.health.writes_paused,
        "api_health": ctx.health.view(),
        "last_round": ctx.last_round,
        "error": ctx.error,
        "login_expired": ctx.login_expired,
        "planner_seconds": ctx.planner_seconds,
        "schemas": {
            "code": {"weeklogboek": LEDGER_SCHEMA, "totaal.json": TOTAL_SCHEMA,
                     "niet_thuis.json": AWAY_SCHEMA, "state.json": STATE_SCHEMA,
                     "tokens.json": TOKENS_SCHEMA},
            "nieuwer_dan_code": sorted(ctx.too_new),
        },
        "weeks": weeks,
        "log": list(ring.lines),
    }
    clean = scrub(json.dumps(out, ensure_ascii=False), _secrets(ctx))
    return json.loads(clean)
