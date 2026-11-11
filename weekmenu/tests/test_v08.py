"""v0.8: notify targets as options, Vanavond, the order reminder, the login line."""
import json
from datetime import date, datetime, time
from pathlib import Path

import httpx
import pytest
import yaml

from app import main
from app.hass import BASE_URL, DEFAULT_OWNER, DEFAULT_TODAY, Hass, notify_target

ROOT = Path(__file__).resolve().parent.parent


# --- 1. notify targets are options ------------------------------------------------------


def test_config_has_the_notify_options_and_dinner_time():
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert config["options"]["notify_owner"] == "mobile_app_s_26_ultra_anto"
    assert config["options"]["notify_today"] == ["mobile_app_s24_van_maartje"]
    assert config["options"]["dinner_time"] == "18:30"
    assert DEFAULT_OWNER == "mobile_app_s_26_ultra_anto"
    assert DEFAULT_TODAY == ["mobile_app_s24_van_maartje"]


def test_no_fixed_notify_name_in_hass():
    text = (ROOT / "app" / "hass.py").read_text(encoding="utf-8")
    assert "NOTIFY_SERVICE" not in text
    assert text.count("mobile_app_s_26_ultra_anto") == 1  # only the default


@pytest.mark.parametrize("raw, want", [
    ("mobile_app_x", "mobile_app_x"),
    (" notify.mobile_app_x ", "mobile_app_x"),
    ("notify/mobile_app_x", "mobile_app_x"),
    ("Mobile_App_X", "mobile_app_x"),
    ("../states/sensor.x", None),
    ("mobile app", None),
    ("", None),
    (None, None),
    (5, None),
])
def test_notify_target_is_a_bare_service_name(raw, want):
    assert notify_target(raw) == want


def test_load_notify_options(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"notify_owner": "notify.mobile_app_a",
                                "notify_today": ["mobile_app_b", "bad name", "mobile_app_c"],
                                "dinner_time": "19:00"}), encoding="utf-8")
    assert main.load_notify_options(path) == ("mobile_app_a", ["mobile_app_b", "mobile_app_c"],
                                              time(19, 0))
    assert main.load_notify_options(tmp_path / "nope.json") == (
        DEFAULT_OWNER, DEFAULT_TODAY, time(18, 30))
    path.write_text(json.dumps({"notify_owner": "../x", "notify_today": [],
                                "dinner_time": "25:99"}), encoding="utf-8")
    # a broken owner falls back to the default; an empty list means nobody gets Vanavond
    assert main.load_notify_options(path) == (DEFAULT_OWNER, [], time(18, 30))


async def test_owner_and_today_targets(respx_mock):
    owner = respx_mock.post(f"{BASE_URL}/services/notify/mobile_app_owner").respond(200, json=[])
    other = respx_mock.post(f"{BASE_URL}/services/notify/mobile_app_other").respond(200, json=[])
    async with httpx.AsyncClient() as http:
        h = Hass(http, owner="mobile_app_owner", today=["mobile_app_other"])
        await h.notify("T", "M")
        await h.notify_to("mobile_app_other", "Weekmenu", "Vanavond: x")
    assert owner.call_count == 1 and other.call_count == 1
    assert h.today == ["mobile_app_other"]
    with pytest.raises(ValueError):
        await h.notify_to("../states/x", "T", "M")
