"""문의 메일 초안(계약 22-7): 받는 사람은 그 과목 글의 조교·교수·문의 주소만(인용과 함께, 항목의 제출 주소 먼저),
못 찾으면 비워 둠, [이름]·[학번] 자리표시, 목적별 틀, 채팅 요청 알아듣기(handle), POST /api/mail/draft(저장 안 함).
가짜 데이터만 쓴다(과목·주소·이름 모두 지어낸 것). 메일은 보내지 않는다."""
import re
from datetime import datetime, time, timedelta

import pytest

from lms_agent import core, maildraft
from lms_agent.api import ApiError, router
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


def ann(aid, cid, posted, title, message):
    return {"id": aid, "title": title, "message": message, "posted_at": posted, "context_code": f"course_{cid}",
            "html_url": f"https://lms.example.test/courses/{cid}/discussion_topics/{aid}"}


def make_cache():
    return {
        "fetched_at": z(0),
        "courses": [
            {"id": "401", "name": "262R (서울-학부)회로이론(영강)(Circuit Theory(English))-00분반", "course_code": "EE401",
             "syllabus_body": "<p>담당 교수: 김교수 (prof.circuit@example.test)</p>"
                              "<p>조교 연락처</p><p>circuit.help@example.test</p>"},
            {"id": "402", "name": "열역학", "course_code": "ME402", "syllabus_body": None},
            {"id": "403", "name": "Modern Poetry", "course_code": "LIT403",
             "syllabus_body": "<p>If you have questions, contact poetry.office@example.test.</p>"},
        ],
        "assignments": [],
        "announcements": [
            ann(1, "401", z(-3), "LAB4 보고서 안내",
                f"<p>LAB4 보고서 마감: {md_ko(6)} 23:59</p><p>제출: lab.box@example.test 로 PDF 첨부</p>"),
            ann(2, "402", z(-2), "과제 3 안내",
                f"<p>과제 3은 {md_ko(5)} 18:00까지 thermo.box@example.test로 보내 주세요.</p>"),
            ann(3, "402", z(-1), "공지", "<p>다음 주 수업은 정상 진행합니다. noreply@example.test</p>"),
        ],
        "conversations": [], "boards": [], "pdf_texts": {}, "errors": [],
    }


def make_store(path, cache=None):
    s = Store(path)
    s.data["cache"] = cache or make_cache()
    core.rebuild(s)
    s.save()
    return s


def item(s, course_id):
    """그 과목의 찾은 항목(가짜 데이터는 과목마다 하나)."""
    (v,) = [v for v in s.view() if v["course_id"] == course_id and v["origin"] == "found"]
    return v


def texts_of_course(s, cid):
    return " ".join(x["plain"] for x in maildraft._sources(s) if str(x.get("course_id")) == cid)


# ---------------------------------------------------------------- 받는 사람

def test_recipients_found_with_quotes_and_item_address_first(tmp_path):
    s = make_store(tmp_path / "s.json")
    lab4 = item(s, "401")
    assert lab4["email_to"] == "lab.box@example.test" and lab4["title"] == "LAB 4"
    d = maildraft.draft(s, "401", "extension", lab4["id"])
    to = d["to"]
    assert [r["email"] for r in to] == ["lab.box@example.test", "circuit.help@example.test", "prof.circuit@example.test"]
    assert [r["role"] for r in to] == ["제출처", "조교", "교수"]          # 연장 문의는 조교가 교수보다 먼저
    assert to[0]["quote"] == "제출: lab.box@example.test 로 PDF 첨부" and to[0]["source_title"] == "LAB4 보고서 안내"
    assert to[1]["quote"] == "circuit.help@example.test" and to[1]["source_title"] == "강의계획서"   # 앞 줄 '조교 연락처'
    assert to[2]["quote"] == "담당 교수: 김교수 (prof.circuit@example.test)"
    # 지어내지 않는다: 모든 주소가 그 과목 글에 그대로 있다
    course_text = texts_of_course(s, "401")
    assert all(r["email"] in course_text for r in to)
    assert any("제출 주소" in n for n in d["notes"])
    # 결석 문의는 교수가 먼저(제출 주소는 늘 맨 앞)
    d = maildraft.draft(s, "401", "absence")
    assert [r["role"] for r in d["to"]] == ["교수", "조교"] and d["body"].startswith("교수님, 안녕하세요.")


def test_contact_words_and_no_role_addresses(tmp_path):
    s = make_store(tmp_path / "s.json")
    d = maildraft.draft(s, "403", "question")
    assert [(r["email"], r["role"]) for r in d["to"]] == [("poetry.office@example.test", "문의처")]
    # 역할·문의 말 없이 제출 주소만 적힌 과목: 항목이 없으면 받는 사람 없음, noreply는 늘 뺌
    d = maildraft.draft(s, "402", "question")
    assert d["to"] == [] and any("주소를 못 찾았어요" in n for n in d["notes"])
    assert "@" not in d["body"] and "@" not in d["subject"]
    # 항목을 주면 그 제출 주소가 받는 사람(인용과 함께)
    hw = item(s, "402")
    d = maildraft.draft(s, "402", "submission", hw["id"])
    assert [r["email"] for r in d["to"]] == ["thermo.box@example.test"] and d["to"][0]["quote"].startswith("과제 3은")


