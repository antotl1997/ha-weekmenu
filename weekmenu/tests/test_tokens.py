import json
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


# --- v0.6: token fixes and schema ------------------------------------------------------


@pytest.mark.parametrize("content", ['["a", "r"]', '{"access_token": "a", '
                                     '"refresh_token": "r", "expires_at": null}', "{kapot"])
def test_load_of_a_broken_file_returns_none_and_logs_no_content(tmp_path, caplog, content):
    path = tmp_path / "tokens.json"
    path.write_text(content, encoding="utf-8")
    caplog.set_level("DEBUG")
    assert TokenStore(path).load() is None
    assert '"a"' not in caplog.text and "refresh_token" not in caplog.text
    assert "access" not in caplog.text


def test_load_of_an_unreadable_file_returns_none(tmp_path, caplog):
    path = tmp_path / "tokens.json"
    path.mkdir()  # reading a directory: an OSError other than FileNotFoundError
    caplog.set_level("DEBUG")
    assert TokenStore(path).load() is None
    assert "tokens.json" in caplog.text


def test_saved_tokens_carry_the_schema_and_go_through_the_durable_helper(tmp_path, monkeypatch):
    from app import durable
    seen = []
    real = durable.replace_durably
    monkeypatch.setattr(durable, "replace_durably", lambda p, b: (seen.append(p), real(p, b)))
    path = tmp_path / "tokens.json"
    TokenStore(path).save(Tokens("a", "r", 2.0))
    assert seen == [path]
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1


def test_old_token_file_loads_and_is_kept_once_as_bak(tmp_path):
    from app.tokens import migrate_tokens_v0_to_v1
    old = {"access_token": "a", "refresh_token": "r", "expires_at": 1.0}
    assert migrate_tokens_v0_to_v1(old) == {**old, "schema": 1}
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    store = TokenStore(path)
    assert store.load() == Tokens("a", "r", 1.0)
    store.save(Tokens("a2", "r2", 2.0))
    assert json.loads((tmp_path / "tokens.json.bak").read_text(encoding="utf-8")) == old


def test_newer_token_file_is_not_used_and_never_written(tmp_path, caplog):
    from app.durable import SchemaTooNew
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"schema": 5, "access_token": "a", "refresh_token": "r",
                                "expires_at": 9e9}), encoding="utf-8")
    before = path.read_bytes()
    store = TokenStore(path)
    # using it could rotate the refresh token that this code then cannot save
    assert store.load() is None
    assert store.too_new()
    with pytest.raises(SchemaTooNew):
        store.save(Tokens("x", "y", 1.0))
    assert path.read_bytes() == before
