"""성적 능력(계약 28-2): 학점 비율·점수 기준 바꾸기 규칙, 화면과 같은 전체 본문(build)과 전→후(preview),
실제 경로 처리기(actions._call)로 저장한 설정 = 성적 화면이 저장했을 설정, 추정이 새 비율·비중을 쓰는지, 검사(한국어 오류).
데모 과목: 101 ASIC(강의 글에서 자동으로 찾은 비중 6개 + 학점 비율 A+·A0 40%), 102 디지털신호처리(설정 없음),
103 반도체공학, 105 연구실 안전 교육. 데모 항목 id는 시간대마다 달라 제목으로 찾는다."""
import copy
import types

import pytest

from lms_agent import actions, capabilities as caps, capabilities_grades as cg, core, gradebook
from lms_agent import grades as g
from lms_agent.api import router
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App
from lms_agent.store import Store


class _Sched:
    def status(self):
        return {"enabled": False, "interval_hours": None}

    def command_text(self):
        return ""


@pytest.fixture()
def demo(tmp_path):
    srv, base = start_mock_canvas()
    store = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), store)
    app = App(store, tmp_path, cfg={}, scheduler=_Sched())
    cg.register_all()
    yield types.SimpleNamespace(app=app, store=store, base=base)
    srv.shutdown()


def cap(cid):
    cg.register_all()
    return caps.REGISTRY[cid]


def build(app, cid, **params):
    return cap(cid).build(app, params)


def preview(app, cid, **params):
    return [(x["label"], x["before"], x["after"]) for x in cap(cid).preview(app, params)]


def run(app, cid, **params):
    """화면이 부르는 것과 같은 경로 처리기로 실행."""
    path, body = build(app, cid, **params)
    return actions._call(app, path, body)


def screen_saved(store, cid, body):
    """성적 화면이 같은 본문을 [저장]했을 때의 설정(저장소 사본에 save_course)."""
    fake = types.SimpleNamespace(data={"grades": copy.deepcopy(store.data.get("grades") or {})})
    gradebook.save_course(fake, cid, copy.deepcopy(body))
    return fake.data["grades"][cid]


def course(state, cid):
    return next(c for c in state["grades"]["courses"] if str(c["course_id"]) == cid)


def err(app, cid, **params) -> str:
    with pytest.raises(ValueError) as e:
        build(app, cid, **params)
    with pytest.raises(ValueError):
        cap(cid).preview(app, params)
    return str(e.value)


# ---------------------------------------------------------------- 등록·모양

def _shape(pattern: str) -> str:
    import re
    return re.sub(r"\(\?P<(\w+)>[^)]*\)", r"{\1}", pattern)


def test_register_all_is_idempotent_and_shapes():
    cg.register_all()
    first = {k: caps.REGISTRY[k] for k in cg.IDS}
    cg.register_all()
    assert all(k in caps.REGISTRY for k in cg.IDS) and len({k for k in caps.REGISTRY if k in cg.IDS}) == len(cg.IDS)
    posts = {_shape(r.pattern.pattern) for r in router.routes if r.method == "POST"}
    for k in cg.IDS:
        c = caps.REGISTRY[k]
        assert c.id == first[k].id and c.area in caps.AREAS and c.title
        assert c.route[0] == "POST" and c.route[1] in posts, c.route
        for name, spec in c.params.items():
            assert spec["type"] in ("str", "int", "float", "bool", "enum", "list", "dict") and spec["desc"]
            assert spec["resolve"] in ("course", "item", "component", None) and isinstance(spec["required"], bool)
            if spec["type"] == "enum":
                assert spec["enum"]
        if k == "grades.settings":
            assert c.tier == "refuse" and c.pane == "grades" and "개인정보" in c.refuse and not c.undoable
        else:
            assert c.tier == "direct" and c.undoable and not c.external and callable(c.build) and callable(c.preview)
    assert caps.REGISTRY["items.set_component"].area == "items"
    assert caps.REGISTRY["grades.set_score"].params["component"]["resolve"] == "component"
    assert caps.REGISTRY["grades.set_dist"].params["course"]["resolve"] == "course"
    assert caps.REGISTRY["items.set_component"].params["item"]["resolve"] == "item"
    # null로 지우는 값은 하네스 값 검사가 '일부러 준 null'로 받게
    assert caps.REGISTRY["grades.set_class_size"].params["size"]["nullable"] is True
    assert caps.REGISTRY["grades.set_count"].params["n"]["nullable"] is True


