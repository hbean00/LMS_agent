"""과제 체크리스트(계약 22-6): 제출 요건 문장 찾기(한국어·영어, 마감만 있는 문장은 아님), 항목마다 읽을 글,
state.checklists·POST /api/checklist/<id>(체크·추가·지우기·다시 찾기, 다시 동기화해도 남음).
가짜 데이터만 쓴다(과목·주소·문장 모두 지어낸 것)."""
from datetime import date, datetime, time, timedelta

import pytest

from lms_agent import checklist, core
from lms_agent.api import ApiError, router
from lms_agent.extract import find_dates
from lms_agent.server import App
from lms_agent.store import Store
from lms_agent.timeutil import iso, local_tz, now_local, parse_iso

TZ = local_tz()
T = now_local().date()


def z(days, h=10, mi=0):
    return iso(datetime.combine(T + timedelta(days=days), time(h, mi), tzinfo=TZ))


def md_ko(days):
    d = T + timedelta(days=days)
    return f"{d.month}월 {d.day}일"


def req(sentence, title=None):
    return checklist.requirements(sentence, title)


# ---------------------------------------------------------------- 문장 하나

@pytest.mark.parametrize("sentence,expected", [
    # 한국어
    ("제출: lab.ta@example.test 로 PDF 첨부", [("format", "PDF 파일로 제출"), ("where", "이메일로 제출: lab.ta@example.test")]),
    ("보고서는 PDF로 변환해서 제출하세요.", [("format", "PDF 파일로 제출")]),
    ("소스 코드와 보고서를 zip 파일 하나로 압축해서 이메일로 보내 주세요.",
     [("format", "ZIP(압축) 파일로 제출"), ("include", "코드 포함"), ("include", "보고서 포함"), ("where", "이메일로 제출")]),
    ("파일명은 Report_LabN_이름_학번.pdf 로 해 주세요.",
     [("format", "PDF 파일로 제출"), ("name", "파일 이름: Report_LabN_이름_학번.pdf")]),
    ("메일 제목은 [회로] HW3_이름_학번 으로 해 주세요.", [("name", "메일 제목: [회로] HW3_이름_학번")]),
    ("분량은 A4 5쪽 이내입니다.", [("length", "분량: A4 5쪽 이내")]),
    ("감상문은 2000자 이상으로 써 주세요.", [("length", "분량: 2000자 이상")]),
    ("결과 화면 캡처를 꼭 첨부하세요.", [("include", "스크린샷 포함")]),
    ("Homework 5는 Canvas 과제함으로 제출하세요.", [("where", "LMS(Canvas)에 제출")]),
    ("출력해서 수업 시간에 제출하세요.", [("where", "출력해서 종이로 제출"), ("where", "수업 시간에 직접 제출")]),
    ("과사무실 앞 우편함에 넣어 주세요.", [("where", "과사무실 앞 우편함에 직접 제출")]),
    ("팀별로 하나의 보고서만 제출합니다.", [("team", "팀(조) 과제")]),
    ("3인 1조로 진행합니다.", [("team", "3인 1조(팀) 과제")]),
    ("개인 과제이며, 다른 학생의 코드를 베끼면 0점 처리합니다.",
     [("team", "개인 과제(혼자 작성)"), ("policy", "표절 금지: 개인 과제이며, 다른 학생의 코드를 베끼면 0점 처리합니다.")]),
    ("늦게 제출하면 하루에 10%씩 감점됩니다.", [("policy", "지각 규정: 늦게 제출하면 하루에 10%씩 감점됩니다.")]),
    ("마감 후 제출은 받지 않습니다.", [("policy", "지각 규정: 마감 후 제출은 받지 않습니다.")]),
    ("hwp 파일은 받지 않습니다.", [("format", "한글(HWP) 파일은 안 됨")]),
    # 영어
    ("Submit as a PDF(report) via email : lab.ta@example.test",
     [("format", "PDF 파일로 제출"), ("where", "이메일로 제출: lab.ta@example.test")]),
    ("Please submit your report as a single PDF file via Canvas.", [("format", "PDF 파일로 제출"), ("where", "LMS(Canvas)에 제출")]),
    ("Name your file as HW3_StudentID_Name.pdf.", [("format", "PDF 파일로 제출"), ("name", "파일 이름: HW3_StudentID_Name.pdf")]),
    ("The report should be no more than 5 pages.", [("length", "분량: no more than 5 pages")]),
    ("Write a 2-page summary of the paper.", [("length", "분량: 2-page summary")]),
    ("Include your Verilog code and simulation screenshots in the report.", [("include", "코드 포함"), ("include", "스크린샷 포함")]),
    ("This is an individual assignment; do not share your code.",
     [("team", "개인 과제(혼자 작성)"), ("policy", "표절 금지: This is an individual assignment; do not share your code.")]),
    ("Work in teams of 3.", [("team", "3인 1조(팀) 과제")]),
    ("Late submissions will receive a 10% penalty per day.",
     [("policy", "지각 규정: Late submissions will receive a 10% penalty per day.")]),
    ("Hand in a printed copy at the beginning of class.", [("where", "출력해서 종이로 제출"), ("where", "수업 시간에 직접 제출")]),
    ("Do not submit .hwp files.", [("format", "한글(HWP) 파일은 안 됨")]),
    ("Drop your lab notebook in the TA office mailbox.", [("where", "TA office mailbox에 직접 제출")]),
])
def test_requirement_sentences(sentence, expected):
    assert req(sentence) == expected


