import os
from pathlib import Path

import pytest

from app.tokens import Tokens, TokenStore


def test_save_then_load_roundtrip(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    t = Tokens("a1", "r1", 1234.5)
    store.save(t)
    assert store.load() == t


def test_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    old = Tokens("a1", "r1", 1.0)
    store.save(old)
    assert not Path(str(path) + ".tmp").exists()

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save(Tokens("a2", "r2", 2.0))
    monkeypatch.undo()
    assert store.load() == old


def test_load_missing_returns_none(tmp_path):
    assert TokenStore(tmp_path / "nope.json").load() is None
