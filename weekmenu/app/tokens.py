import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from app.durable import fsync_dir


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Tokens | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Tokens(
                data["access_token"], data["refresh_token"], float(data["expires_at"])
            )
        except (FileNotFoundError, KeyError, ValueError):
            return None

    def save(self, t: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(self.path) + ".tmp")
        try:
            tmp.write_text(json.dumps(asdict(t)), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        fsync_dir(self.path.parent)