@pytest.mark.parametrize("sentence", [
    "LAB4 보고서 마감: 11월 18일(수) 23:59",                          # 마감만
    "Lab 3 is due Nov. 20 at 11:59 PM.",
    "Report due: 23:59 on 11/26",
    "마감은 공지사항을 확인하세요.",
    "LAB4 보고서 마감이 11월 20일 자정으로 연장되었습니다.",
    "11월 20일 휴강입니다. 보강은 추후 공지합니다.",
    "기말고사는 12월 15일 10:30 공학관 101호에서 봅니다.",          # 시험장은 제출처가 아니다
    "강의자료는 PDF로 올렸습니다.",                                 # 강의 자료를 나눠 줌
    "Please find the attached slides.",
    "I have attached the updated grading policy.",
    "The lecture slides are attached as a PDF file.",
    "자세한 내용은 Lab6.pdf를 참고하세요.",                        # 다른 파일 참고
    "질문은 lab.ta@example.test로 메일 주세요.",                    # 문의처 — 제출처 아님
    "If you have questions, contact the TA at lab.ta@example.test.",
    "The quiz covers the lecture materials so far.",
    "Each homework is due one week after it is posted.",
    "지각 3회는 결석 1회로 처리합니다.",                            # 출석 지각 — 제출 규정 아님
    "Closed book. Bring a calculator.",
])
def test_not_requirements(sentence):
    assert req(sentence) == []


def test_requirement_text_has_no_dates_but_quote_is_verbatim():
    s = f"LAB5 결과보고서는 {md_ko(13)} 18:00까지 메일로 제출하고, 늦게 내면 {md_ko(14)}까지만 받습니다."
    out = checklist.extract([{"text": s, "source_title": "LAB5 안내", "url": "https://lms.example.test/a/1"}])
    assert [(r["kind"], r["text"]) for r in out][0] == ("where", "이메일로 제출")
    late = next(r for r in out if r["kind"] == "policy")
    assert late["text"].startswith("지각 규정: ") and "까지" not in late["text"]
    for r in out:
        assert not find_dates(r["text"], T), r["text"]
        assert r["quote"] == s and r["source_title"] == "LAB5 안내" and r["url"] == "https://lms.example.test/a/1"
        assert r["id"].startswith("r-") and r["id"] == checklist.rule_id(r["kind"], r["text"])


def test_strip_dates():
    assert checklist.strip_dates("늦게 제출하면 안 되니 11월 18일(수) 23:59까지 꼭 내세요") == "늦게 제출하면 안 되니 꼭 내세요"
    assert checklist.strip_dates("Late work is accepted until Nov. 20 at 5 PM with a penalty.") == \
        "Late work is accepted with a penalty."
    assert checklist.strip_dates("날짜 없는 문장") == "날짜 없는 문장"