def test_student_id_is_refused(demo):
    c = cap("grades.settings")
    with pytest.raises(ValueError, match="개인정보"):
        c.build(demo.app, {"student_id": "2026123456"})
    assert c.preview(demo.app, {}) == []
    assert "student_id" not in demo.store.data["settings"]


# ---------------------------------------------------------------- 학점 비율 바꾸기 규칙(순수 함수)

ASIC_NOW = {"A+": 20.0, "A0": 20.0, "B+": 22.5, "B0": 22.5, "C+": 3.0, "C0": 3.0, "D+": 3.0, "D0": 3.0, "F": 3.0}


def test_relative_letters_and_bands():
    d, groups, _ = cg.relative_dist({"A": 40, "B": 40, "C": 20}, ASIC_NOW)
    assert d == {"A+": 20.0, "A0": 20.0, "B+": 20.0, "B0": 20.0, "C+": 10.0, "C0": 10.0, "D+": 0.0, "D0": 0.0, "F": 0.0}
    assert [x["label"] for x in groups] == ["A", "B", "C"]
    # 기본 비율 모양(C+ 15 : C0 20)으로 묶음 안을 나눈다 — 합은 정확히 100
    d, _, _ = cg.relative_dist({"A학점": 40, "B grade": 40, "C": 20}, None)
    assert (d["C+"], d["C0"]) == (8.57, 11.43) and round(sum(d.values()), 6) == 100
    assert not g.validate_distribution(d)
    # A+가 따로 있으면 맨 글자 A는 A0, A-도 A0
    d, _, _ = cg.relative_dist({"A+": 10, "A": 30, "B+~B0": 40, "C+~F": 20}, ASIC_NOW)
    assert (d["A+"], d["A0"], d["B+"], d["B0"]) == (10.0, 30.0, 20.0, 20.0)
    assert round(sum(d[x] for x in ("C+", "C0", "D+", "D0", "F")), 2) == 20
    d, _, _ = cg.relative_dist({"A+": 15, "A-": 25, "B+, B0": 60}, None)
    assert (d["A+"], d["A0"], d["B+"] + d["B0"], d["F"]) == (15.0, 25.0, 60.0, 0.0)


def test_relative_partial_keeps_current_shape_for_the_rest():
    """말하지 않은 학점은 남은 %를 지금 비율 모양대로('A 50%로만' → B·C 이하는 지금 비율대로 줄어듦)."""
    d, groups, mentioned = cg.relative_dist({"A": 50}, ASIC_NOW)
    assert d == {"A+": 25.0, "A0": 25.0, "B+": 18.75, "B0": 18.75, "C+": 2.5, "C0": 2.5, "D+": 2.5, "D0": 2.5,
                 "F": 2.5}
    assert mentioned == {0, 1} and groups == [{"label": "A", "idx": [0, 1]}]
    d, _, _ = cg.relative_dist({"A": 40, "B": 40, "나머지": None}, None)   # 나머지 = 100 − 합, 기본 모양
    assert (d["C+"], d["C0"], d["D+"], d["F"]) == (7.5, 10.0, 0.0, 2.5)


def test_relative_cumulative_and_tail():
    d, groups, _ = cg.relative_dist({"A": 30, "B": 70}, ASIC_NOW, cumulative=True)
    assert d["A+"] + d["A0"] == 30 and d["B+"] + d["B0"] == 40 and round(sum(d.values()), 6) == 100
    assert groups[1] == {"label": "B", "idx": [0, 1, 2, 3]}
    d, _, _ = cg.relative_dist({"A+": 10, "A": 30, "B 이상": 80}, ASIC_NOW)
    assert (d["A+"], d["A0"], d["B+"] + d["B0"]) == (10.0, 30.0, 40.0)
    d, _, _ = cg.relative_dist({"A": 30, "B": 40, "C 이하": 30}, ASIC_NOW)
    assert round(sum(d[x] for x in ("C+", "C0", "D+", "D0", "F")), 2) == 30 and d["C+"] == 6.0
    d, _, _ = cg.relative_dist({"A or above": 35, "B and up": 80}, None)
    assert d["A+"] + d["A0"] == 35 and d["B+"] + d["B0"] == 45


