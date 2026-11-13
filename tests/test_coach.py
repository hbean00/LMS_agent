"""코치 카드(계약 22-4): 주간 브리핑(근거 id·닫기는 그 주만), 주간 회고(월·화만), 종강 리포트, 바뀐 마감(그대로 인용),
백그라운드 알림 순서(신규 마감 > 주간 브리핑 주 1회 > 보상).

데모(가짜 Canvas)의 '오늘'을 오늘 이전의 가장 가까운 금요일로 고정해 요일 모양이 늘 같게 한다(시각 비교는 같은 순간인지로).
T=금요일: LAB4 후보 T+6(목, 공지) → T+8(토, 메시지 '마감 연장'), Homework 5 T+4(화, Canvas), Quiz T+5(수, 시험),
중간 프로젝트 보고서 T+7(금), LAB5 T+10(월, Canvas), 기말고사 T+30 15:00(시험), LAB3 T-5(일, 제출함)."""
import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta

import pytest

from lms_agent import cli, coach, core, course_modes, gradebook, notify
from lms_agent.canvas import Canvas
from lms_agent.config import save_config
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
_today = date.today()
T = _today - timedelta(days=(_today.weekday() - 4) % 7)   # 오늘 또는 그 전의 금요일


def at(n: int, hh: int = 9, mm: int = 0) -> datetime:
    return datetime.combine(T + timedelta(days=n), time(hh, mm), tzinfo=TZ)


def md(n: int) -> str:
    d = T + timedelta(days=n)
    return f"{d.month}월 {d.day}일"


@pytest.fixture()
def mock():
    srv, base = start_mock_canvas(today=T)
    yield srv, base
    srv.shutdown()


@pytest.fixture()
def store(tmp_path, mock):
    _, base = mock
    s = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), s)
    return s


def by_title(store, title, due_day=None):
    for v in store.view():
        if v["title"] == title and (due_day is None or parse_iso(v["due"]).date() == T + timedelta(days=due_day)):
            return v
    raise AssertionError(title)


def line(card, tone):
    return next(ln for ln in card["lines"] if ln["tone"] == tone)


def all_ids(card):
    return {i for ln in card["lines"] for i in ln["item_ids"]}


# ---------------------------------------------------------------- C. 주간 브리핑

def test_briefing_counts_a_task_once_and_cites_items(store):
    now = at(3)                                                      # 다음 주 월요일 09:00
    card = coach.state(store, now=now)["briefing"]
    monday = (T + timedelta(days=3)).isoformat()
    assert card["id"] == f"briefing:{monday}" and card["kind"] == "briefing" and card["created_for"] == monday
    sem = core.semester_info(store.data["cache"], store.view(), today=now.date())
    assert card["week"] == sem["week_now"] and card["title"] == f"{sem['week_now']}주차 작전"
    assert {ln["tone"] for ln in card["lines"]} <= set(coach.TONES)
    hw5 = by_title(store, "Homework 5")
    lab4_old, lab4_new = by_title(store, "LAB4 보고서", 6), by_title(store, "LAB4 보고서", 8)
    mid = by_title(store, "중간 프로젝트 보고서")
    dl = line(card, "deadline")
    # 같은 과제의 두 후보(LAB4 목·토)는 하나로 — 더 최근 글(메시지)의 토요일 후보
    assert set(dl["item_ids"]) == {hw5["id"], lab4_new["id"], mid["id"]} and lab4_old["id"] not in all_ids(card)
    assert dl["text"].startswith("이번 주 마감 3개") and "고비" not in dl["text"]   # 하루에 2개 이상 몰린 날은 없다
    warn = line(card, "warn")
    assert set(warn["item_ids"]) == {lab4_new["id"], mid["id"]} and "2개는 아직 확인 전" in warn["text"]
    exam = line(card, "exam")
    assert exam["item_ids"] == [by_title(store, "Quiz")["id"], by_title(store, "기말고사")["id"]]
    assert "D-2 " in exam["text"] and "D-27 " in exam["text"]
    pace = line(card, "pace")
    assert "km" in pace["text"] and pace["item_ids"] == []
    assert not [ln for ln in card["lines"] if ln["tone"] == "study"]   # 데모에는 성적 추정이 없다
    # 같은 주라면 일요일에도 같은 카드
    assert coach.state(store, now=at(9, 22))["briefing"]["id"] == card["id"]