def test_find_contacts_generic():
    rows = maildraft.find_contacts([
        {"plain": "TA: Jane Doe (jane.ta@example.test)\nOffice hours: Tue 3-5pm\n보고서는 box@example.test로 내세요.",
         "title": "Syllabus"},
        {"plain": "Questions? Email help.desk@example.test\nsys.prof7@example.test", "title": "Notice"},
    ])
    assert [(r["email"], r["role"]) for r in rows] == [("jane.ta@example.test", "조교"), ("help.desk@example.test", "문의처"),
                                                       ("sys.prof7@example.test", "교수")]   # 마지막은 주소 앞부분의 prof
    assert rows[0]["source_title"] == "Syllabus"


# ---------------------------------------------------------------- 본문

def test_draft_body_placeholders_names_and_due(tmp_path):
    s = make_store(tmp_path / "s.json")
    lab4 = item(s, "401")
    d = maildraft.draft(s, "401", "extension", lab4["id"])
    assert set(d) >= {"course_id", "purpose", "to", "subject", "body", "notes"}
    assert d["course_id"] == "401" and d["purpose"] == "extension" and d["item_id"] == lab4["id"]
    assert d["subject"] == "[회로이론(영강)] LAB 4 기한 연장 문의 - [학번] [이름]"
    assert "[이름]" in d["body"] and "[학번]" in d["body"] and "[희망 날짜]" in d["body"]
    assert maildraft.due_text(lab4["due"]) in d["body"]
    assert any("보내지 않아요" in n for n in d["notes"])
    # 사용자 이름이 있으면 이름만 채운다(학번은 늘 자리표시)
    d = maildraft.draft(s, "401", "extension", lab4["id"], note="독감으로 입원해 진단서를 첨부합니다.", names=["김가짜"])
    assert "[이름]" not in d["body"] and "김가짜" in d["body"] and "[학번]" in d["body"] and d["subject"].endswith("[학번] 김가짜")
    assert "독감으로 입원해 진단서를 첨부합니다." in d["body"] and "[사정을 적어 주세요]" not in d["body"]
    # 목적별 제목
    for purpose, label in maildraft.PURPOSE_LABEL.items():
        assert label in maildraft.draft(s, "401", purpose, lab4["id"])["subject"]
    assert "이메일(lab.box@example.test)로" in maildraft.draft(s, "401", "submission", lab4["id"])["body"]


def test_due_text_is_local():
    dt = datetime(2026, 11, 18, 23, 59, tzinfo=local_tz())
    assert maildraft.due_text(iso(dt)) == "11월 18일(수) 23:59"
    assert maildraft.due_text(None) is None


def test_draft_validation(tmp_path):
    s = make_store(tmp_path / "s.json")
    with pytest.raises(ValueError):
        maildraft.draft(s, "401", "complain")
    with pytest.raises(ValueError):
        maildraft.draft(s, "401", "question", note="가" * (maildraft.NOTE_MAX + 1))
    with pytest.raises(LookupError):
        maildraft.draft(s, "999", "question")
    with pytest.raises(LookupError):
        maildraft.draft(s, "401", "question", "f-000000000000")
    with pytest.raises(ValueError):                       # 항목과 과목이 다름
        maildraft.draft(s, "402", "question", item(s, "401")["id"])


# ---------------------------------------------------------------- 채팅(handle)

@pytest.mark.parametrize("message,purpose", [
    ("LAB4 연장 문의 메일 써줘", "extension"),
    ("교수님께 결석 메일 초안", "absence"),
    ("성적 문의 이메일 작성해줘", "grade"),
    ("LAB4 제출 확인 메일 써 줘", "submission"),
    ("조교님께 보낼 질문 메일 초안 만들어줘", "question"),
    ("Can you draft an email asking for an extension?", "extension"),
])
def test_handle_recognizes_mail_requests(tmp_path, message, purpose):
    s = make_store(tmp_path / "s.json")
    r = maildraft.handle(s, message)
    assert r is not None and r["draft"]["purpose"] == purpose and "보내지는 않았어요" in r["answer"]


@pytest.mark.parametrize("message", [
    "기말고사 날짜가 안 보여요",
    "LAB4 마감이 메일로 왔는데 안 보여요",
    "조교 이메일 주소 알려줘",
    "벌써 메일로 공지가 왔는데 마감이 안 보여요",
    "Signals 기말 날짜가 안 보여요 (제 메일 me@example.test)",       # 메일은 곁들인 말
    "메일로 문의할 때 쓰는 주소가 안 보여요",
    "신호와시스템 성적이 안 들어와요",
    "열역학 성적이 안 들어오는 문제가 있습니다",
    "기말 범위 어디까지야?",
    "제출 형식이 안 보여요",
    "이메일로 제출하는 과제가 있나요?",
    "기말에서 몇 점 받아야 A?",
    "컴퓨터 켤 때 자동 실행해줘",
    "",
])
def test_handle_none_for_other_messages(tmp_path, message):
    s = make_store(tmp_path / "s.json")
    assert maildraft.handle(s, message) is None