def test_extract_html_dedupe_order_and_other_task():
    html = ("<p>보고서는 조교 이메일(<a href='mailto:lab.ta@example.test'>lab.ta@example.test</a>)로 제출합니다.</p>"
            "<p>메일로 제출해 주세요.</p><p>Lab 1 보고서는 zip 파일로 압축해 제출했습니다.</p>"
            "<p>늦게 제출하면 감점됩니다.</p><p>보고서는 PDF로 제출하세요.</p>")
    out = checklist.extract([{"text": html, "source_title": "Lab 2 보고서", "url": None},
                             {"text": "보고서는 PDF로 제출하세요.", "source_title": "다른 글", "url": None}],
                            title="Lab 2 보고서")
    assert [(r["kind"], r["text"]) for r in out] == [
        ("where", "이메일로 제출: lab.ta@example.test"),   # 주소 없는 '메일로 제출'은 뺀다
        ("format", "PDF 파일로 제출"),                      # 같은 문장은 처음 글 것만
        ("policy", "지각 규정: 늦게 제출하면 감점됩니다.")]  # 'Lab 1 …zip'은 다른 과제 문장
    assert next(r for r in out if r["kind"] == "format")["source_title"] == "Lab 2 보고서"
    assert checklist.other_task("Lab0 and Lab1 scores are uploaded.", "LAB4 보고서")
    assert not checklist.other_task("보고서는 PDF로 제출하세요.", "LAB4 보고서")
    assert not checklist.other_task("LAB 4 report must be a PDF.", "LAB4 보고서")


# ---------------------------------------------------------------- 저장된 글로 항목마다

def ann(aid, cid, posted, title, message):
    return {"id": aid, "title": title, "message": message, "posted_at": posted, "context_code": f"course_{cid}",
            "html_url": f"https://lms.example.test/courses/{cid}/discussion_topics/{aid}"}


def asg(aid, cid, name, due, types, description=""):
    return {"id": aid, "course_id": cid, "name": name, "description": description, "due_at": due,
            "submission_types": types, "created_at": z(-20), "updated_at": z(-19),
            "html_url": f"https://lms.example.test/courses/{cid}/assignments/{aid}",
            "submission": {"workflow_state": "unsubmitted", "submitted_at": None, "score": None}}


LAB2_DESC = ("<p>Lab 2 보고서는 PDF 한 파일로 만들어 조교 이메일(circuit.ta@example.test)로 제출하세요.</p>"
             "<p>파일 이름은 Lab2_이름_학번.pdf 형식으로 해 주세요.</p>"
             "<p>회로 사진과 측정 결과 표를 보고서에 포함해야 합니다.</p>"
             "<p>Lab 1 보고서는 zip 파일로 압축해 제출했습니다.</p>")


def make_cache():
    return {
        "fetched_at": z(0),
        "courses": [{"id": "301", "name": "262R (서울-학부)회로설계실습-01분반", "course_code": "EE301", "syllabus_body": None},
                    {"id": "302", "name": "Technical Writing", "course_code": "ENG302", "syllabus_body": None}],
        "assignments": [
            asg(3011, "301", "Lab 2 보고서", None, ["none"], LAB2_DESC),
            asg(3012, "301", "Lab 3 보고서", z(9, 23, 59), ["online_upload"], "<p>Canvas에 업로드하세요.</p>"),
            asg(3013, "301", "Lab 0 보고서", None, ["none"], "<p>PDF로 제출하세요.</p>"),     # 마감 없음 → 안 보임
            asg(3021, "302", "Essay 1", z(5, 23, 59), ["online_upload"],
                "<p>The essay should be no more than 1,500 words.</p>"),
        ],
        "announcements": [
            ann(1, "301", z(-3), "Lab 2 보고서 안내",
                f"<p>Lab 2 보고서 마감: {md_ko(6)} 23:59</p><p>늦게 제출하면 하루에 10%씩 감점됩니다.</p>"),
            ann(2, "301", z(-2), "휴강 안내", f"<p>{md_ko(3)} 휴강입니다.</p>"),
            ann(3, "302", z(-1), "Essay 1 guide",
                "<p>Essay 1 is an individual assignment.</p><p>Late submissions will receive a 10% penalty per day.</p>"),
            ann(4, "302", z(-1), "Reading list", f"<p>Read chapter 3 before {md_ko(4)}.</p>"),
        ],
        "conversations": [], "boards": [], "pdf_texts": {}, "errors": [],
    }


