import json
import re
from pathlib import Path

import pytest

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.json"))
POSTCODE = re.compile(r"\d{4}\s?[A-Z]{2}")
BAD_KEY = re.compile(
    r"token|email|address|street|memberid|customerid|phone|firstname|lastname"
    r"|initials|housenumber|postalcode|zip",
    re.I,
)


def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield path, k, None
            yield from walk(v, f"{path}.{k}")
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, path + "[]")
    else:
        yield path, None, o


def test_there_are_fixtures():
    assert FIXTURES


@pytest.mark.parametrize("f", FIXTURES, ids=lambda f: f.name)
def test_fixture_is_clean(f):
    text = f.read_text(encoding="utf-8")
    assert "@" not in text
    assert "eyJ" not in text
    assert "Bearer" not in text
    for path, key, value in walk(json.loads(text)):
        if key is not None:
            assert not BAD_KEY.search(key), f"{f.name}: field {path}.{key}"
        if isinstance(value, str):
            assert not POSTCODE.search(value), f"{f.name}: postcode-like {path}"