@pytest.mark.parametrize("spec,kw,msg", [
    ({"A": 60, "B": 50}, {}, "100%를 넘어요"),
    ({"A+": 10, "A0": 10, "B+": 10, "B0": 10, "C+": 10, "C0": 10, "D+": 10, "D0": 10, "F": 10}, {}, "합이 100%가 되게"),
    ({"A": 40, "A+~B0": 30}, {}, "겹쳐요"),
    ({"A": 50, "B": 40}, {"cumulative": True}, "늘어나야"),
    ({"Z": 10}, {}, "학점으로 읽지 못했어요"),
    ({"A+B": 70}, {}, "학점으로 읽지 못했어요"),
    ({"A": -5}, {}, "0~100"),
    ({"A": "많이"}, {}, "숫자여야"),
    ({"C~A": 20}, {}, "거꾸로"),
    ({"A+, B+": 20}, {}, "이어지지 않아요"),
    ({}, {}, "{학점: %}"),
])
def test_relative_errors(spec, kw, msg):
    with pytest.raises(ValueError, match=msg):
        cg.relative_dist(spec, ASIC_NOW, **kw)


def test_absolute_cutoffs_rules():
    assert cg.absolute_cutoffs({"A": 90, "B": 80, "C": 70}) == {"A+": 95.0, "A0": 90.0, "B+": 85.0, "B0": 80.0,
                                                               "C+": 75.0, "C0": 70.0}
    assert cg.absolute_cutoffs({"A+": 97, "A0": 90, "B": "80점 이상"}) == {"A+": 97.0, "A0": 90.0, "B+": 85.0,
                                                                          "B0": 80.0}
    with pytest.raises(ValueError, match="낮아야"):
        cg.absolute_cutoffs({"A": 80, "B": 90})
    with pytest.raises(ValueError, match="F는 기준 없음"):
        cg.absolute_cutoffs({"A": 90, "F": 0})


# ---------------------------------------------------------------- 학점 비율: build → 실제 경로 → 추정

def test_set_dist_relative_through_the_real_route(demo):
    app, store = demo.app, demo.store
    before = copy.deepcopy(store.data["grades"]["101"])
    path, body = build(app, "grades.set_dist", course="ASIC", dist={"A": 50})
    assert path == "/api/grades/101"
    assert body == {**cg.screen_body(before), "dist": body["dist"]}   # 바꾼 것은 학점 비율뿐
    assert body["dist"]["A+"] == 25.0 and body["dist"]["A0"] == 25.0 and round(sum(body["dist"].values()), 6) == 100
    lines = preview(app, "grades.set_dist", course="ASIC", dist={"A": 50})
    assert ("학점 비율 A", "40%", "50%") in lines and ("학점 비율 B+~F", "60%", "50%") in lines
    assert ("학점 기준", "강의 글(Updated Grading Policy)", "직접 넣은 비율") in lines
    assert ("평가 비중 자동 채우기", "강의 글 따라 자동", "직접 넣은 값으로 고정") in lines
    expected = screen_saved(store, "101", body)
    st = actions._call(app, path, body)
    assert store.data["grades"]["101"] == expected
    assert store.data["grades"]["101"]["dist_auto"] == before["dist_auto"]   # 강의 글 비율은 남는다(사용자 것이 이김)
    c = course(st, "101")
    assert c["letter_basis"] == "user" and c["dist_used"] == body["dist"]
    # 사용자가 직접 시킨 것 — 근거 글이 없는 과목(102)도 바로 된다
    path, body = build(app, "grades.set_dist", course="디지털", dist={"A": 40, "B": 40, "C": 20})
    actions._call(app, path, body)
    assert store.data["grades"]["102"]["dist"] == body["dist"] and store.data["grades"]["102"]["components"] == []
    # 같은 비율이면 바꿀 것이 없다, 되돌리기(reset)는 직접 넣은 비율을 지운다
    assert "이미" in err(app, "grades.set_dist", course="102", dist={"A": 40, "B": 40, "C": 20})
    path, body = build(app, "grades.set_dist", course="102", reset=True)
    assert body["dist"] is None
    assert ("학점 기준", "직접 넣은 비율", "기본 비율") in preview(app, "grades.set_dist", course="102", reset=True)
    actions._call(app, path, body)
    assert store.data["grades"]["102"]["dist"] is None
    assert "직접 넣은 학점 비율이 없어요" in err(app, "grades.set_dist", course="102", reset=True)


def _expected_estimate(st, store, cid):
    """화면 묶음의 설정(세부 점수가 붙은 것) + 저장된 비율로 직접 계산한 추정."""
    c = course(st, cid)
    return g.estimate(gradebook.expand_components(c["config"]["components"]), store.data["grades"][cid]["dist"],
                      class_size=store.data["grades"][cid]["class_size"])