def make_store(path, cache=None):
    s = Store(path)
    s.data["cache"] = cache or make_cache()
    core.rebuild(s)
    s.save()
    return s


def make_app(tmp_path, cache=None):
    store = make_store(tmp_path / "state.json", cache)
    return App(store, tmp_path, cfg={"canvas_url": "https://lms.example.test"})


def post(app, path, body):
    """서버처럼 경로를 찾아 부른다: (상태 코드, 결과)."""
    for route, groups in router.match("POST", path):
        try:
            res = route.handler(app, body, **groups)
        except ApiError as e:
            return e.status, {"error": str(e)}
        except ValueError as e:
            return 400, {"error": str(e)}
        if res is not None:
            return 200, res
    return 404, None


def by_title(items, title):
    rows = [v for v in items if v["title"] == title]
    assert rows, [v["title"] for v in items]
    return rows[0]


def texts(cl):
    return [(r["kind"], r["text"]) for r in cl["items"]]


def test_sources_per_item(tmp_path):
    s = make_store(tmp_path / "s.json")
    view = s.view()
    lab2 = by_title(view, "Lab 2 보고서")
    assert lab2["origin"] == "found" and lab2["linked_to"] == "canvas-301-3011" and lab2["status"] == "review"
    srcs = checklist.all_sources(s)
    keys = [t["key"] for t in checklist.texts_for(lab2, srcs, view)]
    assert keys == ["assignment:3011", "announcement:1"]       # 연결된 Canvas 과제 설명 → 근거 글(제목도 맞음)
    essay = by_title(view, "Essay 1")
    keys = [t["key"] for t in checklist.texts_for(essay, srcs, view)]
    assert keys == ["assignment:3021", "announcement:3"]       # 과제 설명 → 같은 과목 공지 중 제목이 맞는 것


def test_state_checklists(tmp_path):
    app = make_app(tmp_path)
    st = app.state()
    cls = st["checklists"]
    lab2 = by_title(st["items"], "Lab 2 보고서")
    assert texts(cls[lab2["id"]]) == [
        ("where", "이메일로 제출: circuit.ta@example.test"), ("format", "PDF 파일로 제출"),
        ("name", "파일 이름: Lab2_이름_학번.pdf"), ("include", "사진 포함"), ("include", "결과 포함"),
        ("policy", "지각 규정: 늦게 제출하면 하루에 10%씩 감점됩니다.")]     # 'Lab 1 …zip'은 빠짐
    row = cls[lab2["id"]]["items"][0]
    assert set(row) >= {"id", "text", "quote", "source_title", "url", "done", "by"}
    assert row["by"] == "rules" and row["done"] is False and row["source_title"] == "Lab 2 보고서"
    assert row["quote"] == "Lab 2 보고서는 PDF 한 파일로 만들어 조교 이메일(circuit.ta@example.test)로 제출하세요."
    assert row["url"] == "https://lms.example.test/courses/301/assignments/3011"
    assert parse_iso(cls[lab2["id"]]["updated_at"]) == parse_iso(app.store.data["last_sync"]) or \
        cls[lab2["id"]]["updated_at"] is None
    essay = by_title(st["items"], "Essay 1")
    assert texts(cls[essay["id"]]) == [("length", "분량: no more than 1,500 words"), ("team", "개인 과제(혼자 작성)"),
                                       ("policy", "지각 규정: Late submissions will receive a 10% penalty per day.")]
    lab3 = by_title(st["items"], "Lab 3 보고서")
    assert texts(cls[lab3["id"]]) == [("where", "LMS(Canvas)에 제출")]
    # 보이는 항목(review·scheduled)만, 요건이 있을 때만
    lab0 = by_title(st["items"], "Lab 0 보고서")
    assert lab0["status"] == "no_deadline" and lab0["id"] not in cls
    assert set(cls) == {lab2["id"], essay["id"], lab3["id"]}
    for cl in cls.values():
        for r in cl["items"]:
            assert not find_dates(r["text"], T), r["text"]


