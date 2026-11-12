"""데모용 가짜 AI CLI. 데모의 [AI로 정리]가 실제 AI 서비스로 글을 보내지 않고도 하네스 전체(가리기·검증·반영·기록)를
그대로 지나가게 한다. agent_cli의 runner 자리에 들어간다: runner(args, input, timeout, cwd) → CompletedProcess.

판단은 단순한 규칙이다(진짜 모델 아님): 같은 과제 후보가 여럿이면 가장 최근 근거를 승인하고 나머지는 제외,
마감 낱말이 있는 단독 후보는 승인, 그 밖에는 보류. 근거는 보낸 글에서 그대로 잘라 쓴다(하네스 검증을 통과해야 하므로).
"""
from __future__ import annotations

import json
import re
import subprocess

DEMO_COMMAND = "claude"
_DEADLINE_WORD = re.compile(r"마감|까지|제출|연장|due|by\s", re.I)
_EVID = re.compile(r"^근거/evidence \d+ \(([^,]*), ([0-9-]*)\): (.*)$")


def _blocks(text: str) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        if line.startswith("### id: "):
            out.append({"id": line[8:].strip(), "group": [], "evidence": []})
        elif out and line.startswith("같은 과제의 다른 후보"):
            out[-1]["group"] = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]
        elif out:
            m = _EVID.match(line)
            if m:
                out[-1]["evidence"].append({"posted": m.group(2), "text": m.group(3)})
    return out


def _quote(text: str) -> str:
    """근거 원문에서 첫 문장(최대 80자) — 그대로 잘라야 검증을 통과한다."""
    first = re.split(r"(?<=[.!?。])\s", text.strip(), maxsplit=1)[0]
    return first[:80].strip() or text[:80]


def _decide(text: str) -> dict:
    blocks = _blocks(text)
    by_id = {b["id"]: b for b in blocks}
    latest = {b["id"]: max((e["posted"] for e in b["evidence"]), default="") for b in blocks}
    decisions = []
    for b in blocks:
        if not b["evidence"]:
            continue
        ev = b["evidence"][0]["text"]
        group = [g for g in b["group"] if g in by_id]
        if group:
            newest = max([b["id"]] + group, key=lambda i: (latest.get(i, ""), i))
            if newest == b["id"]:
                decisions.append({"id": b["id"], "decision": "approved", "evidence": _quote(ev),
                                  "reason": "같은 과제 후보 중 가장 최근 글(연장·변경 공지)이라 이 마감을 골랐어요"})
            else:
                decisions.append({"id": b["id"], "decision": "rejected", "evidence": _quote(ev),
                                  "reason": "나중에 올라온 글이 마감을 바꿔서 이 후보는 뺐어요"})
        elif _DEADLINE_WORD.search(ev):
            decisions.append({"id": b["id"], "decision": "approved", "evidence": _quote(ev),
                              "reason": "근거 글에 마감 날짜가 분명히 적혀 있어요"})
        else:
            decisions.append({"id": b["id"], "decision": "keep", "evidence": _quote(ev),
                              "reason": "마감인지 확실하지 않아 직접 확인이 필요해요"})
    return {"decisions": decisions, "skipped": []}


def runner(args, input, timeout, cwd) -> subprocess.CompletedProcess:
    """triage_review만 흉내 낸다. 다른 작업(평가 비중·마감 찾기)은 '찾지 못함'으로 답한다(데모는 규칙이 찾는다)."""
    if "### id: " in (input or ""):
        model = json.dumps(_decide(input), ensure_ascii=False)
    else:
        model = json.dumps({"components": [], "deadlines": [], "evidence": ""}, ensure_ascii=False)
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": model,
                      "session_id": "demo", "modelUsage": {"demo-rules (가짜 AI)": {"inputTokens": 0}}},
                     ensure_ascii=False)
    return subprocess.CompletedProcess(args, 0, out, "")