def test_estimate_uses_new_dist_and_weights(demo):
    app, store = demo.app, demo.store
    run(app, "grades.set_weights", course="102", weights=[
        {"name": "중간고사", "weight": 30}, {"name": "과제", "weight": 20}, {"name": "기말고사", "weight": 40},
        {"name": "출석", "weight": 10}])
    # 중간고사는 데모 메시지의 점수(78, 평균 65.3, 표준편차 12.1)가 자동으로 연결된 세부 점수 → 직접 넣지 않는다
    assert "세부 점수" in err(app, "grades.set_score", course="102", component="Midterm exam", score=80)
    run(app, "grades.set_score", course="102", component="과제", score="18", max_score=20, mean=16.2, sd=2.4)
    run(app, "grades.set_class_size", course="102", size="48명")
    cfg = store.data["grades"]["102"]
    assert [c["name"] for c in cfg["components"]] == ["중간고사", "과제", "기말고사", "출석"]
    assert cfg["components"][3]["fixed"] is True and cfg["class_size"] == 48   # 출석은 순위 제외(앱이 자동으로 넣을 때와 같음)
    st = run(app, "grades.set_dist", course="102", dist={"A+": 100})
    est = course(st, "102")["estimate"]
    assert est["grade"] == "A+" and course(st, "102")["letter_basis"] == "user"   # 모두 A+인 비율 → A+
    want = _expected_estimate(st, store, "102")
    assert (est["grade"], round(est["top"], 6)) == (want["grade"], round(want["top"], 6))
    st = run(app, "grades.set_dist", course="102", dist={"A": 5, "B": 5, "C": 5, "D": 5, "F": 80})
    est = course(st, "102")["estimate"]
    assert est["grade"] not in ("A+", "A0") and est["grade"] == _expected_estimate(st, store, "102")["grade"]
    # 비중을 바꾸면 추정의 항목 비중도 바뀐다(합이 100을 넘는 순서는 경로처럼 막힌다)
    assert "100%를 넘을 수 없어요" in err(app, "grades.set_component", course="102", component="중간고사", weight=40)
    run(app, "grades.set_component", course="102", component="기말고사", weight=30)
    st = run(app, "grades.set_component", course="102", component="중간고사", weight=40)
    rows = {r["name"]: r["weight"] for r in course(st, "102")["estimate"]["components"]}
    assert (rows["중간고사"], rows["기말고사"]) == (40, 30) and course(st, "102")["weight_total"] == 100


def test_set_dist_absolute_saves_through_route(demo):
    """절대평가 기준표도 말로(리드가 save_course·distribution.effective에 사용자 기준표를 더함 — USER_CUTOFFS)."""
    from lms_agent import actions, distribution
    app = demo.app
    assert gradebook.USER_CUTOFFS is True
    path, body = build(app, "grades.set_dist", course="ASIC", kind="absolute", dist={"A": 90, "B": 80, "C": 70})
    assert body["dist"] is None and body["cutoffs"]["A0"] == 90.0 and body["cutoffs"]["A+"] == 95.0
    lines = preview(app, "grades.set_dist", course="ASIC", kind="absolute", dist={"A": 90, "B": 80, "C": 70})
    assert ("학점 기준 A0", "—", "90점 이상") in lines
    assert ("학점 기준", "강의 글(Updated Grading Policy)", "직접 넣은 절대평가 기준") in lines
    actions._call(app, path, body)
    app.store.reload_if_changed()
    cid = path.rsplit("/", 1)[-1]
    eff = distribution.effective(app.store.data["grades"][cid])
    assert (eff["by"], eff["kind"], eff["cutoffs"]["A0"]) == ("user", "absolute", 90.0)


def test_set_dist_pass_fail_and_hidden(demo):
    app, store = demo.app, demo.store
    store.data.setdefault("course_prefs", {})["104"] = {"mode": "pass_fail"}
    assert "P/NP" in err(app, "grades.set_dist", course="Capstone", dist={"A": 50})
    store.data["course_prefs"]["103"] = {"mode": "hidden"}
    assert "제외한 과목" in err(app, "grades.set_dist", course="반도체", dist={"A": 50})
    assert "kind는" in err(app, "grades.set_dist", course="ASIC", kind="curve", dist={"A": 50})


# ---------------------------------------------------------------- 평가 비중·항목

