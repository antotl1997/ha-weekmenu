"""Durable file writes and the schema version of every JSON file the add-on writes.

`replace_durably` writes a tmp file (created 0600, its content fsynced), replaces the
target with it and fsyncs the directory: a power cut leaves either the old or the new
file, never half of one.

Every JSON file carries `"schema": N`. A file without it is version 0 (the form from
before 0.6). A file with a higher version than this code knows came from a newer
add-on: it is never written (`SchemaTooNew`), only read for display. The first time
an older file is written over, a copy is kept once as `<name>.bak`.
"""
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# shown once when a file is newer than this code (also by the web API)
UPDATE_MESSAGE = "Deze versie van de add-on is ouder dan je gegevens; werk de add-on bij"

_REPLACE_TRIES = 6
_REPLACE_WAIT = 0.05


class SchemaTooNew(Exception):
    """The file was written by a newer add-on: this code must not write it."""

    def __init__(self, path: Path):
        super().__init__(f"{Path(path).name} is nieuwer dan deze versie van de add-on")
        self.path = Path(path)


def fsync_dir(directory: Path) -> None:
    """Make a rename in `directory` survive a power cut.

    Skipped where a directory cannot be opened (Windows): the write itself already
    succeeded, so this never raises.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def replace_durably(path: Path, data: bytes) -> None:
    """Write `data` to `path` so that a crash leaves the old or the new file whole.

    The tmp file is created with mode 0600 (it may hold tokens), its content is
    fsynced before the replace, and the directory after it. On any error the tmp
    file is removed and the original is left as it was. A tmp file left over from a
    crash is removed first: O_CREAT only sets the mode of a new file, so reusing an
    old 0644 one would make the target 0644 too.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        tmp.unlink(missing_ok=True)
        fd = os.open(str(tmp), flags, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: a scanner may hold the file for a moment
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_WAIT)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def remove_durably(path: Path) -> None:
    """Remove `path` (if there) so that the removal survives a power cut."""
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fsync_dir(path.parent)


# --- schema versions -------------------------------------------------------------------


def schema_of(data) -> int:
    """The schema version of parsed JSON: 0 when the field is missing (before 0.6)."""
    if isinstance(data, dict):
        v = data.get("schema")
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return v
    return 0


def disk_schema(path: Path) -> int | None:
    """The schema version of the file on disk; None when there is no file.

    A file that cannot be parsed counts as version 0: it is kept as `.bak` before
    it is written over.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    try:
        return schema_of(json.loads(raw.decode("utf-8")))
    except ValueError:
        return 0


def too_new(path: Path, known: int) -> bool:
    """The file at `path` was written by a newer add-on than this one."""
    try:
        v = disk_schema(path)
    except OSError:
        return False
    return v is not None and v > known


def backup_once(path: Path) -> None:
    """Keep the file as `<name>.bak` next to it, unless a backup is already there."""
    path = Path(path)
    bak = path.with_name(path.name + ".bak")
    if bak.exists():
        return
    replace_durably(bak, path.read_bytes())
    log.info("Oude versie van %s bewaard als %s", path.name, bak.name)


def write_json(path: Path, data: dict, version: int) -> None:
    """Write `data` with `"schema": version` durably.

    Never writes over a file from a newer add-on (raises SchemaTooNew). The first time
    an older file is written over it is kept once as `<name>.bak`.
    """
    path = Path(path)
    on_disk = disk_schema(path)
    if on_disk is not None and on_disk > version:
        raise SchemaTooNew(path)
    if on_disk is not None and on_disk < version:
        backup_once(path)
    body = {"schema": version, **{k: v for k, v in data.items() if k != "schema"}}
    replace_durably(path, json.dumps(body).encode("utf-8"))


def migrate(data, version: int, steps: dict):
    """Bring parsed JSON of schema `schema_of(data)` up to `version`.

    `steps[n]` turns version n into n + 1. A file newer than `version` is returned
    unchanged: the caller decides what reading it may do.
    """
    v = schema_of(data)
    while v < version:
        data = steps[v](data)
        v += 1
    return data
