"""Find a Chrome for the frontend tests: PATH on Linux, the usual folders on Windows."""

import shutil
from pathlib import Path

WINDOWS_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]


def find_chrome(which=shutil.which, is_file=lambda p: Path(p).is_file()):
    for name in NAMES:
        found = which(name)
        if found:
            return found
    return next((p for p in WINDOWS_PATHS if is_file(p)), None)