def test_set_weights_keeps_scores_and_matches_the_screen(demo):
    app, store = demo.app, demo.store
    gradebook.save_course(store, "102", {"components": [
        {"name": "중간고사", "weight": 30, "score": 78, "mean": 65.3, "sd": 12.1, "source": "메시지 '중간고사 성적 안내'"},
        {"name": "과제", "weight": 20, "score": 18, "max_score": 20}, {"name": "기말고사", "weight": 50}],
        "dist": None, "class_size": 48})
    weights = {"Midterm exam": 40, "Homework": 25, "출석": 10}   # {이름: 비중}도 받는다, 별칭은 같은 항목
    path, body = build(app, "grades.set_weights", course="EE342", weights=weights)
    assert path == "/api/grades/102"
    assert body["components"] == [
        {"name": "중간고사", "weight": 40.0, "score": 78.0, "max_score": None, "mean": 65.3, "sd": 12.1, "fixed": False,
         "source": "메시지 '중간고사 성적 안내'"},
        {"name": "과제", "weight": 25.0, "score": 18.0, "max_score": 20.0, "mean": None, "sd": None, "fixed": False,
         "source": None},
        {"name": "출석", "weight": 10.0, "score": None, "max_score": None, "mean": None, "sd": None, "fixed": True,
         "source": None}]
    assert body["class_size"] == 48 and body["dist"] is None
    lines = preview(app, "grades.set_weights", course="EE342", weights=weights)
    assert ("중간고사 비중", "30%", "40%") in lines and ("기말고사 비중", "50%", "지움") in lines
    assert ("출석 비중", "없음", "10% (신규 · 순위 제외)") in lines
    assert ("비중 합", "100%", "75% (나머지 25%는 평가 항목이 없어 계산 못 해요)") in lines   # 100이 안 돼도 메모만
    expected = screen_saved(store, "102", body)
    st = actions._call(app, path, body)
    assert store.data["grades"]["102"] == expected and course(st, "102")["weight_missing"] == 25


@pytest.mark.parametrize("weights,msg", [
    ([{"name": "중간고사", "weight": 70}, {"name": "기말고사", "weight": 40}], "100%를 넘을 수 없어요"),
    ([{"name": "중간고사", "weight": 130}], "0~100"),
    ([{"name": "중간고사", "weight": 30}, {"name": "Midterm exam", "weight": 30}], "한 번만"),
    ([{"name": "", "weight": 30}], "이름이 비어"),
    ("중간 30", "목록이어야"),
])
def test_set_weights_errors(demo, weights, msg):
    gradebook.save_course(demo.store, "102", {"components": [{"name": "중간고사", "weight": 30}]})
    assert msg in err(demo.app, "grades.set_weights", course="102", weights=weights)


def test_set_weights_ambiguous_name(demo):
    msg = err(demo.app, "grades.set_weights", course="ASIC", weights=[{"name": "Quiz", "weight": 10}])
    assert "여럿" in msg and "Midterm quiz" in msg and "Final quiz" in msg


def test_set_component_change_add_and_errors(demo):
    app, store = demo.app, demo.store
    lines = preview(app, "grades.set_component", course="ASIC", component="과제", weight=20)   # '과제' = Homework
    assert ("Homework assignments 비중", "30%", "20%") in lines
    run(app, "grades.set_component", course="ASIC", component="과제", weight=20)
    assert "Report" not in [c["name"] for c in store.data["grades"]["101"]["components"]]
    path, body = build(app, "grades.set_component", course="ASIC", component="Report", weight=10)
    assert body["components"][-1]["name"] == "Report" and body["components"][-1]["weight"] == 10.0
    assert ("Report 비중", "없음", "10% (신규)") in preview(app, "grades.set_component", course="ASIC",
                                                         component="Report", weight=10)
    expected = screen_saved(store, "101", body)
    actions._call(app, path, body)
    assert store.data["grades"]["101"] == expected
    assert all(not c.get("auto") for c in store.data["grades"]["101"]["components"])   # 화면 저장처럼 직접 넣은 값
    assert "이미 10%" in err(app, "grades.set_component", course="ASIC", component="report", weight=10)
    assert "100%를 넘을 수 없어요" in err(app, "grades.set_component", course="ASIC", component="Lab", weight=20)
    assert "여럿" in err(app, "grades.set_component", course="ASIC", component="Quiz", weight=1)
    msg = err(app, "grades.set_component", course="없는 과목", component="Quiz", weight=1)
    assert "찾지 못했어요" in msg and "ASIC설계" in msg and "디지털신호처리" in msg


