"""성적 공개 반응과 학기 중 보상(계약 11장, 11-1장): 데모의 중간고사 메시지·LAB3 채점, 본 것 저장,
처음 실행 때 오래된 점수 조용히 넘기기, 다시 동기화해도 한 번만, 보상 종류별 조건과 하루 2개 제한."""
import json
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta

import pytest

from lms_agent import celebrate, core
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz

TZ = local_tz()


# ---------------------------------------------------------------- 데모(가짜 Canvas) + 화면 API

@pytest.fixture()
def demo(tmp_path):
    mock, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), store)
    app = App(store, tmp_path, cfg={})
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    yield app, base, store
    httpd.shutdown()
    mock.shutdown()


def call(app, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def grades_of(st):
    return [p for p in st["celebrations"]["pending"] if p["kind"] in ("graded", "score")]


def test_demo_midterm_message_and_canvas_lab3(demo):
    app, _, _ = demo
    code, st = call(app, "GET", "/api/state")
    assert code == 200
    cel = st["celebrations"]
    mid = next(p for p in cel["pending"] if p["kind"] == "score" and p["course_short"] == "디지털신호처리")
    assert (mid["score"], mid["mean"], mid["sd"]) == (78, 65.3, 12.1)
    assert mid["band"] == "A" and mid["letter"] == "A+" and mid["exam"] == "midterm" and mid["basis"] == "score"
    assert 14 < mid["top"] < 15.5
    assert mid["source_title"] == "중간고사 성적 안내"
    lab3 = next(p for p in cel["pending"] if p["kind"] == "graded")
    assert lab3["title"] == "LAB3 보고서" and (lab3["score"], lab3["max_score"]) == (9, 10)
    assert lab3["band"] is None and lab3["letter"] is None and lab3["top"] is None   # 평균이 없으면 등급을 지어내지 않는다
    assert lab3["exam"] == "assignment"
    # 마라톤: km = 42.195 × 주차 / 전체 주
    sem = st["semester"]
    assert cel["week_now"] == sem["week_now"] and cel["weeks_total"] == sem["weeks_total"]
    assert cel["km"] == round(42.195 * sem["week_now"] / sem["weeks_total"], 1) and cel["total_km"] == 42.195
    # 첫 동기화 → 합류 인사(start)만. 합류 전에 지난 이정표·완주 주는 메달이 아니다(계약 13-4)
    assert [p["kind"] for p in cel["pending"] if p["kind"] not in ("graded", "score")] == ["start"]
    assert cel["medals"] == []
    assert cel["joined_km"] == cel["km"] and cel["joined_at"]
    assert [m["before_join"] for m in cel["milestones"]] == [m["reached"] for m in cel["milestones"]]
    # 성적 반응의 근거: 학점 비율은 기본값, 만점 미기재 점수는 100점 가정
    assert mid["letter_basis"] == "default" and mid["max_assumed"] is True
    assert lab3["letter_basis"] is None and lab3["max_assumed"] is False


def test_seen_persists_and_no_duplicate_after_resync(demo, tmp_path):
    app, base, store = demo
    _, st = call(app, "GET", "/api/state")
    first = [p["id"] for p in st["celebrations"]["pending"]]
    assert len(first) == len(set(first))
    core.sync(Canvas(base, DEMO_TOKEN), store)                # 아직 안 본 채로 다시 동기화 → 그대로 한 번씩
    _, st2 = call(app, "GET", "/api/state")
    assert [p["id"] for p in st2["celebrations"]["pending"]] == first

    code, st3 = call(app, "POST", "/api/celebrations/seen", {"ids": first + ["모르는-id"]})
    assert code == 200 and st3["celebrations"]["pending"] == []
    assert not any(m["kind"] == "start" for m in st3["celebrations"]["medals"])   # 출발은 합류 인사 — 메달 아님
    assert Store(tmp_path / "state.json").data["celebrated"]  # 파일에 남았다

    core.sync(Canvas(base, DEMO_TOKEN), store, full=True)     # 전체 다시 읽어도 다시 뜨지 않는다
    _, st4 = call(app, "GET", "/api/state")
    assert st4["celebrations"]["pending"] == []
    fresh = App(Store(tmp_path / "state.json"), tmp_path, cfg={})   # 앱을 다시 켜도
    assert fresh.state()["celebrations"]["pending"] == []


def test_seen_rejects_bad_body(demo):
    app, _, _ = demo
    assert call(app, "POST", "/api/celebrations/seen", {"ids": "x"})[0] == 400
    assert call(app, "POST", "/api/celebrations/seen", {"ids": [1, 2]})[0] == 400


def test_regrade_shows_again(demo):
    app, _, store = demo
    _, st = call(app, "GET", "/api/state")
    call(app, "POST", "/api/celebrations/seen", {"ids": [p["id"] for p in st["celebrations"]["pending"]]})
    lab3 = next(a for a in store.data["cache"]["assignments"] if a["name"] == "LAB3 보고서")
    lab3["submission"]["score"] = 10.0                       # 점수가 바뀜(재채점)
    _, st2 = call(app, "GET", "/api/state")
    again = grades_of(st2)
    assert [(p["kind"], p["score"]) for p in again] == [("graded", 10.0)]


def test_old_grades_are_quiet_on_first_run(tmp_path):
    mock, base = start_mock_canvas()
    try:
        store = Store(tmp_path / "state.json")
        core.sync(Canvas(base, DEMO_TOKEN), store)
        old = iso(datetime.now(TZ) - timedelta(days=40))
        for a in store.data["cache"]["assignments"]:
            if a.get("submission", {}).get("score") is not None:
                a["submission"]["submitted_at"] = old          # 오래전에 채점된 과제
        for cv in store.data["cache"]["conversations"]:
            for m in cv.get("messages") or []:
                m["created_at"] = old
        app = App(store, tmp_path, cfg={})
        st = app.state()
        assert grades_of(st) == []                            # 쏟아지지 않는다
        assert store.data["celebrate_init"]
        saved = Store(tmp_path / "state.json").data
        assert saved["celebrate_init"] and any(k.startswith("graded:") for k in saved["celebrated"])
        # 처음 계산 뒤에 새로 채점된 것은 오래된 과제라도 뜬다
        lab3 = next(a for a in store.data["cache"]["assignments"] if a["name"] == "LAB3 보고서")
        lab3["submission"]["score"] = 7.5
        assert [p["score"] for p in grades_of(app.state())] == [7.5]
    finally:
        mock.shutdown()


def test_nothing_before_first_sync(tmp_path):
    app = App(Store(tmp_path / "state.json"), tmp_path, cfg={})
    cel = app.state()["celebrations"]
    assert cel["pending"] == [] and cel["medals"] == []
    assert "celebrate_init" not in app.store.data              # 첫 동기화 전에는 '처음' 표시를 하지 않는다


# ---------------------------------------------------------------- 보상: 저장 없이 함수로

MON = date(2026, 9, 7)  # 1주차 월요일


def sem_at(day: date, total=16):
    weeks = [{"n": i + 1, "start": (MON + timedelta(7 * i)).isoformat(), "end": (MON + timedelta(7 * i + 6)).isoformat()}
             for i in range(total)]
    wn = (day - MON).days // 7 + 1
    return {"start": MON.isoformat(), "end": weeks[-1]["end"], "weeks": weeks, "week_now": wn, "weeks_total": total,
            "source": "canvas"}


def at(day: date, h=12):
    return datetime.combine(day, time(h, 0), tzinfo=TZ)


def due_in(week: int, submitted=False, status="scheduled", n=0):
    d = MON + timedelta(7 * (week - 1) + 3)
    return {"id": f"w{week}-{n}", "status": status, "kind": "과제", "due": iso(at(d, 23)), "submitted": submitted,
            "week": week}


@pytest.fixture()
def bare(tmp_path):
    s = Store(tmp_path / "s.json")
    s.data["cache"] = {"courses": [], "assignments": [], "announcements": [], "conversations": []}
    return s


def run(store, day, items, h=12):
    """한 번 계산하고 저장할 것을 합친다(화면이 상태를 물은 것과 같다)."""
    out, up = celebrate.state(store, sem_at(day), items, at(day, h))
    celebrate.apply(store.data, up)
    return out


def rewards(out):
    return [p for p in out["pending"] if p["kind"] not in ("graded", "score")]


def see_all(store, day, items, h=12):
    out = run(store, day, items, h)
    celebrate.mark_seen(store, sem_at(day), items, [p["id"] for p in out["pending"]], at(day, h))


def test_start_and_km_milestone(bare):
    day = MON + timedelta(7 * 6)          # 7주차 → 18.5km
    out = run(bare, day, [])
    assert [p["kind"] for p in rewards(out)] == ["start"] and "18.5km 지점에서 합류" in rewards(out)[0]["title"]
    assert out["medals"] == [] and out["joined_km"] == 18.5          # 합류 전에 지난 이정표는 메달이 아니다
    assert [(m["km"], m["before_join"]) for m in out["milestones"] if m["reached"]] == [(5.0, True), (10.0, True)]
    assert out["next"]["km"] == 21.1
    see_all(bare, day, [])
    out = run(bare, day + timedelta(days=7), [])               # 8주차 → 21.1km 하프
    km = rewards(out)
    assert [(p["kind"], p["km"]) for p in km] == [("km", 21.1)] and km[0]["title"] == "반환점 돌았어요"
    assert out["km"] == 21.1 and out["joined_km"] == 18.5
    see_all(bare, day + timedelta(days=7), [])
    out = run(bare, day + timedelta(days=7), [])
    assert [(m["km"], m["title"]) for m in out["medals"]] == [(21.1, "반환점 돌았어요")]   # 배너·메달·토스트 같은 이름
    assert not next(m for m in out["milestones"] if m["km"] == 21.1)["before_join"]


def test_first_week_start_title(bare):
    out = run(bare, MON + timedelta(days=1), [])
    assert rewards(out)[0]["title"].startswith("출발! 16주 마라톤")


def test_finish_line(bare):
    last = MON + timedelta(7 * 15 + 2)
    see_all(bare, last - timedelta(days=7), [])
    out = run(bare, last, [])
    assert out["finished"] and out["km"] == 42.195 and out["next"] is None
    fin = rewards(out)
    assert fin and fin[0]["kind"] == "km" and fin[0]["finish"] and fin[0]["title"] == "한 학기 완주!"


def test_week_clear_after_submitting(bare):
    day = MON + timedelta(7 * 4)          # 5주차 월요일
    items = [due_in(4, submitted=False), due_in(4, submitted=True, n=1), due_in(5)]
    see_all(bare, day - timedelta(days=7), items)   # 4주차에 합류(출발 인사 확인)
    see_all(bare, day, items)
    assert rewards(run(bare, day, items)) == []                # 하나라도 안 냈으면 완주 아님
    items[0]["submitted"] = True
    out = run(bare, day, items)
    wc = rewards(out)
    assert [(p["kind"], p["week"], p["count"]) for p in wc] == [("week_clear", 4, 2)]
    assert wc[0]["title"] == "4주차 완주"
    # 이번 주(5주차)는 아직 지나지 않았으니 안 센다, 시험·확인 전 후보도 마감으로 안 센다
    items += [dict(due_in(4, n=2), kind="시험"), due_in(4, status="review", n=3)]
    assert [p["week"] for p in rewards(run(bare, day, items))] == [4]


def test_first_run_past_weeks_are_before_join_not_medals(bare):
    """합류(처음 계산) 전에 끝난 주의 완주·연속은 메달로 주지 않고, 나중에도 뜨지 않는다(계약 13-4)."""
    day = MON + timedelta(7 * 4)
    items = [due_in(2, True), due_in(3, True), due_in(4, True)]
    out = run(bare, day, items)
    assert [p["kind"] for p in rewards(out)] == ["start"]
    assert out["medals"] == [] and out["streak"] == 0
    see_all(bare, day, items)
    later = run(bare, day + timedelta(days=2), items)
    assert rewards(later) == [] and later["medals"] == []


def test_existing_account_pre_join_medals_hidden_on_read(bare):
    """실제 계정 모양: 11주차(29km)에 처음 계산했고, 예전 버전이 그때 지난 이정표·주 완주·연속을 조용히 메달함에 넣어 두었다.
    읽을 때 합류 전 메달과 출발 인사는 빼고 before_join으로만 보인다."""
    day = MON + timedelta(7 * 10 + 4)     # 11주차 금요일
    bare.data["celebrate_init"] = iso(at(day, 9))
    term = celebrate.term_of(sem_at(day))
    old = [{"id": f"km:{term}:5", "kind": "km", "km": 5.0, "title": "5km 통과", "at": "2026-09-14", "week": 2},
           {"id": f"km:{term}:10", "kind": "km", "km": 10.0, "title": "10km 통과", "at": "2026-09-28", "week": 4},
           {"id": f"km:{term}:21.1", "kind": "km", "km": 21.1, "title": "반환점 돌았어요", "at": "2026-10-26", "week": 8},
           {"id": f"week_clear:{term}:2026-10-05", "kind": "week_clear", "title": "5주차 완주", "at": "2026-10-11",
            "week": 5, "count": 2},
           {"id": f"streak:{term}:3", "kind": "streak", "title": "3주 연속 완주", "at": "2026-10-18", "week": 6, "n": 3},
           {"id": f"start:{term}", "kind": "start", "title": "29km 지점에서 합류했어요", "at": iso(at(day, 9)), "week": 11}]
    bare.data["medals"] = [dict(m, term=term, seen_at=iso(at(day, 9)), silent=m["kind"] != "start") for m in old]
    bare.data["celebrated"] = {m["id"]: "1" for m in old}
    out = run(bare, day, [])
    assert out["joined_km"] == 29.0 and out["joined_at"] == iso(at(day, 9))
    assert out["medals"] == [] and rewards(out) == []
    assert {m["km"] for m in out["milestones"] if m["before_join"]} == {5.0, 10.0, 21.1}
    assert not next(m for m in out["milestones"] if m["km"] == 30.0)["before_join"]
    assert len(bare.data["medals"]) == 6                     # 저장된 기록은 그대로(읽을 때만 뺀다)
    # 12주차 → 31.6km: 30km 이정표는 합류 뒤라 메달이 된다
    nxt = run(bare, day + timedelta(days=4), [])
    assert [(p["kind"], p["km"]) for p in rewards(nxt)] == [("km", 30.0)]


def test_streak_skips_empty_weeks_and_breaks_on_missed(bare):
    day = MON + timedelta(7 * 2)          # 3주차에 처음 실행
    items = [due_in(1, True), due_in(2, False)]
    see_all(bare, day, items)
    # 3·5·6주 완주, 4주차는 마감 없음(끊지 않음) → 7주차 월요일에 3주 연속
    items += [due_in(3, True), due_in(5, True), due_in(6, True)]
    day7 = MON + timedelta(7 * 6)
    out = run(bare, day7, items)
    kinds = [p["kind"] for p in rewards(out)]
    assert "streak" in kinds and out["streak"] == 3
    st = next(p for p in rewards(out) if p["kind"] == "streak")
    assert st["n"] == 3 and st["title"] == "3주 연속 완주"
    # 같은 종류가 여럿(3·5·6주차 완주) 밀려 있으면 가장 최근 것 하나만 띄운다
    assert [p["week"] for p in rewards(out) if p["kind"] == "week_clear"] == [6]
    items.append(due_in(7, False))
    out8 = run(bare, MON + timedelta(7 * 7), items)            # 7주차를 놓침 → 끊김
    assert out8["streak"] == 0


def test_collapsed_week_clears_all_go_to_medals_when_seen(bare):
    day = MON + timedelta(7 * 2)
    items = [due_in(1, False), due_in(2, False)]
    see_all(bare, MON, items)                                  # 1주차에 합류
    see_all(bare, day, items)
    items[0]["submitted"] = items[1]["submitted"] = True       # 늦게 체크 → 1·2주차 완주
    out = run(bare, day, items)
    wc = [p for p in rewards(out) if p["kind"] == "week_clear"]
    assert [p["week"] for p in wc] == [2]
    celebrate.mark_seen(bare, sem_at(day), items, [wc[0]["id"]], at(day))
    out = run(bare, day, items)
    assert {m["week"] for m in out["medals"] if m["kind"] == "week_clear"} == {1, 2}
    assert rewards(out) == []


def test_at_most_two_new_rewards_per_day(bare):
    day = MON + timedelta(7 * 4)          # 5주차(13.2km)
    items = [due_in(2), due_in(3), due_in(4)]
    see_all(bare, MON + timedelta(7), items)                   # 2주차(5.3km)에 합류
    for it in items:
        it["submitted"] = True                                 # 완주 3주 + 3주 연속 + 10km → 보상 3종류
    out = run(bare, day, items)
    assert [p["kind"] for p in rewards(out)] == ["streak", "week_clear"] and out["waiting"] == 1
    celebrate.mark_seen(bare, sem_at(day), items, [p["id"] for p in rewards(out)], at(day))
    assert rewards(run(bare, day, items, h=18)) == []          # 오늘은 끝
    tomorrow = day + timedelta(days=1)
    nxt = rewards(run(bare, tomorrow, items))
    assert [(p["kind"], p["km"]) for p in nxt] == [("km", 10.0)]


def test_inbox_zero_only_when_user_cleared_it(bare):
    day = MON + timedelta(7 * 3)
    review = [dict(due_in(5, n=i), status="review", id=f"r{i}") for i in range(3)]
    see_all(bare, day, review)
    assert bare.data["review_peak"]["count"] == 3
    for it in review:                                          # 사용자가 셋 다 정함
        it["status"] = "scheduled"
        bare.data["user"][it["id"]] = {"decision": "approved", "decided_at": iso(at(day, 13))}
    out = run(bare, day, review, h=14)
    iz = [p for p in rewards(out) if p["kind"] == "inbox_zero"]
    assert len(iz) == 1 and iz[0]["count"] == 3 and iz[0]["title"] == "확인함 비우기 완료"
    assert len([p for p in rewards(run(bare, day, review, h=15)) if p["kind"] == "inbox_zero"]) == 1  # 한 번만
    celebrate.mark_seen(bare, sem_at(day), review, [iz[0]["id"]], at(day, 15))
    out = run(bare, day, review, h=16)
    assert not [p for p in rewards(out) if p["kind"] == "inbox_zero"]
    assert any(m["kind"] == "inbox_zero" for m in out["medals"])


def test_inbox_zero_counts_assignments_without_deadline(bare):
    """마감 없는 과제가 남아 있으면 '놓친 마감 없음'이라 하지 않고 그 수를 보상 이름에 넣는다(계약 13-4)."""
    day = MON + timedelta(7 * 3)
    review = [dict(due_in(5, n=i), status="review", id=f"r{i}") for i in range(3)]
    nodl = {"id": "canvas-1-9", "status": "no_deadline", "kind": "과제", "due": None, "submitted": False, "week": None}
    see_all(bare, day, review + [nodl])
    for it in review:
        it["status"] = "scheduled"
        bare.data["user"][it["id"]] = {"decision": "approved", "decided_at": iso(at(day, 13))}
    out = run(bare, day, review + [nodl], h=14)
    iz = [p for p in rewards(out) if p["kind"] == "inbox_zero"]
    assert len(iz) == 1 and iz[0]["no_deadline_left"] == 1
    assert "1개" in iz[0]["title"] and "놓친" not in iz[0]["title"]
    celebrate.mark_seen(bare, sem_at(day), review + [nodl], [iz[0]["id"]], at(day, 15))
    medal = next(m for m in run(bare, day, review + [nodl], h=16)["medals"] if m["kind"] == "inbox_zero")
    assert medal["title"] == iz[0]["title"] and medal["no_deadline_left"] == 1      # 토스트와 메달이 같은 이름


def test_grade_news_fresh_only_for_scores_new_since_join(bare):
    """성적 소식 fresh: 합류 뒤 3일 안에 나온 점수가 있을 때만(계약 13-4). 오래된 점수만이면 '앱에서 아직 안 본 성적'."""
    day = MON + timedelta(7 * 4)
    now = at(day)
    bare.data["celebrate_init"] = iso(now - timedelta(days=10))
    bare.data["news_seen"] = {"init": "x", "items": {}, "materials": {}, "weights": {}}
    _news_store(bare, [], day)

    def graded(aid, when):
        return {"id": aid, "course_id": 1, "name": f"Quiz {aid}", "points_possible": 10,
                "submission": {"score": 8, "submitted_at": iso(when), "graded_at": iso(when)}}

    bare.data["cache"]["assignments"] = [graded(1, now - timedelta(days=5))]
    card = news_of(run(bare, day, []), "grades")[0]
    assert card["fresh"] is False
    bare.data["cache"]["assignments"].append(graded(2, now - timedelta(hours=5)))
    card = news_of(run(bare, day, []), "grades")[0]
    assert card["fresh"] is True and card["count"] == 2


def test_letter_basis_user_when_distribution_set(demo):
    app, _, store = demo
    store.data.setdefault("grades", {})["102"] = {"components": [], "class_size": None,
                                                  "dist": {"A+": 10, "A0": 20, "B+": 20, "B0": 20, "C+": 10, "C0": 10,
                                                           "D+": 0, "D0": 0, "F": 10}}
    mid = next(p for p in grades_of(app.state()) if p["kind"] == "score")
    assert mid["letter_basis"] == "user" and mid["letter"]


def test_inbox_zero_not_for_wipe_or_small_queue(bare):
    day = MON + timedelta(7 * 3)
    review = [dict(due_in(5, n=i), status="review", id=f"r{i}") for i in range(4)]
    see_all(bare, day, review)
    out = run(bare, day, [], h=14)                             # 결정 없이 사라짐(로그아웃·전체 다시 읽기 등)
    assert not [p for p in rewards(out) if p["kind"] == "inbox_zero"]
    two = [dict(due_in(5, n=i), status="review", id=f"s{i}") for i in range(2)]
    run(bare, day, two, h=15)
    for it in two:
        it["status"] = "scheduled"
        bare.data["user"][it["id"]] = {"decision": "rejected", "decided_at": iso(at(day, 15))}
    assert not [p for p in rewards(run(bare, day, two, h=16)) if p["kind"] == "inbox_zero"]   # 2개는 안 셈


def test_helpers():
    assert celebrate.exam_of("기말고사 성적") == "final" and celebrate.exam_of(None, "Midterm exam") == "midterm"
    assert celebrate.exam_of("Homework 5") == "assignment"
    assert [celebrate.band_of(x) for x in ("A+", "B0", "C+", "D0", "F", None)] == ["A", "B", "C", "D", "F", None]
    assert celebrate.km_of({"week_now": 0, "weeks_total": 16}) == 0
    assert celebrate.km_of({"week_now": 8, "weeks_total": 16}) == 21.1
    assert celebrate.km_of({"week_now": 17, "weeks_total": 16}) == 42.195


def test_course_estimate_used_when_no_mean(demo):
    """평균이 없는 점수라도 그 과목 성적 추정이 있으면 그 학점으로(근거: 과목)."""
    app, _, store = demo
    store.data.setdefault("grades", {})["101"] = {"components": [
        {"name": "중간고사", "weight": 40, "score": 90, "max_score": None, "mean": 70, "sd": 10, "fixed": False,
         "source": None}], "dist": None, "class_size": None}
    lab3 = next(p for p in grades_of(app.state()) if p["kind"] == "graded")
    assert lab3["basis"] == "course" and lab3["band"] == "A" and lab3["letter"] in ("A+", "A0")


# ---------------------------------------------------------------- 동기화 소식(가운데 큰 카드)

def news_of(out, kind=None):
    return [c for c in out["news"] if kind is None or c["kind"] == kind]


def test_demo_first_sync_is_one_summary_card_then_grades(demo):
    app, base, store = demo
    _, st = call(app, "GET", "/api/state")
    kinds = [c["kind"] for c in st["celebrations"]["news"]]
    assert kinds == ["first", "grades"]                       # 항목마다가 아니라 요약 한 장 + 성적 공개
    first = st["celebrations"]["news"][0]
    assert first["id"] == "news:first" and {r["label"] for r in first["items"]} >= {"일정에 넣은 마감", "확인할 후보"}
    rows = {r["label"]: r["n"] for r in first["items"]}
    assert rows["확인할 후보"] == sum(1 for v in st["items"] if v["status"] == "review")
    assert rows["일정에 넣은 마감"] + rows["확인할 후보"] == first["count_deadlines"]      # 겹치지 않게
    assert first["count_materials"] == rows.get("받은 자료", 0)
    assert first["count"] == first["count_deadlines"] + first["count_materials"]
    grades = st["celebrations"]["news"][1]
    assert grades["count"] == 2 and "score" not in grades["items"][0]   # 점수는 카드에서 미리 보여 주지 않는다
    assert grades["fresh"] is False                                       # 첫 실행: '앱에서 아직 안 본 성적'
    code, st = call(app, "POST", "/api/celebrations/seen", {"ids": ["news:first"]})
    assert [c["kind"] for c in st["celebrations"]["news"]] == ["grades"]
    core.sync(Canvas(base, DEMO_TOKEN), store)                 # 같은 내용으로 다시 동기화 → 새 소식 없음
    _, st = call(app, "GET", "/api/state")
    assert [c["kind"] for c in st["celebrations"]["news"]] == ["grades"]
    # 성적 반응을 보면 성적 소식도 사라진다
    gids = st["celebrations"]["news"][0]["grade_ids"]
    _, st = call(app, "POST", "/api/celebrations/seen", {"ids": gids})
    assert st["celebrations"]["news"] == []


def test_grade_news_snooze_twice_then_badge_only(demo):
    app, _, _ = demo
    call(app, "POST", "/api/celebrations/seen", {"ids": ["news:first"]})
    _, st = call(app, "GET", "/api/state")
    card = news_of(st["celebrations"], "grades")[0]
    _, st = call(app, "POST", "/api/celebrations/snooze", {"ids": card["grade_ids"]})
    again = news_of(st["celebrations"], "grades")                # 다음 실행 때 한 번 더
    assert again and again[0]["snoozed"]
    _, st = call(app, "POST", "/api/celebrations/snooze", {"ids": card["grade_ids"]})
    assert news_of(st["celebrations"], "grades") == []           # 두 번 미루면 큰 카드로는 안 뜸
    assert len([p for p in st["celebrations"]["pending"] if p["kind"] in ("graded", "score")]) == 2   # 표시는 남음
    assert call(app, "POST", "/api/celebrations/snooze", {"ids": "x"})[0] == 400


def _news_store(bare, items, day):
    bare.data["items"] = [dict(it) for it in items]
    bare.data["runs"] = [{"finished_at": iso(at(day, 9))}]
    return bare


def test_news_new_deadlines_changes_materials_weights(bare):
    day = MON + timedelta(7 * 4)
    a = dict(due_in(6), id="canvas-1-1", origin="canvas", course_id="1", title="LAB5")
    b = dict(due_in(6, n=1), id="f-old", origin="found", course_id="1", title="LAB4 보고서", status="review",
             linked_to="canvas-1-9")
    items = [a, b]
    _news_store(bare, items, day)
    out = run(bare, day, items)
    assert [c["kind"] for c in news_of(out)] == ["first"]
    celebrate.mark_seen(bare, sem_at(day), items, ["news:first"], at(day))
    assert news_of(run(bare, day, items)) == []                 # 본 뒤에는 조용

    # 새 마감 2개, Canvas 과제 마감 바뀜, 찾은 마감이 새 날짜로 바뀜(옛 항목은 사라짐), 자료 1개, 평가 비중 자동 입력
    a2 = dict(a, due=iso(at(MON + timedelta(7 * 5 + 5), 23)))
    b2 = dict(b, id="f-new", due=iso(at(MON + timedelta(7 * 6 + 1), 23)))
    c = dict(due_in(7, n=2), id="canvas-1-2", origin="canvas", course_id="1", title="Homework 6")
    d = dict(due_in(8, n=3), id="f-d", origin="found", course_id="2", title="기말 보고서", status="review")
    past = dict(due_in(2, n=4), id="canvas-1-3", origin="canvas", course_id="1", title="지난 과제")
    items2 = [a2, b2, c, d, past]
    _news_store(bare, items2, day)
    bare.data["downloads"] = {"1:10001": {"filename": "Lab6.pdf", "course_id": "1", "post_title": "Lab6"}}
    bare.data["grades"] = {"1": {"components": [{"name": "중간고사", "weight": 30, "auto": True},
                                                {"name": "기말고사", "weight": 40, "auto": True}]}}
    out = run(bare, day, items2, h=13)
    kinds = [c["kind"] for c in news_of(out)]
    assert kinds == ["deadlines", "changes", "materials", "weights"]
    dl = news_of(out, "deadlines")[0]
    assert dl["count"] == 2 and [i["title"] for i in dl["items"]] == ["Homework 6", "기말 보고서"]   # 지난 과제는 소식 아님
    assert dl["review_ids"] == ["f-d"]
    ch = news_of(out, "changes")[0]
    assert {(i["id"], i["old"], i["new"]) for i in ch["items"]} == {
        ("canvas-1-1", a["due"], a2["due"]), ("f-new", b["due"], b2["due"])}
    assert news_of(out, "materials")[0]["items"][0]["filename"] == "Lab6.pdf"
    w = news_of(out, "weights")[0]
    assert not w["changed"] and [x["name"] for x in w["items"][0]["components"]] == ["중간고사", "기말고사"]

    # 하나씩 봤어요 → 그 카드만 사라진다
    celebrate.mark_seen(bare, sem_at(day), items2, [dl["id"]], at(day, 14))
    assert [c["kind"] for c in news_of(run(bare, day, items2, h=14))] == ["changes", "materials", "weights"]
    out = run(bare, day, items2, h=14)
    celebrate.mark_seen(bare, sem_at(day), items2, [c["id"] for c in news_of(out)], at(day, 15))
    assert news_of(run(bare, day, items2, h=15)) == []
    # 평가 비중이 다시 바뀌면 '바뀌었어요'
    bare.data["grades"]["1"]["components"][0]["weight"] = 35
    w = news_of(run(bare, day, items2, h=16), "weights")[0]
    assert w["changed"] and w["items"][0]["changed"]


def test_news_conflict_from_new_post(bare):
    day = MON + timedelta(7 * 4)
    b = dict(due_in(6), id="f-1", origin="found", course_id="1", title="LAB4 보고서", status="review", linked_to="c-9")
    _news_store(bare, [b], day)
    see_all(bare, day, [b])
    celebrate.mark_seen(bare, sem_at(day), [b], ["news:first"], at(day))
    b2 = dict(b, id="f-2", due=iso(at(MON + timedelta(7 * 5 + 1), 23)), conflict="같은 과제에 다른 마감")
    b1 = dict(b, conflict="같은 과제에 다른 마감")
    _news_store(bare, [b1, b2], day)
    ch = news_of(run(bare, day, [b1, b2], h=13), "changes")
    assert len(ch) == 1 and ch[0]["items"][0]["conflict"] and ch[0]["items"][0]["old"] == b["due"]
    assert news_of(run(bare, day, [b1, b2], h=13), "deadlines") == []


def test_news_quiet_for_existing_accounts(bare):
    day = MON + timedelta(7 * 4)
    items = [dict(due_in(6), id="canvas-1-1", origin="canvas", course_id="1", title="LAB5")]
    _news_store(bare, items, day)
    bare.data["runs"] = [{"finished_at": "x"}, {"finished_at": "y"}]   # 이 기능 전부터 여러 번 동기화한 계정
    out = run(bare, day, items)
    assert news_of(out) == [] and bare.data["news_seen"]["init"]


def test_news_weights_changed_by_notice_shows_diff(bare):
    """공지로 평가 비중이 바뀌면(gradebook이 auto_previous를 남김) 무엇이 바뀌었는지 카드에 담는다."""
    day = MON + timedelta(7 * 4)
    _news_store(bare, [], day)
    bare.data["news_seen"] = {"init": "x", "items": {}, "materials": {}, "weights": {"2": "old-sig"}}
    bare.data["grades"] = {"2": {"components": [{"name": "중간고사", "weight": 30, "auto": True},
                                                {"name": "과제", "weight": 20, "auto": True},
                                                {"name": "출석", "weight": 10, "auto": True}],
                                 "auto_previous": [{"name": "중간고사", "weight": 30}, {"name": "과제", "weight": 30}],
                                 "auto_source": "Updated Grading Policy: …", "auto_by": "rules"}}
    w = news_of(run(bare, day, []), "weights")[0]
    row = w["items"][0]
    assert w["changed"] and row["by"] == "rules" and row["source"].startswith("Updated")
    assert row["diff"]["changed"] == [{"name": "과제", "from": 30, "to": 20}]
    assert [x["name"] for x in row["diff"]["added"]] == ["출석"] and row["diff"]["removed"] == []


def test_news_existing_account_keeps_last_24h_as_news(bare):
    """업데이트 직후(전부터 쓰던 계정) 첫 계산: 최근 24시간 안에 처음 보인 마감은 소식, 오래된 것은 조용히.
    실제 사례: 업데이트 뒤 첫 동기화가 LearningX에서 ASIC 마감 5개를 새로 찾았는데 '조용한 초기화'에 묻혔다."""
    day = MON + timedelta(7 * 4)
    now = at(day)
    old = dict(due_in(6), id="canvas-1-1", origin="canvas", course_id="1", title="LAB5",
               first_seen=iso(now - timedelta(days=3)))
    fresh = dict(due_in(8), id="f-lab6", origin="found", course_id="1", title="LAB6", status="review",
                 first_seen=iso(now - timedelta(hours=2)))
    _news_store(bare, [old, fresh], day)
    bare.data["runs"] = [{"finished_at": "x"}, {"finished_at": "y"}]
    out = run(bare, day, [old, fresh])
    cards = news_of(out, "deadlines")
    assert len(cards) == 1 and [i["id"] for i in cards[0]["items"]] == ["f-lab6"]
    assert bare.data["news_seen"]["init"] and "canvas-1-1" in bare.data["news_seen"]["items"]
    assert "f-lab6" not in bare.data["news_seen"]["items"]      # 볼 때까지 소식으로 남는다