def test_handle_finds_course_and_item(tmp_path):
    s = make_store(tmp_path / "s.json")
    r = maildraft.handle(s, "LAB4 연장 문의 메일 써줘", names=["김가짜"])
    d = r["draft"]
    assert d["course_id"] == "401" and d["item_id"] == item(s, "401")["id"]
    assert d["to"][0]["email"] == "lab.box@example.test" and "lab.box@example.test" in r["answer"]
    assert "김가짜" in d["body"]
    # 과목 이름으로
    r = maildraft.handle(s, "열역학 과제 3 제출 확인 메일 써줘")
    assert r["draft"]["course_id"] == "402" and r["draft"]["item_id"] == item(s, "402")["id"]
    r = maildraft.handle(s, "Modern Poetry 질문 메일 초안")
    assert r["draft"]["course_id"] == "403" and r["draft"]["item_id"] is None
    # 화면에서 고른 과목(course_id)
    r = maildraft.handle(s, "교수님께 결석 메일 초안", course_id="401")
    assert r["draft"]["course_id"] == "401" and r["draft"]["to"][0]["role"] == "교수"
    # 과목을 모르면 초안은 [과목명]으로, 받는 사람은 비우고 과목을 되묻는다
    r = maildraft.handle(s, "교수님께 결석 메일 초안")
    assert r["draft"]["course_id"] is None and r["draft"]["to"] == []
    assert "[과목명]" in r["draft"]["body"] and r["draft"]["subject"].startswith("[과목명] ")
    assert "회로이론(영강)" in r["answer"] and "열역학" in r["answer"]


def test_handle_skips_hidden_course(tmp_path):
    s = make_store(tmp_path / "s.json")
    s.data["course_prefs"] = {"401": {"mode": "hidden"}}
    r = maildraft.handle(s, "LAB4 연장 문의 메일 써줘")
    assert r["draft"]["course_id"] != "401" and "회로이론" not in r["answer"]


# ---------------------------------------------------------------- 경로

def post(app, path, body):
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


def test_route_mail_draft_does_not_write(tmp_path):
    s = make_store(tmp_path / "state.json")
    app = App(s, tmp_path, cfg={"canvas_url": "https://lms.example.test", "user_name": "김가짜"})
    before = s.path.stat().st_mtime_ns
    lab4 = item(s, "401")
    code, out = post(app, "/api/mail/draft", {"course_id": "401", "purpose": "grade", "item_id": lab4["id"],
                                              "note": "LAB4 채점 기준이 궁금합니다."})
    assert code == 200 and set(out) == {"draft"}
    d = out["draft"]
    assert d["purpose"] == "grade" and "김가짜" in d["body"] and "LAB4 채점 기준이 궁금합니다." in d["body"]
    assert s.path.stat().st_mtime_ns == before and "mail" not in s.data
    code, out = post(app, "/api/mail/draft", {"course_id": 402, "purpose": "question"})   # 숫자 id도 받음
    assert code == 200 and out["draft"]["course_id"] == "402" and out["draft"]["to"] == []
    assert post(app, "/api/mail/draft", {"course_id": "401", "purpose": "send"})[0] == 400
    assert post(app, "/api/mail/draft", {"purpose": "question"})[0] == 400
    assert post(app, "/api/mail/draft", {"course_id": "401", "purpose": "question", "note": 5})[0] == 400
    assert post(app, "/api/mail/draft", {"course_id": ["401"], "purpose": "question"})[0] == 400
    assert post(app, "/api/mail/draft", {"course_id": "999", "purpose": "question"})[0] == 404
    assert post(app, "/api/mail/draft", {"course_id": "401", "purpose": "question", "item_id": "f-0"})[0] == 404


def test_demo_lab4_extension_draft(tmp_path):
    """데모: 'LAB4 연장 문의 메일 써줘' → ASIC 조교 주소(데모 공지·과제 설명에 있는 가짜 주소), 연장된 마감."""
    from lms_agent.canvas import Canvas
    from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
    mock, base = start_mock_canvas()
    try:
        s = Store(tmp_path / "state.json")
        core.sync(Canvas(base, DEMO_TOKEN), s)
    finally:
        mock.shutdown()
    r = maildraft.handle(s, "LAB4 연장 문의 메일 써줘")
    d = r["draft"]
    assert d["course_id"] == "101" and [x["email"] for x in d["to"]] == ["asic.ta@example.ac.kr"]
    it = next(v for v in s.view() if v["id"] == d["item_id"])
    latest = max((v for v in s.view() if v["title"] == "LAB4 보고서" and v["origin"] == "found"),
                 key=lambda v: parse_iso(v["due"]))
    assert parse_iso(it["due"]) == parse_iso(latest["due"])          # 가장 최근 글(연장)의 마감
    assert re.search(r"\d+월 \d+일\(.\) \d\d:\d\d", d["body"])