def test_remove_component_merge_and_keeps_removed_record(demo):
    app, store = demo.app, demo.store
    store.data["grades"]["101"]["removed"] = [{"name": "Participation", "weight": 5, "merge_into": None,
                                              "at": "2026-11-01T10:00:00+09:00", "by": "ai", "reason": "중복"}]
    lines = preview(app, "grades.remove_component", course="ASIC", component="Final quiz", merge_into="Final project")
    assert ("Final project 비중", "25%", "30%") in lines and ("Final quiz 비중", "5%", "지움(Final project에 더함)") in lines
    assert not any(x[0] == "비중 합" for x in lines)
    path, body = build(app, "grades.remove_component", course="ASIC", component="Final quiz",
                       merge_into="Final project")
    expected = screen_saved(store, "101", body)
    actions._call(app, path, body)
    cfg = store.data["grades"]["101"]
    assert cfg == expected and "Final quiz" not in [c["name"] for c in cfg["components"]]
    assert next(c for c in cfg["components"] if c["name"] == "Final project")["weight"] == 30.0
    assert [r["name"] for r in cfg["removed"]] == ["Participation"]   # 지운 기록은 경로가 그대로 둔다
    core.sync(Canvas(demo.base, DEMO_TOKEN), store)   # 다음 동기화의 자동 채우기가 지운 항목을 되살리지 않는다
    assert "Final quiz" not in [c["name"] for c in store.data["grades"]["101"]["components"]]
    # 지우기는 이름이 분명할 때만(실습→과제 같은 넓은 규칙은 안 씀)
    msg = err(app, "grades.remove_component", course="ASIC", component="Lab")
    assert "없어요" in msg and "Homework assignments" in msg
    assert "같은 평가 항목" in err(app, "grades.remove_component", course="ASIC", component="Midterm quiz",
                                merge_into="midterm quiz")
    lines = preview(app, "grades.remove_component", course="ASIC", component="Attendance")
    assert ("비중 합", "100%", "90% (나머지 10%는 평가 항목이 없어 계산 못 해요)") in lines


# ---------------------------------------------------------------- 점수·수강 인원

def test_set_score_and_guards(demo):
    app, store = demo.app, demo.store
    lines = preview(app, "grades.set_score", course="ASIC", component="Midterm project", score=85, max_score=100,
                    mean=70, sd=10)
    assert ("Midterm project 점수", "—", "85") in lines and ("Midterm project 표준편차", "—", "10") in lines
    path, body = build(app, "grades.set_score", course="ASIC", component="Midterm project", score=85, mean=70, sd=10)
    row = next(c for c in body["components"] if c["name"] == "Midterm project")
    assert (row["score"], row["max_score"], row["mean"], row["sd"]) == (85.0, None, 70.0, 10.0)
    expected = screen_saved(store, "101", body)
    actions._call(app, path, body)
    assert store.data["grades"]["101"] == expected
    # 만점보다 높은 값은 막지 않고(화면과 같음) 확인 메모
    assert any(x[0] == "확인" for x in preview(app, "grades.set_score", course="ASIC", component="Midterm quiz",
                                                 score=12, max_score=10))
    # 연결된 세부 점수(LAB3 보고서)로 계산하는 항목은 화면처럼 직접 넣지 않는다
    assert "세부 점수" in err(app, "grades.set_score", course="ASIC", component="Homework", score=90)
    assert "0 이상" in err(app, "grades.set_score", course="ASIC", component="Midterm project", score=-1)
    assert "점수(score)" in err(app, "grades.set_score", course="ASIC", component="Midterm project")
    assert "이미" in err(app, "grades.set_score", course="ASIC", component="Midterm project", score=85)
    run(app, "grades.set_score", course="ASIC", component="Midterm project", clear=True)
    row = next(c for c in store.data["grades"]["101"]["components"] if c["name"] == "Midterm project")
    assert (row["score"], row["mean"], row["sd"]) == (None, None, None)


def test_set_class_size(demo):
    app, store = demo.app, demo.store
    assert preview(app, "grades.set_class_size", course="ASIC", size=48)[0] == ("수강 인원", "—", "48명")
    run(app, "grades.set_class_size", course="ASIC", size=48)
    assert store.data["grades"]["101"]["class_size"] == 48
    assert "이미" in err(app, "grades.set_class_size", course="ASIC", size=48)
    assert "정수" in err(app, "grades.set_class_size", course="ASIC", size=4.5)
    assert "1~100000" in err(app, "grades.set_class_size", course="ASIC", size=0)
    run(app, "grades.set_class_size", course="ASIC", size=None)
    assert store.data["grades"]["101"]["class_size"] is None


# ---------------------------------------------------------------- 점수 후보 연결·개수

