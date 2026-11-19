"""AI로 성적 철저히 찾기 — 계약 29-2 (v0.8.4, 담당 S2).

규칙(scorefile)이 내 점수를 내지 못한 점수 관련 파일(pdf_texts — 첨부 PDF·표·문서·zip 속 파일, 직접 가져온 파일 "import:…")을
사용자가 켠 AI CLI(작업 score_read, agent_cli)로 읽는다. 개인정보가 가장 중요하다: 점수표에는 다른 학생의 학번·점수가 있다.

보내는 글(build_input) — 다른 학생의 줄은 보내지 않는다:
- 내 학번 → [내 학번]. 다른 학번 모양 → [학번]: 6~12자 학번(scorefile과 같은 규칙), 7자리 이상 숫자, 글자 섞인 학번(2025KU0501),
  가린 학번(2021****00), 구분자로 끊긴 학번(2099-12-3456 — 날짜 2026-11-18은 그대로). 내 학번이 '2099 123456'·'2099-12-3456'처럼
  끊겨 있어도 내 학번으로 본다.
- 이름 → [이름]: 학번이 있는 줄의 한글 2~4글자(가린 이름 김*빈·김OO 포함, 결시·면제 같은 상태 낱말은 그대로)와 영문 낱말,
  그 밖의 줄에서는 학번 바로 옆의 한글 이름, 그리고 넘겨받은 이름(names — 사용자 이름)은 어디서나.
  이메일·링크·전화·토큰은 agent_cli의 가리기 그대로.
- 제목·머리글·요약(평균·표준편차·만점…) 줄은 그대로, **내 줄**은 그대로(학번·이름만 가림), 다른 학생 줄은 앞의 SAMPLE_ROWS줄만
  숫자를 <수>로 바꿔 모양만, 나머지는 빼고 개수만 적는다. 표 안의 숫자뿐인 줄·이름뿐인 줄, 표 뒤의 숫자가 많은 줄도 보내지 않는다.
- 앱이 계산한 열 통계(n·평균·표본표준편차·최저·최고·내 값·내 등수·만점)를 덧붙인다(모든 학생 줄로 계산 — 이 PC 안에서).
- 내 줄을 못 찾았거나, 가린 학번이 여러 줄과 맞고 이름으로도 하나를 못 고르면 **AI에 보내지 않는다**(결과 'none' + 이유).

저장: store.data["ai_scores"][출처 key] = {hash, results, at, model, error, note, course_id, filename, attempts, by, local}.
hash = 출처 글·표·학번·판(VERSION)의 sha1 — 같으면 다시 묻지 않는다(실패는 RETRY_HOURS 뒤, 사용자가 누르면 바로 다시).
후보(gradebook.register_candidates): source_type "ai_score", key "ai:<출처>#<label>", decided_by "ai", reason(근거 인용).
같은 파일을 다시 물어 라벨이 같으면 key를 이어 쓴다(사용자의 '넣지 않음'이 그대로 남게).
규칙이 그 파일에서 점수를 내면(나중에 규칙이 좋아져도) AI 후보는 내지 않는다 — 같은 점수를 두 번 세지 않게.
AI가 고른 평가 항목(component)은 grade_links_ai에 기억한다(사용자 연결이 이긴다 — gradebook.link_candidates).
사용자는 다른 후보처럼 '넣지 않음'(POST /api/grades/<cid>/link {key, component: null})으로 뺀다.

실행: after_sync(store, session) — 동기화 뒤 AUTO_PER_SYNC개까지(core._after_sync가 같은 AiSession으로 부른다),
lookup(...) — [AI로 성적 찾기](features/aiscores, 백그라운드). 진행·결과는 lookup_state(store) → state.grades.ai_lookup
(gradetrace.state_fields가 성적 묶음에 넣는다).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import threading
from collections import Counter
from datetime import timedelta
from pathlib import Path

from . import agent_cli, gradebook, scorefile
from .store import hidden_courses
from .timeutil import iso, now_local, parse_iso

VERSION = 1            # 보낼 글 만들기·검사 규칙의 판 — 올리면 같은 파일도 다시 묻는다(hash에 들어감)
TASK = "score_read"
SOURCE_TYPE = "ai_score"
AUTO_PER_SYNC = 3      # 동기화 뒤 자동으로 AI에 묻는 파일 수(하루 한도 안에서)
ROUTE_MAX = 10         # [AI로 성적 찾기] 한 번에 묻는 파일 수
SAMPLE_ROWS = 5        # 모양을 보여 줄 다른 학생 줄(숫자는 <수>)
RETRY_HOURS = 12       # 실패한 파일을 자동으로 다시 묻는 간격
MAX_STATS = 30         # 통계 열 수
STATS_BUDGET = 3_000
ROW_MAX = 600          # 내 줄 한 줄의 최대 글자

MARK = "성적 파일(가린 글) / score file (masked):"   # mock_ai가 이 작업을 알아보는 첫 줄
MY_ROW = "[내 줄 / MY ROW] "
OTHER_ROW = "[다른 학생 줄 — 모양만 / other row, shape only] "
OMITTED = "(… 다른 학생 줄·숫자 줄은 보내지 않음 / other rows omitted …)"
STATS_HEAD = "=== 앱이 계산한 열 통계 / column statistics computed by the app (이 PC에서 모든 줄로 계산) ==="
TEXT_HEAD = "=== 글 / text (제목·머리글·요약·내 줄 — 다른 학생 줄은 모양만) ==="
ME, OTHER_ID, NAME, NUM = "[내 학번]", "[학번]", "[이름]", "<수>"

_SENT = ""       # 가리는 동안만 쓰는 표시(내 학번 자리) — 보내는 글에는 [내 학번]으로 바뀐다
_MASKCH = "*#●○■□xX＊"
_RUN = re.compile(r"[A-Za-z0-9*#●○■□＊]+(?:-[A-Za-z0-9*#●○■□＊]+)*")
_DATE_HY = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}")
_GENERIC = [(label, rx) for label, rx in agent_cli._REDACT if label != "학번"]   # 이메일·링크·토큰·전화
_PHONE = dict(agent_cli._REDACT)["전화"]
_HANGUL = re.compile(r"(?<![가-힣])[가-힣][가-힣*○●＊◯OＯ]{1,3}(?![가-힣*○●＊◯OＯ])")
_EN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2,}(?![A-Za-z0-9])")
_KEEP_KO = frozenset((
    "결시 미응시 미제출 불참 결석 지각 출석 공결 면제 합격 불합격 통과 재수강 청강 포기 철회 이수 미이수 패스 논패스 해당없음 없음 "
    "미입력 비고 합계 평균 총점 점수 학번 이름 성명 학과 전공 분반 학년 순번 번호 등수 순위 석차 만점 배점 최고 최저 중간 기말 과제 "
    "실습 퀴즈 시험 보고서 출결 제출 미채점 채점중 재시험 추가 감점 가점 수정 정정 이메일 링크 토큰 전화 재제출 지연 늦음 해당 무효").split())
_KEEP_EN = frozenset(("pass fail np pf na absent abs excused late none yes no ok incomplete withdrawn audit drop dropped "
                      "exempt waived missing tbd null nan inc ip").split())
_SURNAME = set("김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민진나지엄채원천방공현함변염여추"
               "도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부")
_KO3 = re.compile(r"(?<![가-힣])[가-힣]{3}(?![가-힣])")
_KEEP_KO3 = frozenset((
    "성적표 성적순 강의실 강의명 강의록 정답률 오답률 정답자 조교님 조교실 이번주 이번에 이후에 이전의 이상인 이하인 이의신 장학금 "
    "신청서 고득점 최고점 최저점 최종점 문제지 문제점 안내문 공지문 기준표 주차별 제출일 제출물 마감일 반영률 인원수 명단표 결시자 "
    "원점수 표준점 백분위 석차율 성실히 정확히 공정한 유의사 주의사 방학중 지난번 전체적 기말고 중간고 하위권 상위권 평균값 중간값 "
    "최댓값 최솟값 이메일 보고서 재제출 정정본 수정본 강의자 수강생 담당자 조교가 교수님").split())
_ADJ_NAME = r"[가-힣][가-힣*○●＊◯OＯ]{1,3}"
_ADJ = re.compile(rf"((?:\[학번\]|{_SENT})[\s_\-.,·/()]{{0,2}})({_ADJ_NAME})(?![가-힣])|"
                  rf"(?<![가-힣])({_ADJ_NAME})([\s_\-.,·/()]{{0,2}}(?:\[학번\]|{_SENT}))")
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
_NUMBER = re.compile(r"(?<![\d.])[-+]?\d+(?:\.\d+)?")
_LETTER = re.compile(r"[A-Za-z가-힣]")
_SUMMARY = re.compile(r"^\s*(?:평균|표준\s*편차|합계|총계|최고|최저|최댓값|최솟값|최대값|최소값|중앙값|중간값|인원|응시\s*인원|응시자|"
                      r"분산|만점|배점|(?:average|mean|avg|median|sd|std|stdev|stddev|variance|max(?:imum)?|min(?:imum)?|count|"
                      r"full\s*marks?|possible)(?![A-Za-z]))", re.I)
_STATSWORD = re.compile(r"만점|배점|평균|표준\s*편차|중앙값|중간값|최고|최저|응시|인원|분포|(?<![A-Za-z])(?:mean|median|average|std|"
                        r"stdev|standard\s+deviation|out\s+of|full\s+marks?|max(?:imum)?|min(?:imum)?|students?)(?![A-Za-z])", re.I)
_HEAD_WORD = re.compile(r"^(?:학번|수험\s*번호|student(?:\s*id)?|id|no\.?|번호|순번|연번|#|이름|성명|name|학과|전공|분반|section|"
                        r"점수|score|scores|총점|합계|total|sum|비고|remarks?|grade|학점)$", re.I)
_HEAD_MAX = re.compile(r"[A-Za-z가-힣].*[(\[]\s*\d{1,3}\s*(?:점|pts?)?\s*[)\]]$|^[(\[]\s*\d{1,3}\s*(?:점|pts?)?\s*[)\]]$|"
                       r"[A-Za-z가-힣][^/]*/\s*\d{1,3}$")
_BARE = re.compile(r"^[-+]?\d+(?:\.\d+)?(?:점|pts?|%|％)?$")
_SCOREISH = re.compile(r"score|점수|성적|grade|result|결과|채점|marks?", re.I)
_SCORE_WORDS = re.compile(r"점수|성적|채점|총점|합계|득점|score|grade|total|points?", re.I)
# 값 칸(scorefile._values와 같은 규칙 — 모듈 사본: 점수 규칙이 바뀌어도 가리기·통계가 흔들리지 않게)
_VNUM = re.compile(r"^\(?([-+]?\d{1,3}(?:\.\d+)?)\)?(?:점|pts?)?$")
_VPCT = re.compile(r"^\d+(?:\.\d+)?\s*[%％]$")
_VDATE = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}\.?$|^\d{1,2}[/-]\d{1,2}$|^\d{1,2}:\d{2}(?::\d{2})?$")
_VPLACE = re.compile(r"^(?:-|–|—|/|결시|미응시|미제출|불참|absent|abs\.?|n/?a|na|무)$", re.I)
_CELLNUM = re.compile(r"^\(?([-+]?\d+(?:\.\d+)?)\)?\s*(?:점|pts?)?$")
_SKIP_COL = re.compile(
    r"^(?:학번|수험\s*번호|student(?:\s*(?:id|no\.?|number|name))?|id|no\.?|num(?:ber)?|번호|순번|연번|#|이름|성명|성|명|name|"
    r"학과|전공|소속|department|dept\.?|major|학년|분반|section|등수|순위|석차|rank(?:ing)?|place|%|％|percent(?:age)?|pct|"
    r"백분율|퍼센트|백분위|비고|remarks?|notes?|memo|메모|e-?mail|이메일|전화(?:번호)?|연락처|phone|날짜|일자|일시|date|time)$", re.I)


# ================================================================ 학번·이름 알아보기

def _is_id(tok: str) -> bool:
    """학번처럼 보이는 낱말(scorefile._is_id와 같은 규칙): 6~12자, 숫자(또는 가린 자리)가 대부분, 숫자 2개 이상."""
    if not 6 <= len(tok) <= 12 or not re.fullmatch(r"[A-Za-z0-9*#●○■□＊]+", tok):
        return False
    digits = sum(ch.isdigit() for ch in tok)
    masked = sum(ch in _MASKCH for ch in tok)
    if digits < 2 or digits + masked < len(tok) - (2 if digits >= 6 else 1):
        return False
    letters = len(tok) - digits - masked
    return tok.isdigit() or masked > 0 or letters <= 1 or (letters == 2 and digits >= 6)


def id_like(tok: str) -> bool:
    """다른 학생의 학번일 수 있는 낱말: 학번 모양, 7자리 이상 숫자, 구분자 학번(2099-12-3456·가린 2021-17-****). 날짜는 아님."""
    t = str(tok or "").strip("-")
    if not t:
        return False
    if "-" in t:
        if _DATE_HY.fullmatch(t):
            return False
        core = t.replace("-", "")
        return sum(ch.isdigit() for ch in core) >= 7 and all(ch.isdigit() or ch in _MASKCH for ch in core)
    return bool(re.fullmatch(r"\d{7,}", t)) or _is_id(t)


def _masked_match(tok: str, sid: str) -> bool:
    if len(tok) != len(sid) or not any(ch in _MASKCH for ch in tok):
        return False
    visible = [(a, b) for a, b in zip(tok, sid) if a not in _MASKCH]
    return len(visible) >= len(sid) / 2 and all(a.lower() == b.lower() for a, b in visible)


def _mine_kind(tok: str, sid: str) -> str | None:
    """"exact"(내 학번 — 구분자·앞뒤 영문 몇 자 붙은 것도) | "masked"(보이는 자리가 맞는 가린 학번) | None."""
    if not sid:
        return None
    core = str(tok or "").strip("-").replace("-", "")
    c, s = core.lower(), sid.lower()
    if not c:
        return None
    if c == s or (s in c and re.fullmatch(r"[a-z]{0,3}" + re.escape(s) + r"[a-z]{0,3}", c)):
        return "exact"
    return "masked" if _masked_match(core, sid) else None


def _split_sid_rx(sid: str):
    if not sid or len(sid) < 6:
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + r"[ \t\-._/]?".join(re.escape(ch) for ch in sid) + r"(?![A-Za-z0-9])", re.I)


def _mark_split_sid(line: str, sid: str, rx=None) -> str:
    """'2099 123456'·'2099.12.3456'처럼 구분자(최대 2개)로 끊긴 내 학번 → 표시 하나(한 낱말)."""
    rx = rx or _split_sid_rx(sid)
    if rx is None:
        return line

    def repl(m):
        extra = len(m.group(0)) - len(sid)
        return _SENT if 0 < extra <= 2 else m.group(0)
    return rx.sub(repl, line)


def _ko_name(m) -> str:
    w = m.group(0)
    return w if w in _KEEP_KO else NAME


def _en_name(m) -> str:
    w = m.group(0)
    return w if w.lower() in _KEEP_EN else NAME


def _adj(m) -> str:
    if m.group(2) is not None:
        return m.group(1) + (m.group(2) if m.group(2) in _KEEP_KO else NAME)
    return (m.group(3) if m.group(3) in _KEEP_KO else NAME) + m.group(4)


def _surname(m) -> str:
    w = m.group(0)
    return NAME if w[0] in _SURNAME and w not in _KEEP_KO3 and w not in _KEEP_KO else w


def mask_text(s: str, sid: str, names=(), row: bool = False, mine: bool = False, surnames: bool = False) -> str:
    """글 한 조각을 가린다. row: 학번이 있는 줄(이름·영문 낱말을 모두 가림), mine: 내 줄(가린 학번이 내 학번과 맞으면 [내 학번]),
    surnames: 표 뒤의 숫자가 있는 줄(성씨로 시작하는 한글 세 글자 이름을 가림 — '수석: 홍길동 50점' 같은 것)."""
    if not s:
        return s or ""
    for label, rx in _GENERIC:
        s = rx.sub(f"[{label}]", s)

    def rid(m):
        t = m.group(0)
        k = _mine_kind(t, sid)
        if k == "exact" or (k == "masked" and mine):
            return _SENT
        if k == "masked" or id_like(t):
            return OTHER_ID
        return t
    s = _RUN.sub(rid, s)
    for nm in names or ():
        nm = str(nm or "").strip()
        if len(nm) >= 2:
            s = s.replace(nm, NAME)
    if row:
        s = _HANGUL.sub(_ko_name, s)
        s = _EN.sub(_en_name, s)
    else:
        s = _ADJ.sub(_adj, s)
        if surnames:
            s = _KO3.sub(_surname, s)
    return s.replace(_SENT, ME)


def _name_hit(text: str, names) -> bool:
    """이 줄에 사용자 이름(또는 가린 이름 '김*빈')이 있는지 — 가린 학번이 여러 줄과 맞을 때 내 줄을 고르는 데만 쓴다."""
    for nm in names or ():
        nm = str(nm or "").strip()
        if len(nm) < 2:
            continue
        if nm in text:
            return True
        for tok in re.findall(r"[가-힣A-Za-z*○●＊◯OＯ]+", text):
            if len(tok) == len(nm) and any(ch in "*○●＊◯OＯ" for ch in tok):
                vis = [(a, b) for a, b in zip(tok, nm) if a not in "*○●＊◯OＯ"]
                if len(vis) >= len(nm) / 2 and all(a == b for a, b in vis):
                    return True
    return False


# ================================================================ 글·표 → 줄

def _cell_str(v) -> str:
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return ""
        return str(int(v)) if float(v).is_integer() else f"{v:.10g}"
    return re.sub(r"\s+", " ", str(v)).strip()


def _is_sheet(rec: dict) -> bool:
    return rec.get("kind") == "sheet" and isinstance(rec.get("sheets"), list)


def _text_blocks(lines: list[str]) -> list[int]:
    """줄마다 표 번호(한 글에 점수표가 여럿이면 나눔 — scorefile._split_tables, 실패하면 하나)."""
    out = [0] * len(lines)
    try:
        for b, t in enumerate(scorefile._split_tables(lines) or []):
            a, e = int(t[0]), t[1]
            for i in range(max(0, a), min(len(lines), int(e) if e is not None else len(lines))):
                out[i] = b
    except Exception:  # noqa: BLE001 — 점수 규칙(scorefile)이 바뀌어도 가리기는 돈다(표 하나로)
        return [0] * len(lines)
    return out


def _doc(rec: dict, sid: str) -> tuple[list[dict], bool]:
    """→ ([{"cells", "block", "raw"}], 표 파일인지). 표 파일은 칸 위치를 지키고(빈 칸은 ""), 글은 공백으로 나눈다."""
    if _is_sheet(rec):
        out = []
        for b, sh in enumerate(rec.get("sheets") or []):
            if not isinstance(sh, dict):
                continue
            out.append({"cells": ["[" + _cell_str(sh.get("name") or "시트") + "]"], "block": b, "raw": "", "title": True})
            for r in sh.get("rows") or []:
                row = {}
                for k, v in (r.items() if isinstance(r, dict) else []):
                    try:
                        c = int(k)
                    except (TypeError, ValueError):
                        continue
                    s = _cell_str(v)
                    if s and 0 <= c < 400:
                        row[c] = s
                if row:
                    out.append({"cells": [row.get(c, "") for c in range(max(row) + 1)], "block": b, "raw": ""})
        return out, True
    text = rec.get("text") if isinstance(rec.get("text"), str) else ""
    lines = text.splitlines()
    blocks = _text_blocks(lines)
    rx = _split_sid_rx(sid)
    return [{"cells": _mark_split_sid(ln, sid, rx).split(), "block": blocks[i], "raw": ln}
            for i, ln in enumerate(lines)], False


def _info(cells: list[str], sid: str) -> dict:
    """줄 하나: 학번 수, 내 학번(exact|masked|None), 학번 밖 숫자 수, 글자, 요약·머리글·통계 낱말."""
    joined = " ".join(cells)
    ids, mine, rest, id_cells = 0, None, joined, []
    if _SENT in joined:
        ids, mine = joined.count(_SENT), "exact"
        rest = rest.replace(_SENT, " ")
        id_cells += [i for i, c in enumerate(cells) if _SENT in c]
    for i, c in enumerate(cells):
        for m in _RUN.finditer(c):
            t = m.group(0)
            if _PHONE.fullmatch(t):   # 전화번호(010-1234-5678)는 학번이 아니다 — 보낼 때는 [전화]로 가린다
                continue
            k = _mine_kind(t, sid)
            if k or id_like(t):
                ids += 1
                id_cells.append(i)
                if k == "exact" or (k == "masked" and mine is None):
                    mine = k
                rest = rest.replace(t, " ", 1)
    lead = next((c for c in cells if _LETTER.search(c)), "")
    norm = [re.sub(r"\s+", " ", c).strip() for c in cells]
    # 칸 전체가 숫자인 것(학생 값처럼 보이는 것). 'Quiz 1'의 1처럼 글자 칸 바로 뒤의 두 자리 이하 숫자는 이름의 번호로 본다
    bare = sum(1 for j, c in enumerate(norm) if _BARE.match(c)
               and not (j > 0 and _LETTER.search(norm[j - 1]) and re.fullmatch(r"\d{1,2}", c)))
    return {"ids": ids, "mine": mine, "id_cells": sorted(set(id_cells)), "nums": len(_NUMBER.findall(rest)), "bare": bare,
            "letters": bool(_LETTER.search(rest)), "digits": bool(re.search(r"\d", rest)),
            "summary": bool(lead and _SUMMARY.match(lead)) or bool(_SUMMARY.match(joined)),
            "header": not ids and bare <= 1 and any(_HEAD_WORD.match(c) or _HEAD_MAX.search(c) for c in norm if c),
            "stats": bool(_STATSWORD.search(rest))}


# ================================================================ 보낼 글 만들기

def _values(cells: list[str], id_at: int) -> list:
    vals = []
    for j, t in enumerate(cells):
        if j == id_at:
            continue
        if j < id_at and re.fullmatch(r"\d{1,4}", t):   # 학번 앞의 작은 정수 = 순번
            continue
        if _VPCT.match(t) or _VDATE.match(t):
            continue
        m = _VNUM.match(t)
        if m:
            vals.append(float(m.group(1)))
        elif _VPLACE.match(t):
            vals.append(None)
    return vals


def _col(name: str, values: list, mine, max_score) -> dict:
    n = len(values)
    return {"name": str(name)[:60], "n": n, "mean": round(statistics.fmean(values), 3),
            "sd": round(statistics.stdev(values), 3) if n >= 2 else None, "min": min(values), "max": max(values),
            "mine": mine, "rank": (1 + sum(v > mine for v in values)) if mine is not None else None, "max_score": max_score}


def _names_for(header_tokens: list[str], k: int) -> tuple[list, list]:
    try:
        names, maxes = scorefile._columns(header_tokens, k)
        if len(names) == k:
            return list(names), list(maxes)
    except Exception:  # noqa: BLE001
        pass
    return [f"열 {j + 1}" for j in range(k)], [None] * k


def _text_stats(lines: list[dict], idxs: list[int], rows: list[tuple[int, list]], mine_idx: int | None,
                first: int) -> list[dict]:
    """글 표 하나의 열 통계: rows = [(줄 번호, 값들)], 칸 수가 가장 흔한 줄만."""
    if len(rows) < 2:
        return []
    k = Counter(len(v) for _, v in rows if v).most_common(1)
    if not k or k[0][0] == 0:
        return []
    k = k[0][0]
    good = [(i, v) for i, v in rows if len(v) == k]
    mine = next((v for i, v in good if i == mine_idx), None)
    head = []
    try:
        raw = [lines[i]["raw"] for i in idxs]
        pos = idxs.index(first)
        head = scorefile._header(raw, pos)
    except Exception:  # noqa: BLE001
        head = []
    names, maxes = _names_for(head, k)
    out = []
    for j in range(k):
        vals = [v[j] for _, v in good if v[j] is not None]
        if len(vals) < 2 or re.fullmatch(r"(?i)등수|순위|석차|rank(?:ing)?|place", str(names[j])):
            continue
        out.append(_col(names[j], vals, mine[j] if mine else None, maxes[j]))
    return out


def _sheet_stats(lines: list[dict], idxs: list[int], data: list[int], mine_idx: int | None) -> list[dict]:
    """표 파일 시트 하나의 열 통계(학번·이름·번호·등수·백분율 열, 글자 열, 값이 모두 10000 이상인 열은 뺌)."""
    if len(data) < 2:
        return []
    count = Counter(c for i in data for c in lines[i]["info"]["id_cells"])
    id_cols = {c for c, n in count.items() if n >= max(2, 0.5 * len(data))}
    first = data[0]
    head_i = next((i for i in reversed([x for x in idxs if x < first][-10:])
                   if sum(bool(_LETTER.search(c)) for c in lines[i]["cells"]) >= 2 and not lines[i]["info"]["summary"]), None)
    header = lines[head_i]["cells"] if head_i is not None else []
    width = max(len(lines[i]["cells"]) for i in data)
    idcol = min(id_cols) if id_cols else 0
    out = []
    for c in range(width):
        if c in id_cols:
            continue
        raw = header[c] if c < len(header) else ""
        if raw and _SKIP_COL.match(re.sub(r"\s*[(\[][^)\]]*[)\]]\s*", "", raw).strip() or raw):
            continue
        nums, texts, mine = [], 0, None
        for i in data:
            cell = lines[i]["cells"][c] if c < len(lines[i]["cells"]) else ""
            m = _CELLNUM.match(cell)
            if m:
                x = float(m.group(1))
                nums.append(x)
                if i == mine_idx:
                    mine = x
            elif cell and not _VPLACE.match(cell):
                texts += 1
        if len(nums) < 2 or len(nums) < texts or min(nums) >= 10000:
            continue
        if c < idcol and nums == [float(x) for x in range(1, len(nums) + 1)]:
            continue   # 학번 앞의 1, 2, 3 … = 순번
        name, mx = raw or f"열 {c + 1}", None
        if raw:
            try:
                name, mx = scorefile._head_name(raw)
            except Exception:  # noqa: BLE001
                name, mx = raw, None
        out.append(_col(name or f"열 {c + 1}", nums, mine, mx))
    return out


def _stream(lines: list[dict], idxs: list[int], sid: str) -> dict | None:
    """칸마다 줄이 끊긴 표(학번 줄에 점수가 없음): 학번 낱말을 기준으로 행(segment)을 다시 만든다(scorefile._stream_rows와 같은 생각)."""
    toks = [(i, t) for i in idxs for t in lines[i]["cells"]]

    def kind(t):
        if _SENT in t:
            return "exact"
        runs = [m.group(0) for m in _RUN.finditer(t)]
        if len(runs) != 1 or runs[0] != t.strip("()[],.;:"):
            return None
        k = _mine_kind(runs[0], sid)
        return k or ("other" if id_like(runs[0]) else None)
    pos = [(p, kind(t)) for p, (_, t) in enumerate(toks)]
    pos = [(p, k) for p, k in pos if k]
    if len(pos) < 3:
        return None
    before = [toks[p - 1][1] if p > 0 else "" for p, _ in pos]
    nums = [int(b) if re.fullmatch(r"\d{1,4}", b) else None for b in before]
    seq = sum(1 for a, b in zip(nums, nums[1:]) if a is not None and b is not None and b == a + 1)
    rowno = seq >= 0.8 * (len(pos) - 1)
    starts = [p - (1 if rowno and p > 0 else 0) for p, _ in pos]
    lens = [starts[j + 1] - p - 1 for j, (p, _) in enumerate(pos[:-1])]
    modal = Counter(lens).most_common(1)[0][0] if lens else 0
    segs = []
    for j, (p, k) in enumerate(pos):
        if j + 1 < len(pos):
            end = starts[j + 1]
        else:
            end = p + 1
            while end < len(toks) and end - p - 1 < modal and not _SUMMARY.match(toks[end][1]):
                end += 1
        segs.append({"start": starts[j], "id": p, "end": end, "kind": k,
                     "lines": sorted({toks[q][0] for q in range(starts[j], end)})})
    return {"toks": toks, "segs": segs, "rowno": rowno}


def _pick_mine(kinds: list[tuple[int, str | None]], texts: dict, names) -> tuple[set, bool, bool]:
    """표 하나에서 내 줄: 정확한 학번이 있으면 그 줄들, 없으면 가린 학번이 맞는 줄이 하나일 때만(여럿이면 이름으로 하나를 고를 때만).
    → (내 줄 번호들, 애매함, 이름으로 골랐는지)."""
    exact = {i for i, k in kinds if k == "exact"}
    if exact:
        return exact, False, False
    masked = [i for i, k in kinds if k == "masked"]
    if len(masked) == 1:
        return set(masked), False, False
    if len(masked) > 1:
        hit = [i for i in masked if _name_hit(texts.get(i, ""), names)]
        if len(hit) == 1:
            return set(hit), False, True
        return set(), True, False
    return set(), False, False


def _names_only(cells: list[str]) -> bool:
    """이름처럼 보이는 낱말(한글 2~4글자, 가린 이름 포함)만 있는 짧은 줄."""
    toks = [c for c in cells if c.strip()]
    return 0 < len(toks) <= 3 and all(_HANGUL.fullmatch(t) and t not in _KEEP_KO for t in toks)


def _fmt(x) -> str:
    return "-" if x is None else f"{round(float(x), 3):g}"


def _stats_lines(stats: list[dict]) -> list[str]:
    out = []
    for c in stats[:MAX_STATS]:
        s = (f"표 {c['table']} · 열 '{c['name']}': n {c['n']} · 평균/mean {_fmt(c['mean'])} · 표준편차/sd {_fmt(c['sd'])} · "
             f"최저/min {_fmt(c['min'])} · 최고/max {_fmt(c['max'])}")
        if c.get("mine") is not None:
            s += f" · 내 값/my value {_fmt(c['mine'])} · 내 등수/my rank {c['rank']}/{c['n']}"
        if c.get("max_score"):
            s += f" · 만점/max score {_fmt(c['max_score'])}"
        out.append(s)
    return out


def build_input(rec: dict, sid: str, names=None, components=None, course: str | None = None) -> dict:
    """점수 파일 기록(pdf_texts의 한 칸) → AI에 보낼, 개인정보를 가린 글과 검사 준비물.
    → {"ok", "why"("no_row"|"ambiguous"|"empty"|None), "reason"(한국어), "text"(보낼 글 — ok일 때만), "mine"(보낸 내 줄),
       "values"(내 줄의 숫자), "stated"(보낸 글의 제목·머리글·요약·내 줄의 숫자 — 통계 칸 제외), "stats"(열 통계),
       "rows"(학생 줄 수), "samples", "omitted", "notes"}."""
    sid = re.sub(r"\s+", "", str(sid or ""))
    names = [str(n).strip() for n in (names or []) if str(n or "").strip()]
    rec = rec if isinstance(rec, dict) else {}
    lines, sheet = _doc(rec, sid)
    for ln in lines:
        ln["info"] = _info(ln["cells"], sid)
    blocks: dict = {}
    for i, ln in enumerate(lines):
        blocks.setdefault(ln["block"], []).append(i)
    act: dict = {}          # 줄 번호 → "mine" | "sample" | "keep" | "drop" | "skip"(stream 행의 나머지 줄)
    rendered: dict = {}     # 줄 번호 → 보낼 글(내 줄·모양 줄·남길 줄)
    stats: list[dict] = []
    rows_total, samples, omitted, ambiguous, by_name = 0, 0, 0, False, False
    sep = " | " if sheet else " "
    for b, idxs in blocks.items():
        idl = [i for i in idxs if lines[i]["info"]["ids"]]
        st = None
        if not sheet and len(idl) >= 3 and sum(1 for i in idl if lines[i]["info"]["nums"] >= 1) < 0.5 * len(idl):
            st = _stream(lines, idxs, sid)
        if st is not None:
            segs = st["segs"]
            texts = {j: " ".join(t for _, t in st["toks"][s["start"]:s["end"]]) for j, s in enumerate(segs)}
            mine_set, amb, nm = _pick_mine([(j, s["kind"] if s["kind"] in ("exact", "masked") else None)
                                            for j, s in enumerate(segs)], texts, names)
            ambiguous |= amb
            by_name |= nm
            body = {i for s in segs for i in s["lines"]}
            lo, hi = min(body), max(body)
            for i in idxs:
                if lo <= i <= hi:
                    act[i] = "skip"
            rows, mine_row = [], None
            for j, s in enumerate(segs):
                cells = [t for _, t in st["toks"][s["start"]:s["end"]]]
                id_at = s["id"] - s["start"]
                rows.append((j, _values(cells, id_at)))
                rows_total += 1
                at = s["lines"][0]
                if j in mine_set:
                    mine_row = j if mine_row is None else mine_row
                    act[at] = "mine"
                    rendered[at] = MY_ROW + mask_text(" ".join(cells), sid, names, row=True, mine=True)[:ROW_MAX]
                elif samples < SAMPLE_ROWS and act.get(at) not in ("mine", "sample"):
                    samples += 1
                    act[at] = "sample"
                    rendered[at] = OTHER_ROW + _DIGITS.sub(NUM, mask_text(" ".join(cells), sid, names, row=True))[:ROW_MAX]
                else:
                    omitted += 1
            head_toks = [t for i in idxs if i < lo for t in lines[i]["cells"] if _LETTER.search(t)][-40:]
            k = Counter(len(v) for _, v in rows if v).most_common(1)
            if k and k[0][0]:
                k = k[0][0]
                good = [(j, v) for j, v in rows if len(v) == k]
                mine_vals = next((v for j, v in good if j == mine_row), None)
                cn, cm = _names_for(head_toks, k)
                for c in range(k):
                    vals = [v[c] for _, v in good if v[c] is not None]
                    if len(vals) >= 2:
                        stats.append({**_col(cn[c], vals, mine_vals[c] if mine_vals else None, cm[c]), "table": b + 1})
            region = (lo, hi)
        else:
            texts = {i: " ".join(lines[i]["cells"]) for i in idl}
            mine_set, amb, nm = _pick_mine([(i, lines[i]["info"]["mine"]) for i in idl], texts, names)
            ambiguous |= amb
            by_name |= nm
            region = (min(idl), max(idl)) if idl else None
            for i in idl:
                rows_total += 1
                if i in mine_set:
                    act[i] = "mine"
                    rendered[i] = MY_ROW + mask_text(sep.join(lines[i]["cells"]), sid, names, row=True, mine=True)[:ROW_MAX]
                elif samples < SAMPLE_ROWS:
                    samples += 1
                    act[i] = "sample"
                    rendered[i] = OTHER_ROW + _DIGITS.sub(NUM, mask_text(sep.join(lines[i]["cells"]), sid, names,
                                                                         row=True))[:ROW_MAX]
                else:
                    act[i] = "drop"
                    omitted += 1
            mine_idx = min(mine_set) if mine_set else None
            try:
                if sheet:
                    got = _sheet_stats(lines, idxs, idl, mine_idx)
                else:
                    rows = []
                    for i in idl:
                        ic = lines[i]["info"]["id_cells"]
                        v = _values(lines[i]["cells"], ic[0] if ic else -1)
                        if v:
                            rows.append((i, v))
                    got = _text_stats(lines, idxs, rows, mine_idx, rows[0][0]) if rows else []
            except Exception:  # noqa: BLE001 — 통계는 덤(가리기·보낼 글은 그대로)
                got = []
            stats += [{**c, "table": b + 1} for c in got]
        # 학번 없는 줄: 표 앞(제목·머리글)·표 안(요약·머리글만)·표 뒤(요약·통계 낱말·숫자 적은 글)
        for i in idxs:
            if i in act:
                continue
            inf = lines[i]["info"]
            text = sep.join(lines[i]["cells"]).strip()
            if not text:
                act[i] = "blank"
                continue
            keep_kind = inf["summary"] or inf["header"] or inf["stats"]
            data_like = inf["bare"] >= 3 and not keep_kind      # 학번 없는 학생 줄('12345 홍길동 18 25 43')일 수 있음
            if region is None or i < region[0]:
                # 표 바로 앞의 이름뿐인 줄(이름이 학번 줄 위에 따로 있는 표의 첫 학생)은 보내지 않는다
                lone = region is not None and region[0] - i <= 3 and _names_only(lines[i]["cells"])
                ok = inf["letters"] and not data_like and not lone
                surn = False
            elif i <= region[1]:
                ok = inf["summary"] or inf["header"]
                surn = True
            else:
                ok = inf["letters"] and (keep_kind or inf["bare"] <= 2)
                surn = inf["digits"]
            if ok:
                act[i] = "keep"
                rendered[i] = mask_text(text, sid, names, surnames=surn)[:ROW_MAX]
            else:
                act[i] = "drop"
    mine_lines = [rendered[i] for i in sorted(rendered) if act.get(i) == "mine"]
    fn = str(rec.get("filename") or "")
    base = {"ok": False, "why": None, "reason": None, "text": "", "mine": [], "values": [], "stated": [], "stats": [],
            "rows": rows_total, "samples": 0, "omitted": 0, "notes": [], "filename": mask_text(fn, sid, names, mine=True)}
    if not lines or not rows_total:
        return {**base, "why": "empty", "reason": "학번이 있는 줄(학생 줄)이 없어요 — AI에 보내지 않았어요"}
    if not mine_lines:
        if ambiguous:
            return {**base, "why": "ambiguous",
                    "reason": "가린 학번과 맞는 줄이 여러 개라 내 줄을 정할 수 없어요 — 다른 학생 줄을 보내지 않으려고 AI에 묻지 않았어요"}
        return {**base, "why": "no_row", "reason": "이 파일에 내 학번 줄이 없어요 — AI에 보내지 않았어요"}
    # 보낼 글: 원래 순서대로, 다른 학생 줄을 뺀 자리에는 표시 하나
    body, last_drop = [], False
    for i in range(len(lines)):
        a = act.get(i)
        if a in ("mine", "sample", "keep"):
            body.append((i, a, rendered[i]))
            last_drop = False
        elif a in ("drop", "skip") and not last_drop and (lines[i]["info"]["ids"] or lines[i]["info"]["digits"]):
            body.append((i, "gap", OMITTED))
            last_drop = True
    comps = [str(c) for c in components or [] if str(c).strip()]
    label = scorefile.label_from_filename(fn) if fn else None
    label = mask_text(label, sid, names, mine=True) if label else None
    if label and not re.sub(r"\[(?:내 학번|학번|이름)\]|[\s_\-]", "", label):
        label = None
    head = [f"{MARK} {base['filename'] or '이름 없는 파일'}"]
    if course:
        head.append(f"과목 / course: {course}")
    head.append("평가 항목 / grading components: " + (" | ".join(comps) if comps else "없음 / none"))
    if label:
        head.append(f"파일 이름에서 본 평가 이름 / label from the file name: {label}")
    head.append(f"가림 / masked: 내 학번 → {ME}, 다른 학번 → {OTHER_ID}, 학번 있는 줄의 이름 → {NAME}. 학생 줄 {rows_total}개 중 "
                f"다른 학생 줄은 {samples}줄만 숫자를 {NUM}로 바꿔 모양만, {omitted}줄은 보내지 않았어요 / other students' rows: "
                f"shape only, numbers replaced by {NUM}; the rest omitted.")
    if by_name:
        head.append("가린 학번이 여러 줄과 맞아 이름으로 내 줄을 골랐어요 / own row picked by name among masked IDs")
    stat_lines = _stats_lines(stats)
    while stat_lines and len("\n".join(stat_lines)) > STATS_BUDGET:
        stat_lines.pop()
    stats_used = stats[:len(stat_lines)]

    def compose(entries):
        return "\n".join(head + [TEXT_HEAD] + [s for _, _, s in entries] + [STATS_HEAD]
                         + (stat_lines or ["(계산할 수 있는 숫자 열이 없어요 / no numeric columns)"]))
    limit = agent_cli.MAX_TEXT - 200
    text = compose(body)
    notes = []
    if len(text) > limit:   # 내 줄에서 먼 줄부터 뺀다(요약·머리글은 마지막에)
        mine_at = [i for i, a, _ in body if a == "mine"]

        def prio(e):
            i, a, s = e
            inf = lines[i]["info"] if a == "keep" else {}
            d = min(abs(i - m) for m in mine_at) if mine_at else i
            return d / (1000.0 if inf.get("summary") or inf.get("header") or inf.get("stats") else 1.0)
        order = sorted([e for e in body if e[1] in ("keep", "gap")], key=prio, reverse=True)
        drop, total = set(), len(text)
        for e in order:
            if total <= limit:
                break
            drop.add(e[0])
            total -= len(e[2]) + 1
        body = [x for x in body if x[0] not in drop or x[1] not in ("keep", "gap")]
        text = compose(body)[:agent_cli.MAX_TEXT]
        notes.append("글이 길어 내 줄에서 먼 줄부터 뺐어요")
    stated = []
    for _, a, s in body:
        if a in ("keep", "mine"):
            stated += [float(x) for x in _NUMBER.findall(s)]
    values = [float(x) for s in mine_lines for x in _NUMBER.findall(s)]
    notes.insert(0, f"보내기 전에 가림: 학생 줄 {rows_total}개 중 내 줄 {len(mine_lines)}개만 그대로, 다른 학생 줄 "
                    f"{samples}개는 모양만(숫자 {NUM}), {omitted}개는 보내지 않음")
    return {**base, "ok": True, "text": text, "mine": mine_lines, "values": values, "stated": stated, "stats": stats_used,
            "samples": samples, "omitted": omitted, "notes": notes, "label_hint": label}


# ================================================================ 대상·기억

def _sid(store) -> str | None:
    sid = ((store.data.get("settings") or {}).get("student_id") or "")
    sid = re.sub(r"\s+", "", str(sid))
    return sid or None


_HASH_MEMO: dict = {}
_MEMO_MAX = 1000


def _memo_put(box: dict, k, v):
    if len(box) >= _MEMO_MAX:
        box.clear()
    box[k] = v
    return v


def source_hash(rec: dict, sid: str) -> str:
    """출처 글·표·파일 이름·학번·판의 sha1(앞 20자) — 같으면 같은 답이 나오므로 다시 묻지 않는다.
    상태를 만들 때마다 여러 번 부르므로 같은 글이면 기억한 값(이 프로세스 메모리)."""
    text = str(rec.get("text") or "")
    sheets = rec.get("sheets") if _is_sheet(rec) else None
    try:
        sig = gradebook._sheet_sig(rec) if sheets is not None else ()
    except Exception:  # noqa: BLE001
        sig = (len(sheets or []),)
    mk = (sid, rec.get("filename"), len(text), hash(text), sig, id(sheets) if sheets is not None else None)
    got = _HASH_MEMO.get(mk)
    if got is not None:
        return got
    h = hashlib.sha1()
    h.update(f"{VERSION}|{sid}|{rec.get('filename') or ''}|".encode("utf-8"))
    h.update(text.encode("utf-8", "replace"))
    if sheets is not None:
        h.update(json.dumps(sheets, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return _memo_put(_HASH_MEMO, mk, h.hexdigest()[:20])


_PREP: dict = {}
_PREP_MAX = 128
_prep_lock = threading.Lock()


def prepare(rkey: str, rec: dict, sid: str, names=None, components=None, course=None, h: str | None = None) -> dict:
    """build_input + 기억(같은 글·같은 이름·같은 평가 항목이면 다시 만들지 않음). 결과에 hash."""
    h = h or source_hash(rec, sid)
    k = (rkey, h, tuple(names or ()), tuple(components or ()), course)
    with _prep_lock:
        got = _PREP.get(k)
    if got is not None:
        return got
    got = build_input(rec, sid, names, components=components, course=course)
    got["hash"] = h
    with _prep_lock:
        if len(_PREP) >= _PREP_MAX:
            _PREP.clear()
        _PREP[k] = got
    return got


def rules_found(rkey: str, rec: dict, sid: str) -> bool:
    """규칙(scorefile — 성적 후보와 같은 판정)이 이 파일에서 내 점수를 냈는지. 점수가 없는 결과('애매함' 등)는 못 낸 것으로 본다."""
    try:
        rs = gradebook._score_results(rkey, rec, sid)
    except Exception:  # noqa: BLE001
        return False
    return any(getattr(r, "score", None) is not None and not getattr(r, "ambiguous", False) for r in rs or [])


def _post_title(store, rkey: str, rec: dict) -> str:
    dl = store.data.get("downloads") or {}
    m = dl.get(rkey) or dl.get(str(rec.get("parent_key") or "")) or {}
    return str(m.get("post_title") or rec.get("post_title") or "")


_RELATED_MEMO: dict = {}


def score_related(store, rkey: str, rec: dict, sid: str) -> bool:
    """점수 관련 파일인지: 직접 가져온 성적 파일, 이름(파일·글 제목)에 점수 낱말, 점수표 모양(gradebook과 같은 판정),
    내 학번이 그대로 있고 점수 낱말이 있는 글(개인 성적표)."""
    if rec.get("source") == "upload" or str(rkey).startswith("import:"):
        return True
    fn = str(rec.get("filename") or "")
    if _SCOREISH.search(Path(fn).stem if fn else "") or _SCOREISH.search(_post_title(store, rkey, rec)):
        return True
    mk = (rkey, source_hash(rec, sid))
    if mk in _RELATED_MEMO:
        return _RELATED_MEMO[mk]
    hit = False
    try:
        hit = bool(gradebook._is_score_rec(rkey, rec))
    except Exception:  # noqa: BLE001
        hit = False
    if not hit:
        text = str(rec.get("text") or "")[:30000]
        hit = bool(sid and _SCORE_WORDS.search(text)
                   and re.search(rf"(?<![A-Za-z0-9]){re.escape(sid)}(?![A-Za-z0-9])", text, re.I))
    return _memo_put(_RELATED_MEMO, mk, hit)


def _components(store, cid) -> list[str]:
    cfg = (store.data.get("grades") or {}).get(str(cid)) or {}
    return [str(c.get("name")) for c in cfg.get("components") or [] if isinstance(c, dict) and c.get("name")]


def _course_name(store, cid) -> str | None:
    from .core import short_course_name
    for c in (store.data.get("cache") or {}).get("courses") or []:
        if str(c.get("id")) == str(cid):
            return short_course_name(c.get("name")) or None
    return None


def targets(store, course_id=None, key=None, names=None) -> list[dict]:
    """AI로 찾아볼 파일: 학번이 있고, 제외하지 않은 과목이고, 규칙이 내 점수를 못 냈고, 점수 관련이고, 내 줄이 있거나
    (가린 학번이 여러 줄과 맞아) 애매한 것. course_id·key를 주면 그 과목·그 파일만(key를 고르면 점수 낱말이 없어도, 내 줄이 없어도
    대상 — 결과에 이유가 남는다). 최근 글 먼저. → [{"key", "course_id", "filename", "posted_at", "hash", "why"}]"""
    sid = _sid(store)
    if not sid:
        return []
    texts = store.data.get("pdf_texts") or {}
    hidden = hidden_courses(store.data)
    pairs = [(key, texts.get(key))] if key is not None else list(texts.items())
    out = []
    for rkey, rec in pairs:
        if not isinstance(rec, dict) or rec.get("course_id") in (None, ""):
            continue
        cid = str(rec.get("course_id"))
        if cid in hidden or (course_id is not None and cid != str(course_id)):
            continue
        if rules_found(rkey, rec, sid):
            continue
        if key is None and not score_related(store, rkey, rec, sid):
            continue
        h = source_hash(rec, sid)
        prep = prepare(rkey, rec, sid, names, h=h)
        if not prep["ok"] and prep["why"] != "ambiguous" and key is None:
            continue
        out.append({"key": rkey, "course_id": cid, "filename": rec.get("filename"), "posted_at": rec.get("posted_at"),
                    "hash": h, "why": prep["why"]})
    out.sort(key=lambda t: str(t.get("posted_at") or ""), reverse=True)
    return out


def needs(store, t: dict, explicit: bool = False) -> bool:
    """다시 물을지: 기억이 없거나, 글이 바뀌었거나(hash), 실패했고 RETRY_HOURS가 지났거나 사용자가 눌렀을 때."""
    e = (store.data.get("ai_scores") or {}).get(t["key"])
    if not isinstance(e, dict) or e.get("hash") != t["hash"]:
        return True
    if e.get("local") and explicit:   # AI에 보내지 않고 이유만 남긴 것(내 줄 없음·애매함) — 사용자가 누르면 다시 본다(이름 등)
        return True
    if e.get("error"):
        if explicit:
            return True
        at = parse_iso(e.get("at"))
        return at is None or now_local() - at >= timedelta(hours=RETRY_HOURS)
    return False


def _norm_label(s) -> str:
    return re.sub(r"[\W_]+", "", str(s or "").lower())


def _keyed(rkey: str, results: list[dict], previous: dict | None) -> list[dict]:
    """결과마다 후보 key "ai:<출처>#<label>". 전에 물은 같은 라벨이면 그 key를 이어 쓴다(사용자 연결·'넣지 않음'이 남게)."""
    old = {_norm_label(r.get("label")): r.get("key") for r in (previous or {}).get("results") or []
           if isinstance(r, dict) and r.get("key")}
    used, out = set(), []
    for r in results:
        k = old.get(_norm_label(r.get("label"))) or f"ai:{rkey}#{r.get('label')}"
        n = 2
        while k in used:
            k = f"ai:{rkey}#{r.get('label')} ({n})"
            n += 1
        used.add(k)
        out.append({**r, "key": k})
    return out


def _display(s: str, sid: str | None) -> str:
    """화면에 보일 근거: 자리 표시 대신 가린 학번(2026****02)·○○○, 줄 머리 표시는 뗀다(계약 13-3 — 화면에 [이메일] 같은 것이 없게)."""
    s = str(s or "")
    for p in (MY_ROW, OTHER_ROW):
        s = s.replace(p.strip(), "")
    s = s.replace(ME, gradebook.mask_student_id(sid) or "내 학번").replace(NAME, "○○○").replace(OTHER_ID, "학번")
    return agent_cli.unplace(re.sub(r"\s+", " ", s)).strip()


def lookup(store, session, course_id=None, key=None, names=None, limit: int = AUTO_PER_SYNC, explicit: bool = False,
           by: str = "auto", on_step=None) -> dict:
    """대상 파일을 AI(session — agent_cli.AiSession)로 읽어 store.data["ai_scores"]에 남긴다. 저장은 부르는 쪽이.
    내 줄이 없거나 애매한 파일은 AI에 보내지 않고 이유만 남긴다(호출 수에 안 셈). limit = 실제로 AI에 묻는 파일 수.
    → {"total", "asked", "found", "none", "failed", "local", "keys"(남긴 출처), "errors"[{key, error}], "error"(멈춘 이유)}"""
    out = {"total": 0, "asked": 0, "found": 0, "none": 0, "failed": 0, "local": 0, "keys": [], "errors": [], "error": None}
    sid = _sid(store)
    if not sid:
        out["error"] = "학번을 넣어야 AI로 내 줄을 찾을 수 있어요(설정 → 성적)"
        return out
    todo = [t for t in targets(store, course_id, key, names) if needs(store, t, explicit)]
    out["total"] = len(todo)
    texts = store.data.get("pdf_texts") or {}
    box = store.data.setdefault("ai_scores", {})
    for t in todo:
        if out["asked"] >= limit:
            break
        rec = texts.get(t["key"]) or {}
        comps = _components(store, t["course_id"])
        prep = prepare(t["key"], rec, sid, names, comps, _course_name(store, t["course_id"]), h=t["hash"])
        base = {"hash": t["hash"], "at": iso(now_local()), "course_id": t["course_id"], "filename": rec.get("filename"),
                "by": by}
        if not prep["ok"]:
            box[t["key"]] = {**base, "results": [], "model": None, "error": None, "note": prep["reason"], "attempts": 0,
                             "local": True}
            out["local"] += 1
            out["none"] += 1
            out["keys"].append(t["key"])
            if on_step:
                on_step(t, box[t["key"]])
            continue
        why = session.blocked() if session is not None else "AI가 꺼져 있어요(설정 → AI 도움에서 켤 수 있어요)"
        if why:
            out["error"] = why
            break
        res = session.run(TASK, {"prepared": prep, "components": comps, "names": names})
        if res.attempts == 0 and not res.ok:
            out["error"] = res.error   # 한도·꺼짐·CLI 없음: 기억하지 않는다(다음에 다시)
            break
        out["asked"] += 1
        data = res.data if res.ok and isinstance(res.data, dict) else {}
        results = _keyed(t["key"], list(data.get("scores") or []), box.get(t["key"]))
        entry = {**base, "results": results, "model": res.model, "error": None if res.ok else res.error,
                 "note": data.get("note"), "attempts": res.attempts, "local": False}
        box[t["key"]] = entry
        out["keys"].append(t["key"])
        if not res.ok:
            out["failed"] += 1
            out["errors"].append({"key": t["key"], "error": res.error})
        elif results:
            out["found"] += 1
            _remember_links(store, t["course_id"], results, sid)
        else:
            out["none"] += 1
        if on_step:
            on_step(t, entry)
    return out


def _remember_links(store, cid: str, results: list[dict], sid: str) -> None:
    """AI가 고른 평가 항목 → grade_links_ai(이미 있으면 덮지 않음 — 사용자 연결은 grade_links라 늘 이긴다)."""
    slot = None
    for r in results:
        if not r.get("component"):
            continue
        slot = slot if slot is not None else store.data.setdefault("grade_links_ai", {}).setdefault(str(cid), {})
        if r["key"] in slot:
            continue
        ev = _display(r.get("evidence"), sid)[:120]
        slot[r["key"]] = {"component": r["component"], "evidence": ev, "reason": f"AI가 연결함 — 근거: \"{ev}\"",
                          "at": iso(now_local()), "error": None}


def after_sync(store, session, names=None, limit: int = AUTO_PER_SYNC) -> dict:
    """동기화 뒤 자동 단계(core._after_sync가 같은 AiSession으로): AI가 켜져 있으면 대상 파일 limit개까지(하루 한도 안).
    → lookup 결과(저장은 부르는 쪽 — core가 ai_scores·grade_links_ai 칸을 합쳐 저장)."""
    if session is None or not getattr(session, "enabled", False) or not _sid(store):
        return {"total": 0, "asked": 0, "found": 0, "none": 0, "failed": 0, "local": 0, "keys": [], "errors": [],
                "error": None}
    return lookup(store, session, names=names, limit=limit, by="auto")


# ================================================================ 성적 후보(gradebook.register_candidates)

def _sentence(r: dict) -> str:
    s = f"AI가 찾음: {r.get('label')} {_fmt(r.get('score'))}" + (f"/{_fmt(r['max_score'])}" if r.get("max_score") else "점")
    if r.get("mean") is not None:
        s += f" · 평균 {float(r['mean']):.1f}"
    if r.get("sd") is not None:
        s += f" · 표준편차 {float(r['sd']):.1f}"
    if r.get("n") and r.get("rank"):
        s += f" · {r['n']}명 중 {r['rank']}등"
    return s


def candidates_provider(store, names: dict | None = None) -> list[dict]:
    """AI가 찾은 점수 → 성적 후보(다른 후보와 같은 모양). 학번·출처 글이 그대로이고, 규칙이 그 파일을 아직 못 읽을 때만."""
    sid = _sid(store)
    if not sid:
        return []
    from .core import short_course_name
    texts = store.data.get("pdf_texts") or {}
    names = names or {}
    out = []
    for src, e in sorted((store.data.get("ai_scores") or {}).items()):
        if not isinstance(e, dict) or e.get("error") or not e.get("results"):
            continue
        rec = texts.get(src)
        if not isinstance(rec, dict) or e.get("hash") != source_hash(rec, sid) or rules_found(src, rec, sid):
            continue
        cid = str(rec.get("course_id")) if rec.get("course_id") is not None else None
        fn = rec.get("filename") or src
        for r in e["results"]:
            if not isinstance(r, dict) or r.get("score") is None or not r.get("key"):
                continue
            ev = _display(r.get("evidence"), sid)
            reason = f"AI가 '{fn}'에서 찾음 — 근거: \"{ev[:150]}\""
            notes = ["AI가 찾은 점수예요 — 맞지 않으면 '넣지 않음'으로 빼 주세요"] + [str(x) for x in r.get("notes") or []][:3]
            out.append({"course_id": cid, "course_short": short_course_name(names.get(cid)) if cid else None,
                        "source_type": SOURCE_TYPE, "source_title": fn, "posted_at": iso(parse_iso(rec.get("posted_at"))),
                        "url": None, "label": r.get("label"), "score": r.get("score"), "max_score": r.get("max_score"),
                        "mean": r.get("mean"), "sd": r.get("sd"), "high": r.get("high"), "low": r.get("low"),
                        "n": r.get("n"), "rank": r.get("rank"), "sentence": _sentence(r), "notes": notes,
                        "key": r["key"], "file_kind": "sheet" if _is_sheet(rec) else "pdf", "decided_by": "ai",
                        "reason": reason[:300], "evidence": ev[:300], "confidence": r.get("confidence"),
                        "ai_source": src, "ai_at": e.get("at"), "ai_model": e.get("model")})
    return out


gradebook.register_candidates(candidates_provider)


# ================================================================ 진단 줄·상태

def entry_status(store, rkey: str, rec: dict, ai_on: bool | None = None) -> dict:
    """성적 파일 진단 한 줄의 "ai" 칸: {"status": found|none|error|pending|None, "results", "at", "note"}.
    pending = AI가 켜져 있고 아직 묻지 않은(또는 글이 바뀐) 대상 파일."""
    sid = _sid(store)
    if ai_on is None:
        ai_on = bool((store.data.get("settings") or {}).get("ai_cli"))
    out = {"status": None, "results": [], "at": None, "note": None}
    if not sid or not isinstance(rec, dict):
        return out
    e = (store.data.get("ai_scores") or {}).get(rkey)
    h = source_hash(rec, sid)
    if isinstance(e, dict) and e.get("hash") == h:
        res = [{"key": r.get("key"), "label": r.get("label"), "score": r.get("score"), "max_score": r.get("max_score"),
                "mean": r.get("mean"), "sd": r.get("sd"), "confidence": r.get("confidence"),
                "evidence": _display(r.get("evidence"), sid)[:200]} for r in e.get("results") or [] if isinstance(r, dict)]
        status = "error" if e.get("error") else ("found" if res else "none")
        return {"status": status, "results": res, "at": e.get("at"),
                "note": str(e.get("error") or e.get("note") or "")[:200] or None}
    if ai_on and rec.get("course_id") not in (None, "") and str(rec.get("course_id")) not in hidden_courses(store.data) \
            and not rules_found(rkey, rec, sid) and score_related(store, rkey, rec, sid):
        prep = prepare(rkey, rec, sid, h=h)
        if prep["ok"] or prep["why"] == "ambiguous":
            out["status"] = "pending"
    return out


_JOBS: dict = {}
_jobs_lock = threading.Lock()


def _pkey(store) -> str:
    try:
        return str(Path(store.path).resolve())
    except (OSError, RuntimeError, AttributeError, TypeError):
        return str(id(store))


def job_of(store) -> dict | None:
    """[AI로 성적 찾기] 백그라운드 작업(이 프로세스 메모리에만 — features/aiscores가 넣는다)."""
    with _jobs_lock:
        j = _JOBS.get(_pkey(store))
        return dict(j) if j else None


def set_job(store, job: dict | None) -> None:
    with _jobs_lock:
        if job is None:
            _JOBS.pop(_pkey(store), None)
        else:
            _JOBS[_pkey(store)] = job


def update_job(store, **kw) -> None:
    with _jobs_lock:
        j = _JOBS.get(_pkey(store))
        if j is not None:
            j.update(kw)


def lookup_state(store) -> dict:
    """state.grades.ai_lookup: {"enabled"(AI 켜짐), "student_id_set", "running", "job"(마지막 [AI로 찾기] 작업 | None),
    "last_at"(마지막으로 물은 시각), "done"(AI가 본 파일 수), "found"(점수를 찾은 파일 수), "pending"(아직 묻지 않은 대상 수),
    "errors"[{key, file, course_id, error, at}](실패한 파일, 최근 5), "per_sync"(동기화 뒤 자동으로 묻는 수)}."""
    st = store.data.get("settings") or {}
    entries = {k: e for k, e in (store.data.get("ai_scores") or {}).items() if isinstance(e, dict)}
    job = job_of(store)
    errors = sorted([{"key": k, "file": e.get("filename"), "course_id": e.get("course_id"),
                      "error": str(e.get("error"))[:200], "at": e.get("at")} for k, e in entries.items() if e.get("error")],
                    key=lambda x: str(x.get("at") or ""), reverse=True)[:5]
    ats = [str(e.get("at")) for e in entries.values() if e.get("at")]
    try:
        pending = sum(1 for t in targets(store) if needs(store, t)) if st.get("ai_cli") else 0
    except Exception:  # noqa: BLE001 — 상태 계산이 성적 화면을 깨지 않게
        pending = 0
    return {"enabled": bool(st.get("ai_cli")), "student_id_set": bool(_sid(store)),
            "running": bool(job and job.get("status") == "running"), "job": job,
            "last_at": max(ats) if ats else None, "done": len(entries),
            "found": sum(1 for e in entries.values() if e.get("results") and not e.get("error")),
            "pending": pending, "errors": errors, "per_sync": AUTO_PER_SYNC}
