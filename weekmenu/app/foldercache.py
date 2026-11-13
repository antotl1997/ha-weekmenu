"""The bonus folders already read from AH, kept over a restart.

Reading a whole folder takes some 340 AH calls one after another (a call per category,
one more per bonus group), which is minutes. The folder does not change during its
week, so it is read once per period and kept here.

`/data/folder.json` (schema, durable): {"folders": {"<period start>": [product, ...]}},
products in AH's own order (the Aanbiedingen tab shows them that way). At most KEEP
periods: the current and the next folder, plus one spare. A file that cannot be read,
or one from a newer add-on, loads as empty: the folder is then simply read again.
Saving never stops anything (logged). Callers hold `ctx.lock`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path

from app.ahclient import Product
from app.durable import schema_of, write_json

log = logging.getLogger(__name__)

FOLDER_SCHEMA = 2  # 2: products know their bonus group (an older file is read again)
KEEP = 3
_FIELDS = {f.name for f in fields(Product)}


def remember(cache: dict[date, list[Product]], start: date,
             items: list[Product]) -> dict[date, list[Product]]:
    """`cache` with the folder of `start` added, without the oldest beyond KEEP."""
    out = {**cache, start: items}
    return {k: out[k] for k in sorted(out)[-KEEP:]}


def load_folders(path: Path) -> dict[date, list[Product]]:
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("%s niet te lezen (%s)", Path(path).name, type(e).__name__)
        return {}
    if not isinstance(data, dict) or schema_of(data) != FOLDER_SCHEMA:
        return {}  # a newer (or unknown) form: read the folder again
    folders = data.get("folders")
    out: dict[date, list[Product]] = {}
    for key, items in (folders or {}).items() if isinstance(folders, dict) else ():
        try:
            out[date.fromisoformat(key)] = [
                Product(**{k: v for k, v in p.items() if k in _FIELDS}) for p in items]
        except (TypeError, ValueError, AttributeError):
            log.warning("Folder van %s in %s overgeslagen", key, Path(path).name)
    return {k: out[k] for k in sorted(out)[-KEEP:]}


def save_folders(path: Path, cache: dict[date, list[Product]]) -> None:
    try:
        write_json(path, {"folders": {k.isoformat(): [asdict(p) for p in v]
                                      for k, v in sorted(cache.items())}}, FOLDER_SCHEMA)
    except Exception as e:  # a lost cache only costs a read of the folder again
        log.warning("%s niet te bewaren (%s)", Path(path).name, type(e).__name__)