def test_link_and_unlink_score(demo):
    app, store = demo.app, demo.store
    path, body = build(app, "grades.link_score", score="LAB3 보고서", component="Midterm project")   # 과목은 후보에서
    assert (path, body) == ("/api/grades/101/link", {"key": "canvas:1011", "component": "Midterm project"})
    assert preview(app, "grades.link_score", score="lab3", component="Midterm project") == [
        ("점수 'LAB3 보고서 9/10'", "Homework assignments(자동)", "Midterm project(직접)")]
    st = actions._call(app, path, body)
    assert course(st, "101")["links"]["canvas:1011"]["component"] == "Midterm project"
    assert store.data["grades"]["101"]["components"][0].get("auto") is True   # 연결은 비중의 자동 표시를 지우지 않는다
    assert "이미" in err(app, "grades.link_score", course="ASIC", score="canvas:1011", component="Midterm project")
    run(app, "grades.unlink_score", course="ASIC", score="LAB3")
    assert store.data["grade_links"]["101"]["canvas:1011"]["component"] is None
    assert "이미" in err(app, "grades.unlink_score", course="ASIC", score="LAB3")
    assert preview(app, "grades.unlink_score", course="ASIC", score="LAB3", reset=True)[0][2] == "자동으로 정함"
    st = run(app, "grades.unlink_score", course="ASIC", score="LAB3", reset=True)
    assert "101" not in (store.data.get("grade_links") or {})
    assert course(st, "101")["links"]["canvas:1011"]["by"] == "rules"
    assert "직접 정한 연결이 없어요" in err(app, "grades.unlink_score", course="ASIC", score="LAB3", reset=True)
    msg = err(app, "grades.link_score", course="ASIC", score="LAB9", component="Midterm project")
    assert "점수 후보에 'LAB9'" in msg and "LAB3 보고서" in msg
    assert "여럿" in err(app, "grades.link_score", course="ASIC", score="LAB3", component="Quiz")


def test_set_count(demo):
    app, store = demo.app, demo.store
    path, body = build(app, "grades.set_count", course="ASIC", component="Homework", n=8)
    assert (path, body) == ("/api/grades/101/count", {"component": "Homework assignments", "n": 8})
    label, before, after = preview(app, "grades.set_count", course="ASIC", component="Homework", n=8)[0]
    assert label == "Homework assignments 개수" and after == "8개(직접)" and before != after
    st = actions._call(app, path, body)
    comp = next(c for c in course(st, "101")["config"]["components"] if c["name"] == "Homework assignments")
    assert comp["count"]["n"] == 8 and comp["count"]["by"] == "user"
    assert "이미 8개" in err(app, "grades.set_count", course="ASIC", component="Homework", n=8)
    assert "1~30" in err(app, "grades.set_count", course="ASIC", component="Homework", n=31)
    assert "정수" in err(app, "grades.set_count", course="ASIC", component="Homework", n=2.5)
    run(app, "grades.set_count", course="ASIC", component="Homework", n=None)
    assert "101" not in (store.data.get("grade_counts") or {})
    assert "직접 넣은 개수가 없어요" in err(app, "grades.set_count", course="ASIC", component="Homework", n=None)


# ---------------------------------------------------------------- 마감·시험 → 평가 항목

def test_item_component(demo):
    app, store = demo.app, demo.store
    path, body = build(app, "items.set_component", item="LAB4 보고서", component="Final project")
    iid = path.split("/")[3]
    assert path == f"/api/item/{iid}/component" and body == {"component": "Final project"}
    (label, before, after), = preview(app, "items.set_component", item="lab 4 보고서", component="Final project")
    assert "LAB4 보고서" in label and before == "Homework assignments(자동)" and after == "Final project(직접)"
    st = actions._call(app, path, body)
    assert store.data["item_links"][iid]["component"] == "Final project"
    lab4 = [it for it in st["items"] if it.get("title") == "LAB4 보고서"]
    assert lab4 and all(it["component"] == "Final project" for it in lab4)   # 같은 과제 후보 묶음 모두
    assert "이미" in err(app, "items.set_component", item=iid, component="final project")
    run(app, "items.set_component", item=iid, exclude=True)
    assert store.data["item_links"][iid]["component"] is None
    run(app, "items.set_component", item=iid, reset=True)
    assert iid not in store.data["item_links"]
    assert "직접 정한 연결이 없어요" in err(app, "items.set_component", item=iid, reset=True)
    video = next(it for it in store.view() if it.get("origin") == "video")
    assert "강의 영상" in err(app, "items.set_component", item=video["id"], component="Final project")
    assert "아직 평가 항목이 없어요" in err(app, "items.set_component", item="기말고사", course="반도체",
                                       component="기말")
    msg = err(app, "items.set_component", item="LAB4 보고서", component="Lecture notes")
    assert "없어요" in msg and "Final project" in msg
    assert "찾지 못했어요" in err(app, "items.set_component", item="없는 과제 99", component="Final project")


