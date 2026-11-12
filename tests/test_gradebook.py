"""성적 화면용 묶음: 설정 저장·검사, 점수 문장 후보, 예상 학점, 남은 시험 필요 점수, 우선순위."""
import pytest

from lms_agent import core, gradebook
from lms_agent.canvas import Canvas
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.store import Store


@pytest.fixture()
def store(tmp_path):
    mock, base = start_mock_canvas()
    s = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), s)
    yield s
    mock.shutdown()


def courses(s):
    return core.course_summaries(s.data["cache"])


def test_score_candidate_from_private_message(store):
    c = gradebook.candidates(store)
    mid = next(x for x in c if x["source_type"] == "inbox" and x["course_short"] == "디지털신호처리")
    assert (mid["score"], mid["mean"], mid["sd"]) == (78, 65.3, 12.1)
    assert mid["source_title"] == "중간고사 성적 안내" and mid["sentence"]


def test_estimate_and_needed_for_remaining_exam(store):
    gradebook.save_course(store, "102", {"components": [
        {"name": "중간고사", "weight": 30, "score": 78, "mean": 65.3, "sd": 12.1, "source": "메시지 '중간고사 성적 안내'"},
        {"name": "과제", "weight": 20, "score": 18, "max_score": 20, "mean": 16.2, "sd": 2.4},
        {"name": "기말고사", "weight": 40},
        {"name": "출석", "weight": 10, "fixed": True},
    ], "class_size": 48})
    sec = gradebook.section(store, courses(store))
    dsp = next(x for x in sec["courses"] if x["course_id"] == "102")
    est = dsp["estimate"]
    assert 5 < est["top"] < 30 and est["range"][0] <= est["top"] <= est["range"][1]
    assert est["grade"] in ("A+", "A0", "B+") and est["rank"] >= 1
    assert dsp["using_default_dist"] and dsp["needed"]
    assert {n["component"] for n in dsp["needed"]} == {"기말고사"}
    assert sec["priorities"] and sec["priorities"][0]["course"] == "디지털신호처리"
    # 설정이 없는 과목은 추정 없이 빈 칸
    asic = next(x for x in sec["courses"] if x["course_id"] == "101")
    assert asic["estimate"] is None and asic["config"]["components"] == []
    assert asic["canvas_score"] == 92.5


@pytest.mark.parametrize("body,msg", [
    ({"components": [{"name": "", "weight": 10}]}, "이름"),
    ({"components": [{"name": "중간", "weight": 80}, {"name": "기말", "weight": 40}]}, "100%"),
    ({"components": [{"name": "중간", "weight": "abc"}]}, "숫자"),
    ({"components": [], "dist": {"A+": 50, "A0": 50, "B+": 50}}, "100"),
])
def test_invalid_config_rejected(store, body, msg):
    with pytest.raises(ValueError, match=msg):
        gradebook.save_course(store, "102", body)


def test_custom_distribution_used(store):
    dist = {"A+": 10, "A0": 10, "B+": 20, "B0": 20, "C+": 20, "C0": 15, "D+": 0, "D0": 0, "F": 5}
    gradebook.save_course(store, "102", {"components": [
        {"name": "중간고사", "weight": 50, "score": 78, "mean": 65.3, "sd": 12.1}], "dist": dist})
    dsp = next(x for x in gradebook.section(store, courses(store))["courses"] if x["course_id"] == "102")
    assert not dsp["using_default_dist"] and dsp["config"]["dist"]["A+"] == 10
