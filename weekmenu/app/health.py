"""ApiHealth: notice when AH changed its API before the add-on writes nonsense.

Per critical endpoint (bonus, recept, product, lijst, bestelling) the failures in a
row are counted. A failure is a shape error (a field missing or of the wrong type, or
no JSON) or a 4xx other than 401, and other than 404 on the order (no active order).
The AhClient decides what counts (`ahclient.health_kind`); this object only counts.

After PAUSE_AFTER failures in a row on one endpoint `writes_paused` is set: the writer
then sends nothing to AH. A success resets that endpoint's count. Writing resumes after
a successful full read round (the bonus, the list or the order, and the endpoint that
paused it) or by hand ("Opnieuw proberen" in the app). Never holds a body or a token:
only counts and the kind of the last failure ("vorm", "HTTP 403").
"""
import logging
from collections.abc import Callable
from datetime import datetime

log = logging.getLogger("weekmenu.health")

CRITICAL = ("bonus", "recept", "product", "lijst", "bestelling")
PAUSE_AFTER = 2
PAUSED_NOTE = "Schrijven staat stil omdat AH iets veranderd lijkt te hebben"
PAUSED_MESSAGE = ("AH lijkt iets veranderd te hebben, schrijven naar je bestelling staat "
                  "stil. Kijk in de Weekmenu-app bij Diagnose.")
_TARGETS = {"lijst", "bestelling"}


class ApiHealth:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self.clock = clock or datetime.now
        self.fails: dict[str, int] = {e: 0 for e in CRITICAL}
        self.last_error: dict[str, str] = {}
        self.writes_paused = False
        self.paused_by: str | None = None
        self.paused_at: str | None = None
        self.last_ok: str | None = None  # the last successful call on a critical endpoint
        self._seen: set[str] = set()  # successes since the pause, for the read round

    def _stamp(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def ok(self, endpoint: str) -> None:
        self.fails[endpoint] = 0
        self.last_ok = self._stamp()
        if not self.writes_paused:
            return
        self._seen.add(endpoint)
        if ("bonus" in self._seen and self._seen & _TARGETS
                and (self.paused_by is None or self.paused_by in self._seen)):
            self.resume("geslaagde leesronde")

    def failed(self, endpoint: str, kind: str) -> None:
        self.fails[endpoint] = self.fails.get(endpoint, 0) + 1
        self.last_error[endpoint] = kind
        if self.writes_paused:
            self._seen.clear()  # the read round starts over
            return
        if self.fails[endpoint] >= PAUSE_AFTER:
            self.writes_paused = True
            self.paused_by = endpoint
            self.paused_at = self._stamp()
            self._seen.clear()
            log.warning("Schrijven naar AH staat stil: %d keer achter elkaar een fout op %s (%s)",
                        self.fails[endpoint], endpoint, kind)

    def resume(self, why: str) -> None:
        was = self.writes_paused
        self.writes_paused = False
        self.paused_by = self.paused_at = None
        self.fails = {e: 0 for e in CRITICAL}
        self._seen.clear()
        if was:
            log.info("Schrijven naar AH staat weer aan (%s)", why)

    def restore(self, paused: bool, paused_by: str | None, paused_at: str | None) -> None:
        """From state.json: a pause survives a restart (a watchdog restart never unpauses)."""
        self.writes_paused = bool(paused)
        self.paused_by = paused_by if paused_by in CRITICAL else None
        self.paused_at = paused_at if isinstance(paused_at, str) else None

    def persisted(self) -> dict:
        return {"writes_paused": self.writes_paused, "paused_by": self.paused_by,
                "paused_at": self.paused_at}

    def view(self) -> dict:
        return {
            "writes_paused": self.writes_paused, "paused_by": self.paused_by,
            "paused_at": self.paused_at, "last_ok": self.last_ok,
            "endpoints": {e: {"fouten_achter_elkaar": self.fails.get(e, 0),
                              "laatste_fout": self.last_error.get(e)} for e in CRITICAL},
        }