# ---------------------------------------------------------------- 하네스(H1)와 함께: plan → run → undo

def test_plan_run_undo_end_to_end(demo):
    app, store = demo.app, demo.store
    planned = caps.plan(app, "grades.set_dist", {"course": "ASIC", "dist": {"A": 50}})
    if planned.get("error") == "아직 준비 중인 능력이에요":
        pytest.skip("H1의 capabilities.plan/run/undo가 아직 준비 중(뼈대) — 들어오면 이 시험이 돈다")
    assert planned["ok"], planned["error"]
    assert planned["tier"] == "direct" and planned["undoable"] and planned["changes"]
    assert any(c["label"] == "학점 비율 A" and c["after"] == "50%" for c in planned["changes"])
    before = copy.deepcopy(store.data["grades"]["101"])
    res = caps.run(app, planned)
    assert res["ok"] and res["undo"]
    assert store.data["grades"]["101"]["dist"]["A+"] == 25.0
    caps.undo(app, res["undo"])
    assert store.data["grades"]["101"] == before


@pytest.mark.parametrize("cid,params,ok", [
    ("grades.set_dist", {"course": "ASIC", "dist": {"A": 40}}, "학점 기준"),   # 계기: 'ASIC 학점 비율 A 40%로' — 근거 글 없이
    ("grades.set_component", {"course": "ASIC설계", "component": "과제", "weight": 20}, "Homework assignments 비중"),
    ("grades.set_class_size", {"course": "EE401", "size": None}, None),          # 이미 비어 있음 → 바꿀 것 없음
    ("grades.set_count", {"course": "asic", "component": "Homework", "n": None}, None),
    ("grades.link_score", {"score": "LAB3", "component": "Midterm project"}, "점수 'LAB3 보고서 9/10'"),
    ("grades.remove_component", {"course": "ASIC", "component": "Quiz"}, None),  # 여럿 → 후보와 함께 오류
])
def test_plan_through_the_harness(demo, cid, params, ok):
    planned = caps.plan(demo.app, cid, params)
    if planned.get("error") == "아직 준비 중인 능력이에요":
        pytest.skip("H1의 capabilities.plan이 아직 준비 중(뼈대)")
    if ok is None:
        assert not planned["ok"] and planned["error"]
    else:
        assert planned["ok"], planned["error"]
        assert any(c["label"].startswith(ok) for c in planned["changes"])
    assert cid in caps.catalog_text("grades") or cid in caps.catalog_text()


def test_user_cutoffs_saved_and_win_then_relative_replaces_them(tmp_path):
    """사용자 절대평가 기준표(리드, 계약 28-2): 저장하면 학점 기준이 '직접 넣은 절대평가', 상대 비율을 다시 넣으면 기준표는 지워진다."""
    from lms_agent import distribution, gradebook
    from lms_agent.store import Store
    s = Store(tmp_path / "s.json")
    s.data["grades"] = {"1": {"components": [{"name": "기말", "weight": 100.0}], "dist": None, "class_size": None}}
    base = {"components": [{"name": "기말", "weight": 100}], "class_size": None}
    gradebook.save_course(s, "1", {**base, "dist": None, "cutoffs": {"A+": 95, "A0": 90, "B+": 85, "B0": 80}})
    eff = distribution.effective(s.data["grades"]["1"])
    assert (eff["by"], eff["kind"], eff["cutoffs"]["A0"]) == ("user", "absolute", 90.0)
    gradebook.save_course(s, "1", {**base, "dist": None})                       # 기준표 칸 없이 저장 → 그대로
    assert distribution.effective(s.data["grades"]["1"])["kind"] == "absolute"
    import pytest
    with pytest.raises(ValueError):
        gradebook.save_course(s, "1", {**base, "dist": None, "cutoffs": {"A0": 80, "B0": 85}})   # 거꾸로
    with pytest.raises(ValueError):
        gradebook.save_course(s, "1", {**base, "dist": dict(distribution.DEFAULT_DISTRIBUTION), "cutoffs": {"A0": 90}})
    gradebook.save_course(s, "1", {**base, "dist": dict(distribution.DEFAULT_DISTRIBUTION)})   # 상대 비율 → 기준표 지움
    cfg = s.data["grades"]["1"]
    assert "cutoffs" not in cfg and distribution.effective(cfg)["kind"] == "relative"