def test_briefing_busiest_day_and_what_is_left(store):
    store.add_manual("반도체공학", f"과제 3 마감: {md(7)} 23:59 까지 이메일로 제출", posted_at=(T + timedelta(days=1)).isoformat())
    core.rebuild(store)
    extra = by_title(store, "과제 3")
    card = coach.state(store, now=at(3))["briefing"]
    dl = line(card, "deadline")
    assert extra["id"] in dl["item_ids"] and dl["text"].startswith("이번 주 마감 4개")
    assert "금요일이 고비예요" in dl["text"]                             # 금요일: 중간 프로젝트 + 과제 3
    # 금요일 낮: 화요일 Homework 5는 지났다(제출 안 함) → 남은 것만 세고, 오늘이 고비
    dl = line(coach.state(store, now=at(7, 12))["briefing"], "deadline")
    assert dl["text"].startswith("이번 주 마감 4개 중 3개 남았어요") and "오늘이 고비예요" in dl["text"]
    # 다 내면 응원
    for v in store.view():
        if v["id"] in dl["item_ids"]:
            store.update_user(v["id"], {"submitted": True})
    first = coach.state(store, now=at(7, 12))["briefing"]["lines"][0]
    assert first["tone"] == "cheer" and "모두 끝냈어요" in first["text"]


def test_briefing_quiet_week_points_to_the_next_deadline(store):
    card = coach.state(store, now=at(-4))["briefing"]                  # 이번 주(T-4~T+2)는 마감 없음
    first = card["lines"][0]
    assert first["tone"] == "cheer" and "이번 주는 마감이 없어요" in first["text"]
    assert first["item_ids"] == [by_title(store, "Homework 5")["id"]] and "Homework 5" in first["text"]


def test_briefing_study_priority_without_letter_grades(store):
    gradebook.save_course(store, "102", {"components": [
        {"name": "중간고사", "weight": 30, "score": 78, "mean": 65.3, "sd": 12.1}, {"name": "기말고사", "weight": 70}]})
    card = coach.state(store, now=at(3))["briefing"]
    study = line(card, "study")
    assert study["text"].startswith("공부는 디지털신호처리부터") and "남은 비중 70%" in study["text"]
    assert not re.search(r"[A-DF][+0]", study["text"])                 # 학점 글자를 크게 드러내지 않는다
    dsp = {v["id"] for v in store.view() if v["course_id"] == "102"}
    assert study["item_ids"] and set(study["item_ids"]) <= dsp
    # 알림 문구에는 공부 줄(성적에서 나온 말)이 들어가지 않는다
    title, body = notify.briefing_message(card)
    assert title == f"LMS Agent · {card['week']}주차 작전"
    assert "공부" not in body and "학점" not in body and body.startswith("마감 3개")


def test_hidden_course_is_left_out(store):
    course_modes.set_mode(store, "101", "hidden")                      # ASIC 제외
    out = coach.state(store, now=at(3))
    asic = {v["id"] for v in store.view(include_hidden=True) if v["course_id"] == "101"}
    ids = all_ids(out["briefing"])
    assert asic and ids and not ids & asic
    assert out["changes"] == []                                         # LAB4(ASIC)의 바뀐 마감도 안 보인다


def test_no_cards_before_first_sync(tmp_path):
    assert coach.state(Store(tmp_path / "state.json")) == {"briefing": None, "retro": None, "report": None,
                                                           "changes": []}


# ---------------------------------------------------------------- 닫기 (화면 API)

