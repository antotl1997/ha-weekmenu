import json
import re
from pathlib import Path

import pytest

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.json"))
POSTCODE = re.compile(r"\d{4}\s?[A-Z]{2}")
BAD_KEY = re.compile(
    r"token|email|address|street|memberid|customerid|phone|firstname|lastname"
    r"|initials|housenumber|postalcode|zip"
    r"|city|invoice|payment|cashier|terminal|transaction|cardnumber|storeaddress"
    r"|cancellationreason|closingtime|deliverytimeperiod|timedisplay",
    re.I,
)
HISTORY = sorted((Path(__file__).parent / "fixtures").glob("history-*.json"))


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


def test_there_are_history_fixtures():
    assert len(HISTORY) >= 10


@pytest.mark.parametrize("f", HISTORY, ids=lambda f: f.name)
def test_history_ids_are_fake(f):
    """Order ids are 9000xx, receipt ids `receipt-n`; payments are gone."""
    for path, key, value in walk(json.loads(f.read_text(encoding="utf-8"))):
        if path.endswith(".orderId"):
            assert isinstance(value, int) and 900001 <= value < 901000, f"{f.name}: {path}"
        if path.endswith("posReceipts[].id") or path.endswith("posReceiptDetails.id"):
            assert re.fullmatch(r"receipt-\d+", value), f"{f.name}: {path}"
        if path.endswith("posReceipts[].dateTime"):
            assert value.endswith("T12:00:00.000Z"), f"{f.name}: time of day kept"
        if key is not None:
            assert key not in ("payments", "orderPayments", "address"), f"{f.name}: {key}"
        if path.endswith("orderedProducts[].product.mainCategory"):
            assert "gezondheid" not in value.lower(), f"{f.name}: health product kept"
        if path.endswith("statusDescription"):
            assert value in ("redacted", "Geannuleerd"), f"{f.name}: {path}"