def test_rejected_item_has_no_checklist(tmp_path):
    app = make_app(tmp_path)
    lab2 = by_title(app.store.view(), "Lab 2 보고서")
    app.store.update_user(lab2["id"], {"decision": "rejected"})
    app.store.save()
    assert lab2["id"] not in app.state()["checklists"]


def test_toggle_add_remove_refresh_and_validation(tmp_path):
    app = make_app(tmp_path)
    st = app.state()
    lab2 = by_title(st["items"], "Lab 2 보고서")
    iid = lab2["id"]
    first, second = st["checklists"][iid]["items"][:2]

    code, st = post(app, f"/api/checklist/{iid}", {"op": "toggle", "id": first["id"]})
    assert code == 200 and st["checklists"][iid]["items"][0]["done"] is True
    code, st = post(app, f"/api/checklist/{iid}", {"op": "toggle", "id": first["id"], "done": True})   # 정해 주기(되돌리기용)
    assert st["checklists"][iid]["items"][0]["done"] is True
    code, st = post(app, f"/api/checklist/{iid}", {"op": "add", "text": "  표지에   실험 조 번호 적기 "})
    mine = st["checklists"][iid]["items"][-1]
    assert code == 200 and mine["by"] == "user" and mine["text"] == "표지에 실험 조 번호 적기" and mine["done"] is False
    assert mine["id"].startswith("u-") and mine["quote"] is None
    code, st = post(app, f"/api/checklist/{iid}", {"op": "toggle", "id": mine["id"]})
    assert st["checklists"][iid]["items"][-1]["done"] is True
    code, st = post(app, f"/api/checklist/{iid}", {"op": "remove", "id": second["id"]})
    assert second["id"] not in [r["id"] for r in st["checklists"][iid]["items"]]
    assert parse_iso(st["checklists"][iid]["updated_at"]) is not None

    # 저장돼 있다(다른 프로세스가 읽어도)
    disk = Store(app.store.path)
    box = disk.data["checklists"][iid]
    assert box["done"][first["id"]] is True and second["id"] in box["removed"] and box["added"][0]["text"].startswith("표지")

    # 다시 찾기: 지운 규칙 칸이 돌아오고 체크·추가는 남는다
    code, st = post(app, f"/api/checklist/{iid}", {"op": "refresh"})
    rows = {r["id"]: r for r in st["checklists"][iid]["items"]}
    assert second["id"] in rows and rows[first["id"]]["done"] is True and rows[mine["id"]]["done"] is True
    code, st = post(app, f"/api/checklist/{iid}", {"op": "remove", "id": mine["id"]})
    assert mine["id"] not in [r["id"] for r in st["checklists"][iid]["items"]]

    # 검사
    assert post(app, "/api/checklist/f-000000000000", {"op": "toggle", "id": first["id"]})[0] == 404
    assert post(app, f"/api/checklist/{iid}", {"op": "toggle", "id": "r-0000000000"})[0] == 404
    assert post(app, f"/api/checklist/{iid}", {"op": "jump"})[0] == 400
    assert post(app, f"/api/checklist/{iid}", {"op": "toggle", "id": "../x"})[0] == 400
    assert post(app, f"/api/checklist/{iid}", {"op": "toggle"})[0] == 400
    assert post(app, f"/api/checklist/{iid}", {"op": "toggle", "id": first["id"], "done": "yes"})[0] == 400
    assert post(app, f"/api/checklist/{iid}", {"op": "add", "text": "   "})[0] == 400
    assert post(app, f"/api/checklist/{iid}", {"op": "add", "text": 3})[0] == 400
    assert post(app, f"/api/checklist/{iid}", {"op": "add", "text": "가" * 201})[0] == 400
    for i in range(checklist.USER_MAX):
        assert post(app, f"/api/checklist/{iid}", {"op": "add", "text": f"칸 {i}"})[0] == 200
    assert post(app, f"/api/checklist/{iid}", {"op": "add", "text": "하나 더"})[0] == 400


