import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.durable import migrate, remove_durably, schema_of, too_new, write_json

log = logging.getLogger(__name__)

# schema of tokens.json; 0 is the file from before 0.6 (no "schema" field)
TOKENS_SCHEMA = 1


def migrate_tokens_v0_to_v1(data: dict) -> dict:
    """0 -> 1: only the version field."""
    return {**data, "schema": 1}


_TOKENS_STEPS = {0: migrate_tokens_v0_to_v1}


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._warned_new = False
        self._warned_bad: float | None = None  # monotonic time of the last warning

    def _warn_bad(self, e: Exception) -> None:
        """A broken file: said at most once an hour (the schedule asks every minute)."""
        now = time.monotonic()
        if self._warned_bad is None or now - self._warned_bad >= 3600:
            self._warned_bad = now
            log.warning("%s niet te lezen (%s)", self.path.name, type(e).__name__)

    def load(self) -> Tokens | None:
        """The tokens, or None when there are none that this code may use.

        Log lines name the file and the kind of error only, never any content.
        A file from a newer add-on is not used: a refresh would rotate the refresh
        token, and this code may not write the answer back.
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            self._warn_bad(e)
            return None
        if schema_of(data) > TOKENS_SCHEMA:
            if not self._warned_new:  # the status page loads this every few seconds
                self._warned_new = True
                log.warning("%s is van een nieuwere versie van de add-on: niet gebruikt",
                            self.path.name)
            return None
        try:
            data = migrate(data, TOKENS_SCHEMA, _TOKENS_STEPS)
            return Tokens(
                data["access_token"], data["refresh_token"], float(data["expires_at"])
            )
        except (KeyError, ValueError, TypeError) as e:
            self._warn_bad(e)
            return None

    def saved_at(self) -> float | None:
        """When the login was last written (logging in or a refresh): the file's mtime,
        as the durable write replaces it whole. None without a file."""
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def too_new(self) -> bool:
        """tokens.json was written by a newer add-on."""
        return too_new(self.path, TOKENS_SCHEMA)

    def save(self, t: Tokens) -> None:
        """Durably, the tmp file created 0600; never over a newer file (SchemaTooNew)."""
        write_json(self.path, asdict(t), TOKENS_SCHEMA)

    def clear(self) -> None:
        """Log out: remove tokens.json, its pre-0.6 copy (.bak) and a left-over tmp file,
        each durably."""
        for name in (self.path.name, self.path.name + ".bak", self.path.name + ".tmp"):
            remove_durably(self.path.with_name(name))
        self._warned_new = False
