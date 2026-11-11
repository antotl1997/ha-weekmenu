"""A release is boring: versions agree, the store files exist and name real options."""

import tomllib
from pathlib import Path

import yaml

from tests.chrome import find_chrome

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent


def test_versions_in_sync():
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["version"] == project["project"]["version"] == "1.0.0"


def test_app_knows_its_own_version():
    from app import __version__
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert __version__ == config["version"]


def test_watchdog_and_app_version_option():
    from app.ahclient import APP_VERSION
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert config["watchdog"] == "http://[HOST]:[PORT:8099]/api/health"
    assert config["options"]["ah_app_version"] == APP_VERSION == "9.28"
    assert "ports" not in config  # still no host port: only the Supervisor reaches it


def test_changelog_has_current_version():
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert f"## {config['version']}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_find_chrome_linux_prefers_google_chrome():
    found = {"google-chrome": "/usr/bin/google-chrome", "chromium": "/usr/bin/chromium"}
    assert find_chrome(which=found.get, is_file=lambda p: False) == "/usr/bin/google-chrome"


def test_find_chrome_linux_falls_back_to_chromium():
    found = {"chromium": "/usr/bin/chromium"}
    assert find_chrome(which=found.get, is_file=lambda p: False) == "/usr/bin/chromium"


def test_find_chrome_windows_path_is_fallback():
    win = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    assert find_chrome(which=lambda n: None, is_file=lambda p: p == win) == win


def test_find_chrome_none_when_absent():
    assert find_chrome(which=lambda n: None, is_file=lambda p: False) is None


def workflow_path():
    """The CI workflow: live in .github/workflows, or parked in workflows-pending."""
    for folder in ("workflows", "workflows-pending"):
        path = REPO / ".github" / folder / "ci.yml"
        if path.is_file():
            return path
    return REPO / ".github" / "workflows" / "ci.yml"


def test_store_files_exist():
    for name in ("DOCS.md", "icon.png", "logo.png", "translations/nl.yaml",
                 "translations/en.yaml", "requirements.txt", "requirements-dev.txt"):
        assert (ROOT / name).is_file(), name
    for name in ("LICENSE", ".github/dependabot.yml"):
        assert (REPO / name).is_file(), name
    assert workflow_path().is_file()


def test_icon_sizes():
    from PIL import Image

    with Image.open(ROOT / "icon.png") as icon, Image.open(ROOT / "logo.png") as logo:
        assert icon.size == (128, 128)
        assert logo.size == (250, 100)


def test_translations_cover_every_option():
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    options = set(config["options"])
    assert options == set(config["schema"])
    for lang in ("nl", "en"):
        tr = yaml.safe_load((ROOT / "translations" / f"{lang}.yaml").read_text(encoding="utf-8"))
        assert set(tr["configuration"]) == options, lang
        for name, entry in tr["configuration"].items():
            assert entry["name"] and entry["description"], (lang, name)


def test_docs_name_the_options_and_sections():
    docs = (ROOT / "DOCS.md").read_text(encoding="utf-8")
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    for option in config["options"]:
        assert option in docs, option
    for heading in ("Wat het doet", "Installeren", "Inloggen bij AH", "Instellingen",
                    "In je bestelling", "Backups", "Problemen oplossen"):
        assert f"## {heading}" in docs, heading
    assert "https://github.com/antotl1997/ha-weekmenu" in docs


def test_license_and_references():
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert "Copyright 2026 antotl1997" in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert "LICENSE" in (REPO / "README.md").read_text(encoding="utf-8")
    assert "LICENSE" in (ROOT / "NOTICE").read_text(encoding="utf-8")


def test_dockerfile_installs_from_requirements():
    assert "requirements.txt" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_runtime_pins_are_exact():
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            assert "==" in line, line


def test_workflow_parses_and_has_jobs():
    ci = yaml.safe_load(workflow_path().read_text(encoding="utf-8"))
    assert set(ci["jobs"]) == {"tests", "frontend", "build", "audit"}