def test_user_edits_survive_resync(tmp_path):
    app = make_app(tmp_path)
    st = app.state()
    lab2 = by_title(st["items"], "Lab 2 보고서")
    iid = lab2["id"]
    rows = {r["text"]: r for r in st["checklists"][iid]["items"]}
    post(app, f"/api/checklist/{iid}", {"op": "toggle", "id": rows["이메일로 제출: circuit.ta@example.test"]["id"]})
    post(app, f"/api/checklist/{iid}", {"op": "toggle", "id": rows["PDF 파일로 제출"]["id"]})
    post(app, f"/api/checklist/{iid}", {"op": "remove", "id": rows["사진 포함"]["id"]})
    post(app, f"/api/checklist/{iid}", {"op": "add", "text": "예비보고서와 비교"})

    # 다시 동기화: 공지가 하나 더 오고, 과제 설명의 형식이 PDF → zip으로 바뀜
    cache = app.store.data["cache"]
    cache["announcements"].append(ann(5, "301", z(0), "Lab 2 보고서 보충",
                                      "<p>Lab 2 보고서는 2인 1조로 작성합니다.</p>"))
    a = next(x for x in cache["assignments"] if x["id"] == 3011)
    a["description"] = a["description"].replace("PDF 한 파일로 만들어", "zip 파일로 압축해").replace(".pdf", ".zip")
    core.rebuild(app.store)
    app.store.save()

    st = app.state()
    lab2 = by_title(st["items"], "Lab 2 보고서")
    assert lab2["id"] == iid                                   # 항목 id는 그대로
    got = {r["text"]: r for r in st["checklists"][iid]["items"]}
    assert got["이메일로 제출: circuit.ta@example.test"]["done"] is True    # 체크가 남음
    assert "PDF 파일로 제출" not in got and got["ZIP(압축) 파일로 제출"]["done"] is False   # 바뀐 요건은 체크 전
    assert got["파일 이름: Lab2_이름_학번.zip"]["done"] is False
    assert "사진 포함" not in got                             # 지운 칸도 그대로 지워져 있음
    assert got["예비보고서와 비교"]["by"] == "user"
    assert got["2인 1조(팀) 과제"]["source_title"] == "Lab 2 보고서 보충"


def test_rule_ids_are_stable(tmp_path):
    s1 = make_store(tmp_path / "a.json")
    s2 = make_store(tmp_path / "b.json")
    r1 = checklist.compute(s1, s1.view())
    r2 = checklist.compute(s2, s2.view())
    assert {k: [r["id"] for r in v] for k, v in r1.items()} == {k: [r["id"] for r in v] for k, v in r2.items()}


def test_hidden_course_has_no_checklist(tmp_path):
    app = make_app(tmp_path)
    app.store.data["course_prefs"] = {"301": {"mode": "hidden"}}
    app.store.save()
    st = app.state()
    assert all(v["course_id"] != "301" for v in st["items"] if v["id"] in st["checklists"])
    assert st["checklists"]                                    # 302 Essay 1은 남는다


def test_demo_has_checklists(tmp_path):
    """데모 LAB4 과제 설명에 덧붙인 날짜 없는 요건 문장(mock_canvas)이 체크리스트로 보인다."""
    from lms_agent.canvas import Canvas
    from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
    mock, base = start_mock_canvas()
    try:
        store = Store(tmp_path / "state.json")
        core.sync(Canvas(base, DEMO_TOKEN), store)
        app = App(store, tmp_path, cfg={"canvas_url": base, "token": DEMO_TOKEN})
        st = app.state()
    finally:
        mock.shutdown()
    lab4 = [v for v in st["items"] if v["title"] == "LAB4 보고서" and v["origin"] == "found"]
    assert len(lab4) == 2
    for it in lab4:
        got = texts(st["checklists"][it["id"]])
        assert ("where", "이메일로 제출: asic.ta@example.ac.kr") in got
        assert {("format", "PDF 파일로 제출"), ("name", "파일 이름: Report_Lab4_이름_학번.pdf"),
                ("length", "분량: A4 10쪽 이내"), ("include", "코드 포함"), ("team", "개인 과제(혼자 작성)")} <= set(got)
    hw5 = next(v for v in st["items"] if v["title"] == "Homework 5" and v["origin"] == "canvas")
    assert texts(st["checklists"][hw5["id"]]) == [("where", "LMS(Canvas)에 제출")]   # 겹친 공지(HW5 안내)에서
