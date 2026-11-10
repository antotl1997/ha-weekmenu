import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from app.durable import migrate, schema_of, too_new, write_json

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
            log.warning("%s niet te lezen (%s)", self.path.name, type(e).__name__)
            return None
        if schema_of(data) > TOKENS_SCHEMA:
            log.warning("%s is van een nieuwere versie van de add-on: niet gebruikt",
                        self.path.name)
            return None
        try:
            data = migrate(data, TOKENS_SCHEMA, _TOKENS_STEPS, "tokens")
            return Tokens(
                data["access_token"], data["refresh_token"], float(data["expires_at"])
            )
        except (KeyError, ValueError, TypeError) as e:
            log.warning("%s niet te lezen (%s)", self.path.name, type(e).__name__)
            return None

    def too_new(self) -> bool:
        """tokens.json was written by a newer add-on."""
        return too_new(self.path, TOKENS_SCHEMA)

    def save(self, t: Tokens) -> None:
        """Durably, the tmp file created 0600; never over a newer file (SchemaTooNew)."""
        write_json(self.path, asdict(t), TOKENS_SCHEMA)