def call(app, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_dismiss_hides_the_briefing_for_that_week_only(store, tmp_path):
    app = App(store, tmp_path, cfg={})
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    try:
        code, st = call(app, "GET", "/api/state")
        card = st["coach"]["briefing"]
        assert code == 200 and card and card["created_for"] == (now_local().date()
                                                               - timedelta(days=now_local().weekday())).isoformat()
        assert st["coach"]["changes"] and st["coach"]["changes"][0]["title"] == "LAB4 보고서"
        for bad in ({"id": "nope"}, {"id": 3}, {}, {"id": "briefing:2020-01-06"}):    # 모양이 틀렸거나 지금 카드가 아님
            code, err = call(app, "POST", "/api/coach/dismiss", bad)
            assert code == 400 and err["error"]
        code, st = call(app, "POST", "/api/coach/dismiss", {"id": card["id"]})
        assert code == 200 and st["coach"]["briefing"] is None
        assert Store(tmp_path / "state.json").data["coach"]["dismissed"] == [card["id"]]
        code, st = call(app, "POST", "/api/coach/dismiss", {"id": card["id"]})   # 두 번 닫아도 하나
        assert code == 200 and Store(tmp_path / "state.json").data["coach"]["dismissed"] == [card["id"]]
    finally:
        httpd.shutdown()
    monday = date.fromisoformat(card["created_for"])
    later = datetime.combine(monday + timedelta(days=6), time(23, 0), tzinfo=TZ)
    assert coach.state(store, now=later)["briefing"] is None                      # 그 주 일요일까지 안 보임
    nxt = coach.state(store, now=datetime.combine(monday + timedelta(days=7), time(8, 0), tzinfo=TZ))["briefing"]
    assert nxt and nxt["id"] != card["id"]                                        # 다음 주에는 다시


# ---------------------------------------------------------------- E. 주간 회고·종강 리포트

def test_retro_only_on_monday_and_tuesday(store):
    lab4 = by_title(store, "LAB4 보고서", 8)
    store.update_user(lab4["id"], {"decision": "approved"})            # 일정에 넣었지만 제출 표시 없음
    hw5 = by_title(store, "Homework 5")
    store.update_user(hw5["id"], {"submitted": True})
    mid = by_title(store, "중간 프로젝트 보고서")                       # 확인 안 한 채 지나감
    card = coach.state(store, now=at(10))["retro"]                      # 그다음 월요일: 지난주 = T+3~T+9
    assert card["kind"] == "retro" and card["id"] == f"retro:{(T + timedelta(days=10)).isoformat()}"
    assert (card["total"], card["done"]) == (2, 1)
    head = line(card, "deadline")
    assert head["text"] == "지난주 마감 2개 중 1개 제출했어요" and set(head["item_ids"]) == {lab4["id"], hw5["id"]}
    warns = [ln for ln in card["lines"] if ln["tone"] == "warn"]
    assert warns[0]["item_ids"] == [lab4["id"]] and "LAB4 보고서" in warns[0]["text"]
    assert warns[1]["item_ids"] == [mid["id"]] and "확인 안 한 후보 1개" in warns[1]["text"]
    assert coach.state(store, now=at(11, 21))["retro"]["id"] == card["id"]      # 화요일 밤에도
    for n in (12, 13, 14, 15, 16):                                              # 수~일에는 없다
        assert coach.state(store, now=at(n))["retro"] is None
    with store.locked():
        assert coach.dismiss(store, card["id"], now=at(10))
        store.write()
    assert coach.state(store, now=at(11))["retro"] is None                      # 닫으면 끝


def test_retro_all_clear_cheers(store):
    card = coach.state(store, now=at(-4))["retro"]                      # 지난주 = LAB3(제출함)뿐
    first = card["lines"][0]
    assert first["tone"] == "cheer" and "모두 제출" in first["text"]
    assert first["item_ids"] == [by_title(store, "LAB3 보고서")["id"]]
    assert coach.state(store, now=at(3))["retro"] is None               # 마감이 없던 주의 회고는 없다


def test_report_after_the_last_week(store):
    assert coach.state(store, now=at(30))["report"] is None
    now = at(60)
    out = coach.state(store, now=now)
    card = out["report"]
    sem = core.semester_info(store.data["cache"], store.view(), today=now.date())
    assert sem["week_now"] > sem["weeks_total"] and out["briefing"] is None
    assert card["kind"] == "report" and card["id"] == f"report:{sem['weeks'][-1]['end']}"
    head = card["lines"][0]
    lab3, hw5, lab5 = by_title(store, "LAB3 보고서"), by_title(store, "Homework 5"), by_title(store, "LAB5 예비보고서")
    assert head["text"] == "한 학기 마감 3개 중 1개를 냈어요" and set(head["item_ids"]) == {lab3["id"], hw5["id"], lab5["id"]}
    texts = [ln["text"] for ln in card["lines"]]
    assert "ASIC설계(영강): 마감 2개 중 1개 제출" in texts and "디지털신호처리: 마감 1개 중 0개 제출" in texts
    assert any("메달" in t for t in texts)
    with store.locked():
        assert coach.dismiss(store, card["id"], now=now)
        store.write()
    assert coach.state(store, now=at(70))["report"] is None              # 닫을 때까지만


# ---------------------------------------------------------------- D. 바뀐 마감

def test_changed_deadline_quotes_the_newer_message(store):
    (ch,) = coach.state(store)["changes"]
    lab4_old, lab4_new = by_title(store, "LAB4 보고서", 6), by_title(store, "LAB4 보고서", 8)
    assert ch["item_id"] == lab4_new["id"] and ch["from_item_id"] == lab4_old["id"] and ch["basis"] == "conflict"
    assert parse_iso(ch["from"]) == at(6, 23, 59) and parse_iso(ch["to"]) == at(8, 23, 59)
    assert ch["direction"] == "later" and ch["course_short"] == "ASIC설계(영강)" and ch["title"] == "LAB4 보고서"
    assert ch["source_title"] == "LAB4 보고서 마감 연장" and parse_iso(ch["posted_at"]) == at(-1, 20)
    assert "연장" in ch["quote"]
    bodies = [m["body"] for cv in store.data["cache"]["conversations"] for m in cv.get("messages") or []]
    assert any(ch["quote"] in " ".join(b.split()) for b in bodies)     # 원문에 그대로 있는 문장(지어내지 않음)
    assert coach.state(store, now=at(20))["changes"] == []               # 14일이 지나면 빠진다
    # 신규 메시지를 본 뒤에 옛 마감(목요일)을 직접 골랐다면 바뀐 마감으로 띄우지 않는다
    store.update_user(lab4_old["id"], {"decision": "approved"})
    assert coach.state(store)["changes"] == []
    # 옛 마감을 먼저 승인해 두었는데 연장 메시지가 나중에 왔다면(신규 후보는 '대체됨'으로 숨는다) 알려 준다
    store.data["user"][lab4_old["id"]]["decided_at"] = iso(at(-2))
    (ch,) = coach.state(store)["changes"]
    assert ch["item_id"] == lab4_new["id"] and ch["status"] == "replaced"


def test_changed_deadline_from_an_edited_post_and_a_canvas_extension(store):
    lab6 = by_title(store, "LAB 6")
    # 소식 카드가 기억한 옛 LAB 6 후보(같은 글이 고쳐져 사라짐)
    store.data["news_seen"] = {"init": iso(at(-3)), "items": {"f-gone": {
        "due": iso(at(11, 23, 59)), "c": "101", "t": "lab6", "l": None, "at": iso(at(-3))}}}
    # Homework 5: Canvas 마감(화 18:00)과 같은 공지가 있는데, 더 새 글이 목요일로 연장 → 같은 과제 묶음(겹친 후보 + 신규 후보)
    store.add_manual("디지털신호처리", f"Homework 5 마감이 {md(6)} 18:00으로 연장되었습니다.")
    # LAB5 예비보고서: Canvas 마감(월 23:59)뿐인데, Canvas 과제보다 새 글이 수요일로 연장(묶음 없음)
    store.add_manual(store.data["cache"]["courses"][0]["name"], f"LAB5 예비보고서 마감이 {md(12)} 23:59로 연장되었습니다.")
    core.rebuild(store)
    out = {c["item_id"]: c for c in coach.state(store)["changes"]}
    ed = out[lab6["id"]]
    assert ed["basis"] == "edited" and parse_iso(ed["from"]) == at(11, 23, 59) and parse_iso(ed["to"]) == at(13, 23, 59)
    assert ed["quote"] == " ".join(lab6["evidence"][-1]["text"].split())
    hw5 = by_title(store, "Homework 5", 6)
    hw = out[hw5["id"]]
    assert hw["basis"] == "conflict" and hw["from_item_id"] == "canvas-102-1021"
    assert parse_iso(hw["from"]) == at(4, 18) and parse_iso(hw["to"]) == at(6, 18) and "연장" in hw["quote"]
    lab5 = by_title(store, "LAB 5", 12)                                # 찾은 후보의 제목은 글의 이름("LAB 5")
    cp = out[lab5["id"]]
    assert cp["basis"] == "canvas_post" and cp["from_item_id"] == "canvas-101-1013" and cp["direction"] == "later"
    assert parse_iso(cp["from"]) == at(10, 23, 59) and parse_iso(cp["to"]) == at(12, 23, 59) and "연장" in cp["quote"]
    # 브리핑은 Homework 5를 한 번만(연장된 후보로) 센다
    dl = line(coach.state(store, now=at(3))["briefing"], "deadline")
    assert hw5["id"] in dl["item_ids"] and "canvas-102-1021" not in dl["item_ids"]


def test_changes_need_a_real_quote(store):
    for it in store.data["items"]:                                      # 근거 문장이 비어 있으면 아무것도 지어내지 않는다
        for e in it.get("evidence") or []:
            e["text"] = ""
    assert coach.state(store)["changes"] == []


# ---------------------------------------------------------------- 알림 문구

def test_briefing_message_uses_deadline_and_exam_lines_only():
    card = {"kind": "briefing", "week": 11, "lines": [
        {"text": "이번 주 마감 4개, 목요일이 고비예요", "item_ids": ["a"], "tone": "deadline"},
        {"text": "그중 1개는 아직 확인 전이에요", "item_ids": ["a"], "tone": "warn"},
        {"text": "시험 D-5 ASIC Quiz 11/18(수)", "item_ids": ["b"], "tone": "exam"},
        {"text": "공부는 ASIC부터 — 한 단계 위 학점이 가까워요", "item_ids": [], "tone": "study"},
        {"text": "지금 27.3km 지점", "item_ids": [], "tone": "pace"}]}
    assert notify.briefing_message(card) == ("LMS Agent · 11주차 작전",
                                             "마감 4개, 목요일이 고비예요 · 시험 D-5 ASIC Quiz 11/18(수)")
    assert notify.briefing_message(None) is None and notify.briefing_message({"kind": "retro", "lines": []}) is None
    long = {"kind": "briefing", "week": 3, "lines": [{"text": "가" * 400, "item_ids": [], "tone": "cheer"}]}
    assert len(notify.briefing_message(long)[1]) <= 190


# ---------------------------------------------------------------- 백그라운드 알림 순서

@pytest.fixture()
def cli_home(tmp_path, mock, monkeypatch):
    _, base = mock
    monkeypatch.setenv("LMS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("LMS_AGENT_CANVAS_URL", raising=False)
    monkeypatch.delenv("LMS_AGENT_TOKEN", raising=False)
    save_config({"canvas_url": base, "token": DEMO_TOKEN}, tmp_path)
    shown = []
    monkeypatch.setattr(notify, "toast", lambda title, body: shown.append((title, body)) or True)
    return tmp_path, shown


def test_background_toasts_deadlines_then_briefing_once_a_week_then_reward(cli_home, monkeypatch, capsys):
    from lms_agent import celebrate
    home, shown = cli_home
    cli.main(["sync", "--background"])                                   # 첫 동기화: 신규 마감 후보 알림이 먼저
    assert len(shown) == 1 and shown[0][0].startswith("LMS Agent · 신규 마감 후보")
    assert not (Store(home / "state.json").data.get("coach") or {}).get("toasted")   # 브리핑은 다음 동기화로 밀림
    cli.main(["sync", "--background"])                                   # 신규 마감 없음 → 이번 주 작전
    card = coach.state(Store(home / "state.json"))["briefing"]
    assert shown[1][0] == f"LMS Agent · {card['week']}주차 작전" == notify.briefing_message(card)[0]
    assert shown[1][1] == notify.briefing_message(card)[1] and "점" not in shown[1][1].replace("지점", "")
    assert f"알림: LMS Agent · {card['week']}주차 작전" in capsys.readouterr().out
    assert Store(home / "state.json").data["coach"]["toasted"] == [card["created_for"]]

    real_state = celebrate.state
    reward = {"id": "week_clear:t:x", "kind": "week_clear", "title": "11주차 완주", "count": 3}

    def fake_state(store, sem, items, now=None):
        out, updates = real_state(store, sem, items, now)
        return {**out, "pending": [reward]}, updates
    monkeypatch.setattr(celebrate, "state", fake_state)
    cli.main(["sync", "--background"])                                   # 브리핑은 이번 주에 이미 → 보상
    assert len(shown) == 3 and shown[2][0] == "LMS Agent · 11주차 완주"
    cli.main(["sync", "--background"])                                   # 더 알릴 것 없음
    assert len(shown) == 3
    real_now = now_local()
    monkeypatch.setattr(coach, "now_local", lambda: real_now + timedelta(days=7))
    cli.main(["sync", "--background"])                                   # 다음 주 첫 백그라운드 동기화 → 다시 작전
    assert len(shown) == 4 and shown[3][0] == f"LMS Agent · {card['week'] + 1}주차 작전"
    assert len(Store(home / "state.json").data["coach"]["toasted"]) == 2


def test_no_briefing_toast_when_the_app_ran_this_week_or_card_closed(cli_home):
    home, shown = cli_home
    cli.main(["sync"])                                                   # 이번 주에 CLI(또는 앱)로 동기화함
    cli.main(["sync", "--background"])
    assert not [t for t in shown if "작전" in t[0]]
    # 백그라운드만 돈 주라도 카드를 닫았으면 알리지 않는다
    store = Store(home / "state.json")
    store.data["runs"] = [dict(r, by="background") for r in store.data["runs"]]
    with store.locked():
        assert coach.dismiss(store, coach.state(store)["briefing"]["id"])
        store.write()
    cli.main(["sync", "--background"])
    assert not [t for t in shown if "작전" in t[0]]
    store = Store(home / "state.json")
    store.data["coach"]["dismissed"] = []
    store.save()
    cli.main(["sync", "--background"])
    assert [t for t in shown if "작전" in t[0]]                           # 닫지 않았다면 알렸을 것
