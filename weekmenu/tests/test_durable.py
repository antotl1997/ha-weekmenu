"""v0.6: durable writes and schema versions."""
import json
import os
import stat
import sys

import pytest

from app import durable
from app.durable import (
    SchemaTooNew,
    disk_schema,
    migrate,
    replace_durably,
    schema_of,
    too_new,
    write_json,
)


def test_replace_durably_writes_and_fsyncs_content_and_directory(tmp_path, monkeypatch):
    synced, dirs = [], []
    real_fsync = os.fsync
    monkeypatch.setattr(durable.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    monkeypatch.setattr(durable, "fsync_dir", lambda d: dirs.append(d))
    path = tmp_path / "sub" / "x.json"
    replace_durably(path, b'{"a": 1}\n')
    assert path.read_bytes() == b'{"a": 1}\n'  # binary: no \r\n on Windows
    assert len(synced) == 1  # the content, before the replace
    assert dirs == [tmp_path / "sub"]
    assert not list((tmp_path / "sub").glob("*.tmp"))


def test_tmp_file_is_created_0600(tmp_path, monkeypatch):
    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **kw):
        if str(path).endswith(".tmp"):
            modes.append(mode)
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(durable.os, "open", spy)
    replace_durably(tmp_path / "tokens.json", b"{}")
    assert modes == [0o600]
    if sys.platform != "win32":
        assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600


def test_power_cut_between_tmp_and_replace_leaves_original_whole(tmp_path, monkeypatch):
    path = tmp_path / "totaal.json"
    replace_durably(path, b'{"total": 1.0}')

    def cut(src, dst):
        raise OSError("stroom weg")

    monkeypatch.setattr(durable.os, "replace", cut)
    with pytest.raises(OSError):
        replace_durably(path, b'{"total": 2.0, "half": ')
    assert path.read_bytes() == b'{"total": 1.0}'
    assert not list(tmp_path.glob("*.tmp"))


def test_replace_retries_a_windows_permission_error(tmp_path, monkeypatch):
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "in gebruik")
        real(src, dst)

    monkeypatch.setattr(durable.os, "replace", flaky)
    monkeypatch.setattr(durable, "_REPLACE_WAIT", 0)
    replace_durably(tmp_path / "x.json", b"1")
    assert len(calls) == 3 and (tmp_path / "x.json").read_bytes() == b"1"


def test_schema_of_and_disk_schema(tmp_path):
    assert schema_of({"a": 1}) == 0
    assert schema_of({"schema": 3}) == 3
    assert schema_of([1]) == 0 and schema_of(4.5) == 0
    assert schema_of({"schema": True}) == 0 and schema_of({"schema": "2"}) == 0
    assert disk_schema(tmp_path / "nope.json") is None
    (tmp_path / "kapot.json").write_text("{half", encoding="utf-8")
    assert disk_schema(tmp_path / "kapot.json") == 0
    (tmp_path / "n.json").write_text('{"schema": 9}', encoding="utf-8")
    assert disk_schema(tmp_path / "n.json") == 9
    assert too_new(tmp_path / "n.json", 1) and not too_new(tmp_path / "n.json", 9)
    assert not too_new(tmp_path / "nope.json", 1)


def test_write_json_refuses_a_newer_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"schema": 2, "x": 1}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SchemaTooNew) as e:
        write_json(path, {"x": 2}, 1)
    assert e.value.path == path
    assert path.read_bytes() == before
    assert not (tmp_path / "state.json.bak").exists()


def test_write_json_keeps_an_older_file_once_as_bak(tmp_path):
    path = tmp_path / "niet_thuis.json"
    path.write_text('{"vrijdag": 2}', encoding="utf-8")
    write_json(path, {"vrijdag": 3}, 1)
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema": 1, "vrijdag": 3}
    bak = tmp_path / "niet_thuis.json.bak"
    assert bak.read_text(encoding="utf-8") == '{"vrijdag": 2}'
    write_json(path, {"vrijdag": 4}, 1)  # same version now: no new backup
    assert bak.read_text(encoding="utf-8") == '{"vrijdag": 2}'


def test_write_json_new_file_has_no_bak(tmp_path):
    write_json(tmp_path / "a.json", {"schema": 0, "k": 1}, 1)
    assert json.loads((tmp_path / "a.json").read_text(encoding="utf-8")) == {"schema": 1, "k": 1}
    assert not (tmp_path / "a.json.bak").exists()


def test_migrate_runs_each_step_and_leaves_newer_alone():
    steps = {0: lambda d: {**d, "schema": 1, "m0": True},
             1: lambda d: {**d, "schema": 2, "m1": True}}
    assert migrate({"a": 1}, 2, steps, "x") == {"a": 1, "schema": 2, "m0": True, "m1": True}
    assert migrate({"schema": 1}, 2, steps, "x") == {"schema": 2, "m1": True}
    assert migrate({"schema": 5}, 2, steps, "x") == {"schema": 5}
