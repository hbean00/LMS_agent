"""베타테스터 제보(2026-11-13): 'Quiz 1 (10) | Midterm (100)'처럼 열마다 다른 평가인 점수표 PDF가 성적에 자동으로 안 들어감.
원인: 표 전체를 점수 하나(마지막 열)로 만들고 이름을 파일 이름('성적.pdf' → 없음)에서 가져와 연결할 수 없었음.
학번·점수는 모두 가짜."""
from lms_agent import gradebook
from lms_agent.store import Store

TABLE = "순번 학번 Quiz 1 (10) Midterm (100)\n" + "\n".join(
    f"{i} 20990000{i:02d} {q} {m}" for i, q, m in
    [(1, "0", "45"), (2, "7.5", "51"), (3, "9", "63"), (4, "9.5", "7"), (5, "9", "89"), (6, "10", "90"),
     (7, "10", "94"), (8, "8", "100"), (9, "2", "69")])


def _store(tmp_path, text=TABLE, filename="성적.pdf"):
    s = Store(tmp_path / "s.json")
    s.data["cache"] = {"courses": [{"id": "77", "name": "신호와 시스템"}], "assignments": [], "announcements": [],
                       "conversations": []}
    s.data["pdf_texts"] = {"77:5001": {"text": text, "filename": filename, "course_id": "77",
                                       "posted_at": "2026-11-10T10:00:00+09:00", "ok": True}}
    s.data["settings"]["student_id"] = "2099000002"
    return s


def test_separate_assessments_become_separate_candidates(tmp_path):
    s = _store(tmp_path)
    cands = [c for c in gradebook.candidates(s) if c["source_type"] == "pdf"]
    assert sorted((c["label"], c["score"], c["max_score"]) for c in cands) == [("Midterm", 51.0, 100.0),
                                                                               ("Quiz 1", 7.5, 10.0)]
    assert len({c["key"] for c in cands}) == 2 and all(c["key"].startswith("pdf:77:5001#") for c in cands)
    comps = [{"name": "Quiz", "weight": 10}, {"name": "Midterm", "weight": 30},
             {"name": "Final", "weight": 40}, {"name": "Homework", "weight": 20}]
    links = gradebook.link_candidates(s, "77", comps, cands)
    by_label = {c["label"]: links[c["key"]]["component"] for c in cands}
    assert by_label == {"Quiz 1": "Quiz", "Midterm": "Midterm"}      # 파일 이름이 '성적.pdf'여도 자동 연결


def test_parts_table_still_one_candidate(tmp_path):
    text = ("Student ID Required Items (10) Functional Description (25) Code Correctness (15) Total (50)\n"
            "2099000001 10 25 15 50\n2099000002 8 20 12 40\n2099000003 9 22 15 46")
    s = _store(tmp_path, text, "Lab0_score-1.pdf")
    cands = [c for c in gradebook.candidates(s) if c["source_type"] == "pdf"]
    assert [(c["label"], c["score"], c["key"]) for c in cands] == [("Lab0", 40.0, "pdf:77:5001")]   # key는 그대로
