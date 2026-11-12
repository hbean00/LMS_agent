"""점수·마감 → 평가 항목 연결, 세부 점수(LAB), 개수, 추정 펼치기, 시험 일정(계약 14장).

실제 ASIC(95079)와 같은 모양의 글(학번·점수는 가짜)로: 'Updated Grading Policy' 공지 → 평가 비중, Lab0/Lab1 점수표 PDF,
Lab3.pdf의 "Next labs: P&R (IC Compiler II) → STA (PrimeTime) → SRAM", 게시판 Lab0~Lab3·Midterm Project, 퀴즈 공지(Oct. 22 꼴).
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta, timezone

import pytest

from lms_agent import agent_cli, core, gradebook, links
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
T = datetime.now().astimezone().date()
CID = "95079"
SID = "2026000002"          # 가짜 학번
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
POLICY = ("<p>Dear All,</p><p>I have attached in the updated grading policy, which includes the midterm quiz (5%) "
          "and the final quiz (5%).</p><ul><li>Homework assignments: 30% (w/ short reports)</li>"
          "<li>Midterm project: 25% (w/ midterm report)</li><li>Midterm quiz: 5%</li>"
          "<li>Final project: 25% (w/ final report)</li><li>Final quiz: 5%</li><li>Attendance: 10%</li></ul>")
NEXT_LABS = "Next labs: P&R (IC Compiler II) → STA (PrimeTime) → SRAM"


def z(days, h=10, m=0):
    return datetime.combine(T + timedelta(days=days), time(h, m), tzinfo=TZ).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def md(days):
    d = T + timedelta(days=days)
    return f"{d.month}/{d.day}"


def en(days):
    d = T + timedelta(days=days)
    return f"{MONTHS[d.month - 1]}. {d.day}, {d.year}"


def score_text(label, shift=0):
    rows = [(18, 25), (17, 27), (15, 20), (19, 28), (12, 18), (16, 24), (20, 29), (14, 22)]
    lines = [f"{label} Score", "No Student ID Prelab(20) Report(30) Total(50)"]
    for i, (a, b) in enumerate(rows, 1):
        a, b = max(0, a - shift * (i % 3)), min(30, b + shift * (i % 2))
        lines.append(f"{i} {2026000000 + i} {a} {b} {a + b}")
    return "\n".join(lines)


def asic_cache(next_labs=NEXT_LABS, extra_announcements=()):
    def ann(aid, posted, title, msg):
        return {"id": aid, "title": title, "message": msg, "posted_at": posted, "context_code": f"course_{CID}",
                "html_url": f"https://canvas.example/courses/{CID}/discussion_topics/{aid}"}

    def post(pid, title, created, content, attachments=()):
        if content:   # 실제 게시판 글 모양: 제목 줄 + 제출 방법 + (due : …)
            head = title if "Project" in title else title.replace(" Assignment", "") + " Assignment"
            content = f"<p>{head} </p><p></p><p>Submit as a PDF via email : ta@example.ac.kr</p><p></p>{content}"
        return {"id": pid, "title": title, "content": content, "created_at": created, "updated_at": created,
                "post_url": None, "attachments": list(attachments)}

    lab3 = f"Lab3: Logic Synthesis\nwork in SYN/ for synthesis; PNR/ and STA/ come in the next labs\n{next_labs}\n6\n"
    pdf = {
        f"{CID}:cf1": {"text": score_text("Lab0"), "course_id": CID, "filename": "Lab0_score-1.pdf",
                       "post_key": "board:announcements-9007", "posted_at": iso(parse_iso(z(-23)))},
        f"{CID}:cf2": {"text": score_text("Lab1", 1), "course_id": CID, "filename": "Lab1_score-2.pdf",
                       "post_key": "board:announcements-9007", "posted_at": iso(parse_iso(z(-23)))},
        f"{CID}:502784": {"text": lab3, "course_id": CID, "filename": "Lab3.pdf",
                          "post_key": "board:83605-5", "posted_at": iso(parse_iso(z(-2)))},
    }
    quiz = (f"<p>As I have mentioned in the last lecture, we will have a short quiz session (+ explanation on midterm "
            f"project) in the regular class hour on {en(-22)} .</p>")
    return {
        "fetched_at": iso(now_local()), "errors": [], "probe": {}, "assignments": [], "conversations": [],
        "courses": [{"id": CID, "name": "262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반",
                     "course_code": "262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반", "syllabus_body": None,
                     "start_at": None, "end_at": None, "term_start": z(-74, 0), "term_end": z(38, 0),
                     "current_score": None}],
        "announcements": [ann(9006, z(-36), "Updated Grading Policy", POLICY),
                          ann(9008, z(-25), "Announcement about Midterm Week", quiz),
                          ann(9007, z(-23), "Lab0, Lab1 Score Announcement (re-announced)", "<p>Scores attached.</p>"),
                          *extra_announcements],
        "boards": [
            {"course_id": CID, "board_id": 83605, "board_title": "과제", "posts": [
                post(1, "Lab0", z(-50), f"<p>(due : 23:59 on {md(-46)})</p>"),
                post(2, "Lab1", z(-38), f"<p>(due : 13:30 on {md(-31)})</p>"),
                post(3, "Lab2", z(-28), f"<p>(due : 23:59 on {md(-21)})</p>"),
                post(4, "Midterm Project", z(-20), f"<p>(due : 23:59 on {md(-8)})</p>"),
                post(5, "Lab3 Assignment", z(-2), f"<p>(due : 23:59 on {md(5)})</p>",
                     [{"id": 502784, "filename": "Lab3.pdf"}])]},
            {"course_id": CID, "board_id": "announcements", "board_title": "공지", "source": "canvas", "posts": [
                post(9007, "Lab0, Lab1 Score Announcement (re-announced)", z(-23), "",
                     [{"id": "cf1", "filename": "Lab0_score-1.pdf"}, {"id": "cf2", "filename": "Lab1_score-2.pdf"}])]},
        ],
        "pdf_texts": pdf,
    }


def make_store(path, sid=SID, **kw):
    s = Store(path)
    cache = asic_cache(**kw)
    s.data.update(cache=cache, pdf_texts=dict(cache["pdf_texts"]), last_sync=cache["fetched_at"])
    if sid:
        s.data["settings"]["student_id"] = sid
    core.rebuild(s, mode="full")
    gradebook.auto_fill(s, core._sources(cache, [], now_local()), ai_enabled=False)
    return s


class _Sched:
    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


@pytest.fixture()
def app(tmp_path):
    a = App(make_store(tmp_path / "state.json"), tmp_path, cfg={}, scheduler=_Sched())
    httpd = start_server(a, 0)
    serve_in_thread(httpd)
    yield a
    httpd.shutdown()


def call(app, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def course(st, cid=CID):
    return next(c for c in st["grades"]["courses"] if c["course_id"] == cid)


def comp(st, name, cid=CID):
    return next(x for x in course(st, cid)["config"]["components"] if x["name"] == name)


# ---------------------------------------------------------------- 규칙(links.py)

ASIC = links.components_of([{"name": n} for n in ("Homework assignments", "Midterm project", "Midterm quiz",
                                                   "Final project", "Final quiz", "Attendance")],
                           ["Updated Grading Policy: Homework assignments: 30% (w/ short reports) / Midterm project: 25% "
                            "(w/ midterm report) / Midterm quiz: 5% / Final project: 25% (w/ final report) / Final quiz: 5% "
                            "/ Attendance: 10%"])


@pytest.mark.parametrize("label,title,want", [
    ("Lab0", "Lab0_score-1.pdf", "Homework assignments"),       # 실습 항목이 없으면 과제 항목
    ("LAB 3", "", "Homework assignments"),
    ("HW 2", "", "Homework assignments"),
    ("Report 2", "", "Homework assignments"),                   # 별칭 'short reports'(중간·기말이 아닌 것)
    ("Midterm report", "", "Midterm project"),                  # 별칭 'midterm report'
    ("Midterm quiz", "", "Midterm quiz"),
    ("Midterm", "Midterm project results", "Midterm project"),  # 출처 제목의 다른 낱말로 가름
    ("Attendance", "", "Attendance"),
    ("Quiz 2", "", None),                                       # Midterm quiz / Final quiz 둘 다
    ("중간고사", "", None),                                      # Midterm project / Midterm quiz 둘 다
    ("Final", "", None),
    (None, "", None),
])
def test_match_asic_components(label, title, want):
    m = links.match(label, ASIC, other_text=title)
    assert m.component == want, m.reason
    if want is None:
        assert "골라 주세요" in m.reason


def test_match_lab_tiers_and_alias_evidence():
    named = links.components_of([{"name": "실습"}, {"name": "과제"}])
    assert links.match("Lab 2", named).component == "실습"
    alias = links.components_of([{"name": "Coursework"}, {"name": "기말고사"}], ["Coursework: 40% (w/ lab reports) / 기말고사 60%"])
    m = links.match("Lab 1", alias)
    assert m.component == "Coursework" and m.evidence == "(w/ lab reports)"
    assert links.match("Lab 1", links.components_of([{"name": "중간고사"}])).component is None


def test_match_items_term_words_kind_and_half_semester():
    ev = "we will have a short quiz session (+ explanation on midterm project) in the regular class hour on Oct. 22, 2026 ."
    assert links.match("Quiz", ASIC, term_text=ev, kind="시험").component == "Midterm quiz"
    assert links.match("Quiz", ASIC, kind="시험").component is None
    assert links.match("Quiz", ASIC, kind="시험", early=True).component == "Midterm quiz"
    assert links.match("Quiz", ASIC, kind="시험", early=False).component == "Final quiz"
    assert links.match("Midterm Project", ASIC, kind="과제").component == "Midterm project"
    assert links.match("Midterm", ASIC, kind="과제").component == "Midterm project"   # 과제 마감 → 시험 아닌 쪽
    assert links.is_exam_component("Midterm quiz") and links.is_exam_component("기말고사")
    assert not links.is_exam_component("Midterm project") and not links.is_exam_component("Homework assignments")


@pytest.mark.parametrize("text,fam,n", [
    ("Lab 0~6 will be held in the lab room", "lab", 7),
    ("Schedule: Lab0-Lab6", "lab", 7),
    ("이번 학기 총 7개의 실습이 있습니다", "lab", 7),
    ("There are 7 labs this term.", "lab", 7),
    ("We will have seven lab assignments.", "lab", 7),
    ("과제 7회 제출", "hw", 7),
    ("8 Report", "report", None),                      # 시간표 줄(주차 번호)
    ("Week 10 Project1 Presentation", "project", None),
    ("Lab 3 Assignment", "hw", None),
    ("Lab 3 - 2026 fall", "lab", None),
    ("the next 3 labs use ICC2", "lab", None),
])
def test_stated_counts(text, fam, n):
    r = links.stated(text, fam)
    assert (r[0] if r else None) == n


def test_hint_next_labs_and_observed():
    text = f"Lab3: Logic Synthesis\nPNR/ and STA/ come in the next labs\n{NEXT_LABS}\n6"
    assert links.hint(text, "lab", True)[0] == 7            # 0번부터: 3 + 1 + 3
    assert links.hint(text, "lab", False)[0] == 6           # 1번부터: 3 + 3
    assert links.hint("Lab 4 안내\n다음 실습: STA, SRAM", "lab", True)[0] == 7
    assert links.hint("PNR/ and STA/ come in the next labs\nfoo", "lab", True) is None
    assert links.hint(NEXT_LABS, "lab", True) is None       # 지금 번호가 없으면 셀 수 없다
    assert links.hint(NEXT_LABS, "lab", True, extra_numbers=[3])[0] == 7   # 파일 이름 'Lab3.pdf'의 번호
    assert links.numbered("Lab0_score-1.pdf", "lab") == [0] and links.numbered("lab\n20", "lab") == []
    assert links.observed_low([0, 1, 2, 3]) == 4 and links.observed_low([1, 2, 3]) == 3 and links.observed_low([]) is None


# ---------------------------------------------------------------- 실제와 같은 모양(14-8, 14-9)

def test_score_candidate_keys_are_stable(tmp_path):
    s = make_store(tmp_path / "s.json", extra_announcements=[{
        "id": 9100, "title": "Quiz 1 결과", "message": "<p>Quiz 1: 8/10 (평균 6, 표준편차 1.5)</p>", "posted_at": z(-3),
        "context_code": f"course_{CID}", "html_url": None}])
    a, b = gradebook.candidates(s), gradebook.candidates(s)
    keys = [c["key"] for c in a]
    assert keys == [c["key"] for c in b] and len(set(keys)) == len(keys)
    assert {k.split(":")[0] for k in keys} == {"pdf", "txt"}
    txt = next(c for c in a if c["key"].startswith("txt:"))
    assert txt["key"] == gradebook.candidate_key(CID, "announcement", "Quiz 1 결과", txt["label"], txt["score"])
    assert txt["key"].startswith("txt:announcement:") and len(txt["key"].split(":")[2]) == 12


def test_real_shape_without_student_id(tmp_path):
    """학번이 없어도 개수는 보인다(관찰 low 4 = LAB 0~3, 단서 7). 점수표 PDF 2개가 학번을 기다린다."""
    s = make_store(tmp_path / "s.json", sid=None)
    app = App(s, tmp_path, cfg={}, scheduler=_Sched())
    st = app.state()
    hw = comp(st, "Homework assignments")
    assert hw["members"] == [] and hw["derived"] is None
    cnt = hw["count"]
    assert (cnt["n"], cnt["low"], cnt["basis"], cnt["by"], cnt["seen"]) == (7, 4, "hint", "rules", [0, 1, 2, 3])
    assert cnt["evidence"] == NEXT_LABS and cnt["source_title"] == "첨부 'Lab3.pdf'"
    assert {"component": "Homework assignments", "weight": 30.0, "count_left": 7, "range": None} in course(st)["remaining"]
    assert st["grades"]["score_files_waiting"] == 2 and st["grades"]["score_files_unmatched"] == []
    assert course(st)["auto"]   # 연결·개수가 평가 비중의 자동 표시를 지우지 않는다


def test_real_shape_members_count_and_estimate(app):
    st = app.state()
    hw = comp(st, "Homework assignments")
    assert [m["label"] for m in hw["members"]] == ["Lab0", "Lab1"]
    m0 = hw["members"][0]
    assert m0["key"] == f"pdf:{CID}:cf1" and m0["link_by"] == "rules" and "과제 항목" in m0["link_reason"]
    assert (m0["score"], m0["max_score"], m0["n"], m0["source_type"]) == (44.0, 50.0, 8, "pdf")
    assert m0["rank"] >= 1 and m0["mean"] is not None and m0["sd"] is not None
    assert hw["count"]["n"] == 7 and hw["derived"]["basis"] == "per_item" and hw["derived"]["received"] == 2
    assert hw["derived"]["score_pct"] == pytest.approx((44 / 50 + hw["members"][1]["score"] / 50) / 2 * 100, abs=0.01)
    c = course(st)
    assert {"component": "Homework assignments", "weight": 21.43, "count_left": 5, "range": None} in c["remaining"]
    # 추정: 세부 점수마다 30/7%, 남은 5개 = 30×5/7%
    est = c["estimate"]
    rows = {r["name"]: r for r in est["components"]}
    assert rows["Homework assignments · Lab0"]["weight"] == pytest.approx(30 / 7)
    assert rows["Homework assignments 남은 5개"]["weight"] == pytest.approx(150 / 7)
    assert sum(r["weight"] for r in est["components"]) == pytest.approx(100)
    assert any("단서 7개 중 2개 받음" in n and "Next labs" in n for n in est["notes"])
    left = [n for n in c["needed"] if n["component"] == "Homework assignments 남은 5개"]
    assert left and all(n["parent"] == "Homework assignments" for n in left)
    assert st["grades"]["priorities"] and "남은 비중 81.4%" in st["grades"]["priorities"][0]["reason"]
    # 같은 계산을 직접: expand_components는 members 없는 항목을 그대로 둔다
    plain = gradebook.expand_components([{"name": "기말", "weight": 40, "score": None}])
    assert plain == [{"name": "기말", "weight": 40, "score": None, "max_score": None, "mean": None, "sd": None,
                      "fixed": False}]
    cands = {x["key"]: x for x in st["grades"]["candidates"]}
    assert cands[f"pdf:{CID}:cf2"]["component"] == "Homework assignments" and cands[f"pdf:{CID}:cf2"]["link_by"] == "rules"
    assert st["grades"]["score_files_waiting"] == 0


def test_observed_only_count_averages(tmp_path):
    """단서가 없으면(관찰만) 남은 개수를 몰라 받은 것 평균으로 W 전체를 하나로(계약 14-5)."""
    s = make_store(tmp_path / "s.json", next_labs="(no hint here)")
    st = App(s, tmp_path, cfg={}, scheduler=_Sched()).state()
    hw = comp(st, "Homework assignments")
    assert (hw["count"]["n"], hw["count"]["low"], hw["count"]["basis"]) == (None, 4, "observed")
    assert hw["derived"]["basis"] == "average"
    rows = {r["name"]: r for r in course(st)["estimate"]["components"]}
    assert rows["Homework assignments"]["weight"] == 30 and rows["Homework assignments"]["status"] == "known"
    assert any("남은 개수를 몰라 받은 것 평균으로 어림" in n for n in course(st)["estimate"]["notes"])
    rem = next(r for r in course(st)["remaining"] if r["component"] == "Homework assignments")
    assert rem == {"component": "Homework assignments", "weight": None, "count_left": None, "range": [15.0, 30.0]}


def test_items_link_to_components_and_exam_schedule(app):
    st = app.state()
    its = {i["title"]: i for i in st["items"] if i["course_id"] == CID}
    for t in ("LAB 0", "LAB 1", "LAB 2", "LAB 3"):
        assert (its[t]["component"], its[t]["component_by"]) == ("Homework assignments", "rules")
    assert its["Quiz"]["kind"] == "시험" and its["Quiz"]["component"] == "Midterm quiz"
    assert "중간" in its["Quiz"]["component_reason"]
    assert its["Midterm Project"]["component"] == "Midterm project"
    hw = comp(st, "Homework assignments")
    assert [x["title"] for x in hw["schedule"]] == ["LAB 0", "LAB 1", "LAB 2", "LAB 3"]
    assert [x["past"] for x in hw["schedule"]] == [True, True, True, False] and hw["date_status"] == "announced"
    assert comp(st, "Midterm quiz")["date_status"] == "past" and comp(st, "Final quiz")["date_status"] == "not_announced"
    ex = st["exams"]
    assert ex["upcoming"] == []   # 지난 시험은 뺀다
    assert {"course_id": CID, "course_short": "ASIC설계(영강)", "component": "Final quiz", "weight": 5.0} in ex["not_announced"]
    assert not any(r["component"] in ("Midterm project", "Final project", "Attendance") for r in ex["not_announced"])


def test_upcoming_unlinked_exam_items(app):
    """평가 항목에 연결되지 않은 다가오는 시험 항목도 upcoming에(component·weight null), 지난 것·연결된 것과 겹치지 않게."""
    sec = gradebook.section(app.store, [{"id": CID, "short_name": "ASIC설계(영강)"}])
    future = {"id": "f-x", "course_id": "777", "course_short": "회로이론", "kind": "시험", "status": "review",
              "component": None, "title": "기말고사", "due": iso(now_local() + timedelta(days=3))}
    items = [future,
             {**future, "id": "f-past", "due": iso(now_local() - timedelta(days=1))},
             {**future, "id": "f-rej", "status": "rejected"}, {**future, "id": "f-rep", "status": "replaced"},
             {**future, "id": "f-hw", "kind": "과제"}, {**future, "id": "f-linked", "component": "Final quiz"}]
    up = gradebook.exams_of(sec, items)["upcoming"]
    assert up == [{"course_id": "777", "course_short": "회로이론", "component": None, "weight": None,
                   "title": "기말고사", "due": future["due"], "item_id": "f-x", "status": "review"}]
    assert gradebook.exams_of(sec)["upcoming"] == []


def test_upcoming_exam(tmp_path):
    s = make_store(tmp_path / "s.json", extra_announcements=[{
        "id": 9200, "title": "Final week", "context_code": f"course_{CID}", "html_url": None, "posted_at": z(-1),
        "message": f"<p>For the final week, we will have a short quiz session in the regular class hour on {en(9)} .</p>"}])
    ex = App(s, tmp_path, cfg={}, scheduler=_Sched()).state()["exams"]
    up = [r for r in ex["upcoming"] if r["component"] == "Final quiz"]
    assert len(up) == 1 and parse_iso(up[0]["due"]).date() == T + timedelta(days=9) and up[0]["status"] == "review"
    assert not any(r["component"] == "Final quiz" for r in ex["not_announced"])


# ---------------------------------------------------------------- 요청(14-6, 14-9)

def test_user_link_and_reset(app):
    key = f"pdf:{CID}:cf2"
    code, st = call(app, "POST", f"/api/grades/{CID}/link", {"key": key, "component": "midterm QUIZ"})
    assert code == 200
    assert [m["label"] for m in comp(st, "Homework assignments")["members"]] == ["Lab0"]
    mq = comp(st, "Midterm quiz")["members"]
    assert [(m["label"], m["link_by"]) for m in mq] == [("Lab1", "user")]
    assert course(st)["auto"]                          # save_course가 아니므로 auto 표시가 남는다
    code, st = call(app, "POST", f"/api/grades/{CID}/link", {"key": key, "component": None})
    assert code == 200 and len(comp(st, "Homework assignments")["members"]) == 1 and not comp(st, "Midterm quiz")["members"]
    cand = next(c for c in st["grades"]["candidates"] if c["key"] == key)
    assert cand["component"] is None and cand["link_by"] == "user"
    code, st = call(app, "POST", f"/api/grades/{CID}/link", {"key": key, "reset": True})
    assert code == 200 and len(comp(st, "Homework assignments")["members"]) == 2
    assert call(app, "POST", f"/api/grades/{CID}/link", {"key": key, "component": "없는 항목"})[0] == 400
    assert call(app, "POST", f"/api/grades/{CID}/link", {"component": "Final quiz"})[0] == 400
    assert "grade_links" in app.store.data and CID not in app.store.data["grade_links"]


def test_user_count(app):
    code, st = call(app, "POST", f"/api/grades/{CID}/count", {"component": "Homework assignments", "n": 10})
    assert code == 200
    cnt = comp(st, "Homework assignments")["count"]
    assert (cnt["n"], cnt["basis"], cnt["by"], cnt["low"]) == (10, "user", "user", 4)
    assert {"component": "Homework assignments", "weight": 24.0, "count_left": 8, "range": None} in course(st)["remaining"]
    code, st = call(app, "POST", f"/api/grades/{CID}/count", {"component": "Homework assignments", "n": None})
    assert code == 200 and comp(st, "Homework assignments")["count"]["basis"] == "hint"
    for bad in ({"component": "Homework assignments", "n": 0}, {"component": "Homework assignments", "n": 2.5},
                {"component": "Homework assignments", "n": True}, {"component": "없는 항목", "n": 3}):
        assert call(app, "POST", f"/api/grades/{CID}/count", bad)[0] == 400
    assert course(st)["auto"]


def test_user_item_component(app):
    st = app.state()
    quiz = next(i for i in st["items"] if i["title"] == "Quiz")
    code, st = call(app, "POST", f"/api/item/{quiz['id']}/component", {"component": "Final quiz"})
    assert code == 200
    q = next(i for i in st["items"] if i["id"] == quiz["id"])
    assert (q["component"], q["component_by"]) == ("Final quiz", "user")
    assert comp(st, "Final quiz")["date_status"] == "past" and comp(st, "Midterm quiz")["date_status"] == "not_announced"
    assert app.store.data["item_links"][quiz["id"]]["component"] == "Final quiz"
    code, st = call(app, "POST", f"/api/item/{quiz['id']}/component", {"component": None})
    q = next(i for i in st["items"] if i["id"] == quiz["id"])
    assert code == 200 and (q["component"], q["component_by"]) == (None, "user")
    # 되돌리기(reset): 사용자 연결을 지우면 규칙이 다시 정한다('직접 고름'으로 남지 않음)
    call(app, "POST", f"/api/item/{quiz['id']}/component", {"component": "Final quiz"})
    code, st = call(app, "POST", f"/api/item/{quiz['id']}/component", {"reset": True})
    q = next(i for i in st["items"] if i["id"] == quiz["id"])
    assert code == 200 and (q["component"], q["component_by"]) == ("Midterm quiz", "rules")
    assert quiz["id"] not in app.store.data["item_links"]
    assert call(app, "POST", f"/api/item/{quiz['id']}/component", {"reset": True})[0] == 200   # 없는 연결도 그대로 성공
    assert call(app, "POST", "/api/item/nope/component", {"reset": True})[0] == 404
    assert call(app, "POST", "/api/item/nope/component", {"component": None})[0] == 404
    assert call(app, "POST", f"/api/item/{quiz['id']}/component", {"component": "없는 항목"})[0] == 400
    assert call(app, "POST", f"/api/item/{quiz['id']}/component", {})[0] == 400


def test_item_link_follows_group_when_due_changes(tmp_path):
    """사용자가 연결한 찾은 후보의 마감이 바뀌어 id가 달라져도, 같은 그룹 키면 그 연결을 따른다."""
    s = make_store(tmp_path / "s.json")
    lab3 = next(v for v in s.view() if v["title"] == "LAB 3")
    gradebook.set_item_component(s, lab3["id"], {"component": "Final project"})
    s.data["item_links"]["f-other"] = s.data["item_links"].pop(lab3["id"])   # 옛 id로 남은 기록
    view = s.view()
    gradebook.link_items(s, view, core.semester_info(s.data["cache"], view))
    got = next(v for v in view if v["id"] == lab3["id"])
    assert (got["component"], got["component_by"]) == ("Final project", "user")
    # 되돌리기는 같은 그룹 키로 남은 옛 기록도 지운다
    gradebook.set_item_component(s, lab3["id"], {"reset": True})
    assert "f-other" not in s.data["item_links"]
    view = s.view()
    gradebook.link_items(s, view, core.semester_info(s.data["cache"], view))
    got = next(v for v in view if v["id"] == lab3["id"])
    assert (got["component"], got["component_by"]) == ("Homework assignments", "rules")


# ---------------------------------------------------------------- AI(14-2 map_score_item, 14-4 count_items)

def _fake(answer):
    calls = []

    def runner(args, input, timeout, cwd):
        calls.append(input)
        body = answer(input)
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)
        import subprocess
        return subprocess.CompletedProcess(args, 0, out, "")
    runner.calls = calls
    return runner


QUIZ1 = {"id": 9100, "title": "Quiz 1 결과", "message": "<p>Quiz 1: 8/10 (평균 6, 표준편차 1.5)</p>", "posted_at": z(-3),
         "context_code": f"course_{CID}", "html_url": None}


def test_ai_map_score_item_links_ambiguous_quiz(tmp_path):
    s = make_store(tmp_path / "s.json", extra_announcements=[QUIZ1])
    s.data["settings"]["ai_cli"] = True
    quiz_key = next(c["key"] for c in gradebook.candidates(s) if c["label"] == "Quiz 1")

    def answer(inp):
        if "### key:" in inp:
            assert "2026000002" not in inp            # 학번·점수표는 보내지 않는다
            return {"links": [{"key": quiz_key, "component": "Midterm quiz", "evidence": "Quiz 1 결과"}]}
        return {"count": None}
    runner = _fake(answer)
    session = agent_cli.AiSession(s, runner=runner, command="claude")
    r = gradebook.ai_links(s, session)
    assert r["linked"] == 1 and r["asked"] >= 1 and r["error"] is None
    assert s.data["grade_links_ai"][CID][quiz_key]["component"] == "Midterm quiz"
    st = App(s, tmp_path, cfg={}, scheduler=_Sched()).state()
    mq = comp(st, "Midterm quiz")["members"]
    assert [(m["label"], m["link_by"]) for m in mq] == [("Quiz 1", "ai")] and "근거" in mq[0]["link_reason"]
    n = len(runner.calls)
    gradebook.ai_links(s, agent_cli.AiSession(s, runner=runner, command="claude"))   # 기억 → 다시 묻지 않음
    assert len(runner.calls) == n
    # 사용자가 바꾸면 사용자가 이긴다
    gradebook.set_link(s, CID, {"key": quiz_key, "component": "Final quiz"})
    st = App(s, tmp_path, cfg={}, scheduler=_Sched()).state()
    assert [m["link_by"] for m in comp(st, "Final quiz")["members"]] == ["user"]


def test_ai_map_score_item_rejects_unknown_component_and_fake_quote(tmp_path):
    s = make_store(tmp_path / "s.json", extra_announcements=[QUIZ1])
    s.data["settings"]["ai_cli"] = True
    quiz_key = next(c["key"] for c in gradebook.candidates(s) if c["label"] == "Quiz 1")
    for bad in ({"key": quiz_key, "component": "Quiz grade", "evidence": "Quiz 1 결과"},
                {"key": quiz_key, "component": "Final quiz", "evidence": "지어낸 문장입니다"}):
        runner = _fake(lambda inp, bad=bad: {"links": [bad]} if "### key:" in inp else {"count": None})
        s.data.pop("grade_links_ai", None)
        r = gradebook.ai_links(s, agent_cli.AiSession(s, runner=runner, command="claude"))
        assert r["linked"] == 0
        assert (s.data.get("grade_links_ai") or {}).get(CID, {}).get(quiz_key, {}).get("component") is None


def test_ai_count_items_when_no_rule(tmp_path):
    line = "Labs run every other week until the end of the term (seven sessions in total)."
    s = make_store(tmp_path / "s.json", next_labs=line)
    s.data["settings"]["ai_cli"] = True
    runner = _fake(lambda inp: {"links": []} if "### key:" in inp else {"count": 7, "evidence": line})
    r = gradebook.ai_links(s, agent_cli.AiSession(s, runner=runner, command="claude"))
    assert r["counted"] == 1
    cnt = comp(App(s, tmp_path, cfg={}, scheduler=_Sched()).state(), "Homework assignments")["count"]
    assert (cnt["n"], cnt["basis"], cnt["by"], cnt["evidence"]) == (7, "hint", "ai", line)
    # 관찰한 최소 개수보다 작은 답은 거절
    s.data.pop("grade_counts_ai")
    runner = _fake(lambda inp: {"links": []} if "### key:" in inp else {"count": 2, "evidence": line})
    r = gradebook.ai_links(s, agent_cli.AiSession(s, runner=runner, command="claude"))
    assert r["counted"] == 0 and s.data["grade_counts_ai"][CID]["homework assignments"]["n"] is None


def test_ai_steps_run_after_sync_sharing_session(tmp_path):
    """core._after_sync가 같은 AiSession으로 평가 항목 연결을 부르고 결과를 저장한다(호출 한도 공유)."""
    s = make_store(tmp_path / "s.json", extra_announcements=[QUIZ1])
    s.data["settings"]["ai_cli"] = True
    s.save()
    quiz_key = next(c["key"] for c in gradebook.candidates(s) if c["label"] == "Quiz 1")
    runner = _fake(lambda inp: {"links": [{"key": quiz_key, "component": "Final quiz", "evidence": "Quiz 1"}]}
                   if "### key:" in inp else {"count": None})
    out = core._after_sync(s, True, ai_runner=runner, ai_command="claude")
    assert out["ai_grade_links"]["linked"] == 1
    disk = Store(tmp_path / "s.json")
    assert disk.data["grade_links_ai"][CID][quiz_key]["component"] == "Final quiz"
    assert any(r["task"] == "map_score_item" for r in disk.data["ai_runs"])


def test_demo_scenario_with_downloads_and_demo_student_id(tmp_path):
    """데모(lms_agent demo와 같은 길): 공지 첨부 점수표 2개 + Lab6.pdf 단서 → Homework assignments 9개 중 2개."""
    from lms_agent.canvas import Canvas
    from lms_agent.mock_canvas import DEMO_STUDENT_ID, DEMO_TOKEN, start_mock_canvas
    mock, base = start_mock_canvas()
    try:
        s = Store(tmp_path / "state.json")
        s.data["settings"]["student_id"] = DEMO_STUDENT_ID
        core.sync(Canvas(base, DEMO_TOKEN), s, **core.sync_options(s, demo_home=tmp_path))
        st = App(s, tmp_path, cfg={}, scheduler=_Sched()).state()
    finally:
        mock.shutdown()
    hw = comp(st, "Homework assignments", "101")
    assert [(m["label"], m["max_score"], m["link_by"]) for m in hw["members"]] == [("Lab0", 50.0, "rules"),
                                                                                    ("Lab1", 50.0, "rules")]
    assert (hw["count"]["n"], hw["count"]["basis"], hw["count"]["source_title"]) == (9, "hint", "첨부 'Lab6.pdf'")
    c = course(st, "101")
    assert {"component": "Homework assignments", "weight": 23.33, "count_left": 7, "range": None} in c["remaining"]
    assert c["estimate"] and c["estimate"]["grade"]
    assert st["grades"]["score_files_waiting"] == 0 and st["grades"]["score_files_unmatched"] == []
    mats = {m["filename"]: m for m in st["materials"]["items"]}
    assert mats["Lab0_score.pdf"]["board_title"] == "공지 첨부 파일"
