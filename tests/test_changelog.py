"""바뀐 점(계약 19-6): 앱에 든 changelog.json — 건너뛴 버전까지 모두, 최근 목록, 업데이트 상태 칸."""
from lms_agent import __version__, changelog


def test_entries_sorted_and_current_version_listed():
    vs = [e["version"] for e in changelog.entries()]
    assert vs and vs == sorted(vs, key=changelog._key, reverse=True)
    assert __version__ in vs, "새 버전을 낼 때 data/changelog.json에 바뀐 점을 적어 주세요"
    assert all(e["title"] and e["items"] for e in changelog.entries())


def test_between_includes_skipped_versions():
    got = [e["version"] for e in changelog.between("0.7.3", "0.8.2")]
    assert got == ["0.8.2", "0.8.1", "0.8.0"]
    assert changelog.between("0.8.2", "0.8.2") == [] and changelog.between(None, None) == []
    assert [e["version"] for e in changelog.between("v0.8.1", "0.8.2-rc1")] == ["0.8.2"]


def test_recent_is_capped_and_not_newer_than_current():
    got = changelog.recent("0.8.1", n=3)
    assert [e["version"] for e in got] == ["0.8.1", "0.8.0", "0.7.3"]


def test_update_status_has_changes_after_update(tmp_path):
    from lms_agent import updater
    from lms_agent.store import Store
    s = Store(tmp_path / "state.json")
    up = updater.Updater(s, tmp_path, fetch=lambda *a, **k: None)
    up._save({"applied": {"version": __version__, "from": "0.7.3", "at": "2026-11-15T10:00:00+09:00"}})
    st = up.status()
    assert st["updated"]["from"] == "0.7.3" and st["changes"] and st["changes"][0]["version"] == __version__
    assert "0.7.3" not in [e["version"] for e in st["changes"]] and st["changelog"]
    up.dismiss()                                   # 배너를 닫으면 바뀐 점 팝업도 다시 안 뜬다
    assert up.status()["changes"] == [] and up.status()["changelog"]
