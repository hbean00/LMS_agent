

def test_write_retries_when_windows_says_file_in_use(tmp_path, monkeypatch):
    """Windows에서 다른 스레드가 state.json을 읽는 순간 os.replace가 PermissionError → 잠깐 뒤 다시(리드, v0.8.4 CI에서 실제로 남)."""
    import os as _os
    from lms_agent import store as store_mod
    s = store_mod.Store(tmp_path / "state.json")
    calls = {"n": 0}
    real = _os.replace

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "file in use")
        return real(a, b)
    monkeypatch.setattr(store_mod.os, "replace", flaky)
    monkeypatch.setattr(store_mod, "REPLACE_WAIT", 0)
    with s.locked():
        s.data["settings"]["notify"] = False
        s.write()
    assert calls["n"] == 3 and store_mod.Store(tmp_path / "state.json").data["settings"]["notify"] is False
