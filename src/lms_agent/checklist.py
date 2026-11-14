"""과제 체크리스트(계약 22-6): 과제 설명(과 거기 링크된 PDF, 24-3)·근거 사슬의 글·같은 과목 공지에서 제출 요건 문장을 규칙으로 찾는다.

특정 과목·문장에 맞추지 않는다. 문장마다 '무엇에 관한 요건인지'를 말하는 낱말 묶음(파일 형식·파일 이름·분량·
포함할 것·제출처·팀/개인·지각/감점/표절 규정)과 '학생이 하는 일'을 말하는 낱말(제출·첨부·포함·보내·submit·include …)로
고른다. 강의 자료를 나눠 준다는 문장('첨부한 슬라이드를 참고')·문의처 문장('질문은 메일로')·다른 과제를 말하는 문장
('Lab0, Lab1 점수 공개')은 고르지 않는다. 날짜·시각은 체크 문장(text)에 넣지 않는다(마감은 따로 있다) — 인용(quote)은 원문 그대로.

저장(store.data["checklists"][item_id]):
  {"done": {칸 id: bool}, "added": [{"id": "u-…", "text", "done", "at"}], "removed": [규칙 칸 id], "updated_at"}
규칙 칸 id("r-…")는 (종류, 체크 문장)에서 만들어 다시 동기화해도 같다 — 사용자의 체크·추가·지우기가 남는다.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date

from .extract import (RX_T_COLON, RX_T_DOT_AP, RX_T_EN, RX_T_KO, RX_T_WORD, find_dates, html_to_text,
                      same_task, split_sentences, title_tokens)
from .timeutil import iso, now_local, parse_iso

QUOTE_MAX = 300      # 인용 길이
LINE_MAX = 100       # 규정 문장을 체크 문장으로 줄일 때
TEXT_MAX = 200       # 사용자가 더하는 칸 글자 수
USER_MAX = 30        # 체크리스트 하나에 사용자가 더하는 칸 수
RULES_MAX = 12       # 규칙이 찾는 칸 수(항목 하나)
VISIBLE = ("review", "scheduled")
KIND_ORDER = ("where", "format", "name", "length", "include", "team", "policy")
OPS = ("toggle", "add", "remove", "refresh")
_ID = re.compile(r"[ru]-[0-9a-f]{4,32}")


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.I)


# 이메일 주소(ASCII만 — 'ta@example.ac.kr로'의 조사 '로'가 주소에 붙지 않게; extract.EMAIL_ADDR은 \w라 한글이 붙는다)
ADDR = re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


def clean_address(s) -> str | None:
    """'thermo.box@example.test로' → 'thermo.box@example.test'. 주소가 아니면 None."""
    m = ADDR.search(str(s or ""))
    return m.group(0) if m else None


# ---------------------------------------------------------------- 문장 성격

# 학생이 하는 일(제출·업로드·보내기·첨부·저장·변환·압축)
SUBMIT = _rx(r"제출|내\s*(?:주세요|주십시오|주시기|주시면|세요|시오|십시오|야\s*(?:합|하|함)|면\s*됩)|보내|올려\s*(?:주|야)|"
             r"올리(?:세요|시오|고|면|기)|업로드|첨부(?:하|해)|첨부$|저장(?:하|해)|변환|압축(?:하|해)|넣어|넣으|"
             r"(?<![a-z])(?:submit\w*|turn(?:ed|ing)?\s+in|hand(?:ed|ing)?\s+in|upload\w*|send|sent|"
             r"e-?mail\s+(?:it|them|your|the|a|to)|save[ds]?|convert\w*|compress\w*|zip\s+(?:up|your|all|the))(?![a-z])")
SUBMIT_STRONG = _rx(r"제출|업로드|(?<![a-z])(?:submit\w*|upload\w*|turn(?:ed|ing)?\s+in|hand(?:ed|ing)?\s+in)(?![a-z])")
# 강의 자료를 나눠 주는 문장(학생 요건이 아님)
MATERIAL = _rx(r"강의\s*(?:자료|슬라이드|노트)|수업\s*자료|첨부(?:된|했|하였|드립)|첨부한(?!\s*(?:뒤|후|다음))|배포|"
               r"공유(?:합니다|했|드립)|올렸|올려\s*(?:두|놓)|"
               r"(?<![a-z])(?:lecture\s+(?:notes?|slides?|materials?)|(?:i|we)\s+(?:have\s+)?attached|"
               r"(?:is|are)\s+attached|(?:has|have)\s+been\s+(?:attached|uploaded|posted|shared))(?![a-z])")
REFERENCE = _rx(r"참고|참조|(?<![a-z])(?:see|refer)(?![a-z])")
CONTACT = _rx(r"문의|질문|연락|상담|(?<![a-z])(?:contact|questions?|inquir\w*|reach\s+(?:me|us|out)|office\s+hours?)(?![a-z])")
DELIVERABLE = _rx(r"보고서|레포트|리포트|과제|숙제|결과물|파일|프로젝트|실습|(?<![a-z])(?:reports?|assignments?|homeworks?|hws?|"
                  r"files?|projects?|labs?|work|write-?ups?|essays?|papers?)(?![a-z])")


# ---------------------------------------------------------------- 파일 형식

_FMT_EXT = [("PDF", r"pdf"), ("ZIP", r"zip|7z|rar|tgz|tar(?:\.gz)?"), ("DOCX", r"docx?"), ("HWP", r"hwpx?"),
            ("PPTX", r"pptx?"), ("XLSX", r"xlsx?"), ("IPYNB", r"ipynb")]
_FMT_RX = [(f, re.compile(r"(?i)(?<![a-z0-9])(?:" + p + r")(?![a-z])")) for f, p in _FMT_EXT]
_FMT_WORDS = [("DOCX", _rx(r"(?<![가-힣])워드\s*(?:파일|문서)|(?<![a-z])(?:ms\s*-?\s*word|word\s+(?:file|document|format))(?![a-z])")),
              ("HWP", _rx(r"(?<![가-힣])한글\s*(?:파일|문서)|아래아\s*한글")),
              ("PPTX", _rx(r"파워\s*포인트|(?<![a-z])power\s*point(?![a-z])")),
              ("XLSX", _rx(r"엑셀\s*(?:파일|문서)?|(?<![a-z])excel(?![a-z])")),
              ("IPYNB", _rx(r"주피터\s*노트북|(?<![a-z])jupyter\s+notebooks?(?![a-z])"))]
_ZIP_KO = _rx(r"압축(?:하|해|한|된|\s*파일)|(?<![a-z])(?:zipped|compressed)(?![a-z])")
FMT_LABEL = {"PDF": "PDF", "ZIP": "ZIP(압축)", "DOCX": "워드(DOCX)", "HWP": "한글(HWP)", "PPTX": "파워포인트(PPTX)",
             "XLSX": "엑셀(XLSX)", "IPYNB": "주피터 노트북(IPYNB)"}
_AFTER_KO = re.compile(r"^\s*(?:파일|문서)?\s*(?:하나(?:의)?|한\s*개|1\s*개|한\s*부|1\s*부|한\s*파일|하나의\s*파일|단일\s*파일)?\s*"
                       r"(?:로|으로|만|형식|형태|포맷|변환|첨부|제출|업로드|저장|파일로|문서로)|"
                       r"^\s*(?:파일|문서)?\s*(?:외의?|이외의?|가\s*아닌|이\s*아닌)\s*(?:\S+\s*){0,2}?"
                       r"(?:받지|제출하지|인정하지|불가|안\s*됩)")
_AFTER_EN = _rx(r"^\s*(?:files?|format|documents?|only|version|copy|attachments?)(?![a-z])")
_BEFORE_EN = _rx(r"(?<![a-z])(?:as|in|into|to|via|one|single|only|an?)\s+(?:(?:a|an|one|single)\s+)?\.?$")
_BEFORE_FIELD = _rx(r"(?:형식|포맷|파일\s*형식|제출\s*형식|(?<![a-z])format|file\s*type)\s*[:：]?\s*(?:은|는)?\s*$")
_NEG_AFTER = _rx(r"^\s*(?:파일|문서|형식)?\s*(?:은|는|로는|으로는|이나|도)?\s*(?:받지|제출하지|인정하지|인정되지|불가|"
                 r"안\s*됩|안\s*받|허용(?:하지|되지)|금지|사절)|"
                 r"^\s*(?:files?|formats?|documents?)?\s*(?:is|are|will)\s+not\s+(?:be\s+)?(?:accepted|allowed|graded)")
_NEG_BEFORE = _rx(r"(?<![a-z])(?:no|not|never|don't|do\s+not)\s+(?:\w+\s+){0,3}\.?$")


def _formats(s: str, submit: bool, templates: list[str]) -> list[tuple[str, str]]:
    out, seen = [], set()
    tpl_spans = [(m.start(), m.end()) for m in _TPL.finditer(s) if any(t in m.group(0) for t in templates)]

    def add(fmt, neg):
        if fmt not in seen:
            seen.add(fmt)
            out.append(("format", f"{FMT_LABEL[fmt]} 파일은 안 됨" if neg else f"{FMT_LABEL[fmt]} 파일로 제출"))

    for fmt, rx in _FMT_RX:
        for m in rx.finditer(s):
            a, b = m.start(), m.end()
            after, before = s[b:b + 24], s[max(0, a - 32):a]
            in_tpl = any(x <= a < y for x, y in tpl_spans)
            if _NEG_AFTER.match(after) or _NEG_BEFORE.search(before):
                add(fmt, True)
                break
            ext = a > 0 and s[a - 1] == "." and a > 1 and (s[a - 2].isalnum() or s[a - 2] in "_]")
            ctx = bool(_AFTER_KO.match(after) or _AFTER_EN.match(after) or _BEFORE_EN.search(before)
                       or _BEFORE_FIELD.search(before))
            if in_tpl or (ctx and (submit or not ext)):
                add(fmt, False)
                break
    for fmt, rx in _FMT_WORDS:
        m = rx.search(s)
        if m and (submit or _AFTER_KO.match(s[m.end():m.end() + 24])):
            neg = bool(_NEG_AFTER.match(s[m.end():m.end() + 24]) or _NEG_BEFORE.search(s[max(0, m.start() - 32):m.start()]))
            add(fmt, neg)
    if "ZIP" not in seen and _ZIP_KO.search(s) and submit:
        add("ZIP", False)
    return out


# ---------------------------------------------------------------- 파일 이름·메일 제목

FILE_KW = _rx(r"파일\s*(?:명|이름)|파일이름|(?<![a-z])(?:file\s*-?\s*names?|filenames?|naming|"
              r"name\s+(?:your|the|each)\s+(?:\w+\s+){0,2}?files?|named)(?![a-z])")
SUBJECT_KW = _rx(r"(?:메일|이메일|e-?mail)\s*(?:의\s*)?제목|제목\s*(?:은|을|에|란|:)|(?<![a-z])(?:subject(?:\s+line)?|"
                 r"title\s+(?:your|the)\s+e-?mails?)(?![a-z])")
PLACEHOLDER = _rx(r"이름|성명|학번|학과|분반|팀\s*명|조\s*명|조\s*번호|번호|"
                  r"(?<![a-z])(?:name|names|studentid|student_?id|sid|id|student\s*number|team|group|"
                  r"lab\s*n|hw\s*n|[a-z]+n|n|x+)(?![a-z])")
_TPL = re.compile(r"(?:\[[^\]\s]{1,20}\]\s*)?[^\s,;:\"'“”‘’「」<>()（）]*_[^\s,;:\"'“”‘’「」<>()（）]+")
_TPL_TAIL = re.compile(r"(?:형식으로|형태로|으로|처럼|로|을|를|은|는)$")
_QUOTED = re.compile(r"[\"'“‘「]([^\"'”’」\n]{3,60})[\"'”’」]")


def _template_tokens(s: str) -> list[str]:
    """밑줄로 이은 이름 틀('Report_LabN_이름_학번.pdf', '[ASIC] LAB4_이름_학번'). 주소·경로·코드 이름은 뺀다."""
    out = []
    for m in _TPL.finditer(s):
        tok = m.group(0).rstrip(".,!?;")
        if "@" in tok or "/" in tok or "\\" in tok or "://" in tok or len(tok) > 80:
            continue
        cut = _TPL_TAIL.search(tok)
        if cut and len(tok) - len(cut.group(0)) >= 3:
            tok = tok[:cut.start()]
        tok = tok.rstrip(".,!?;")
        core_part = re.sub(r"^\[[^\]]*\]\s*", "", tok)
        parts = [p for p in re.split(r"_", re.sub(r"\.[A-Za-z0-9]{1,5}$", "", core_part)) if p]
        if len(parts) >= 2 and (PLACEHOLDER.search(" ".join(parts).replace("_", " ")) or FILE_KW.search(s)
                                or SUBJECT_KW.search(s)):
            out.append(tok)
    return out


def _names(s: str, templates: list[str]) -> list[tuple[str, str]]:
    file_kw, subj_kw = FILE_KW.search(s), SUBJECT_KW.search(s)
    label = "메일 제목" if subj_kw and not file_kw else "파일 이름"
    if templates:
        return [("name", f"{label}: {t}") for t in templates[:2]]
    kw = file_kw or subj_kw
    if not kw:
        return []
    if subj_kw and not file_kw and not re.search(r"(?i)메일|e-?mail|subject", s):
        return []          # '제목은 …'만으로는 메일 제목인지 모른다(글 제목·과제 제목일 수 있음)
    q = _QUOTED.search(s[kw.start():])
    if q:
        return [("name", f"{label}: {q.group(1).strip()}")]
    if PLACEHOLDER.search(s[kw.end():]):
        return [("name", f"{label} 규칙: {_line(s)}")]
    return []


# ---------------------------------------------------------------- 분량

_NUM = r"(?:\d[\d,]*(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty)"
_UNIT = r"(?:쪽|페이지|매|장|자|글자|단어|(?<![a-z])words?(?![a-z])|(?<![a-z])pages?(?![a-z])|(?<![a-z])slides?(?![a-z]))"
_LEN = [
    _rx(r"분량\s*(?:은|는|:|：)?\s*(?:A4\s*(?:용지\s*)?(?:기준\s*)?)?\d[\d,]*\s*(?:[~\-–]\s*\d[\d,]*\s*)?" + _UNIT +
        r"(?:\s*(?:이내|이하|이상|내외|미만|안팎|정도|초과))?"),
    _rx(r"(?:A4\s*(?:용지\s*)?(?:기준\s*)?)?(?<![\d.])\d[\d,]*\s*(?:[~\-–]\s*\d[\d,]*\s*)?" + _UNIT +
        r"\s*(?:이내|이하|이상|내외|미만|초과|안팎|정도|분량|(?:을|를)?\s*넘지)"),
    _rx(r"A4\s*(?:용지\s*)?\d+\s*(?:[~\-–]\s*\d+\s*)?(?:장|쪽|매|페이지)"),
    _rx(r"(?<![a-z])(?:no\s+more\s+than|no\s+longer\s+than|not\s+(?:to\s+)?exceed(?:ing)?|at\s+most|up\s+to|"
        r"a\s+maximum\s+of|maximum(?:\s+of)?|max\.?|at\s+least|a\s+minimum\s+of|minimum(?:\s+of)?|min\.?|within|under|"
        r"less\s+than|fewer\s+than|limited\s+to|between)\s+" + _NUM + r"(?:\s*(?:and|-|–|to)\s*" + _NUM + r")?\s*"
        r"(?:-\s*)?(?:pages?|words?|slides?|characters?)(?![a-z])"),
    _rx(r"(?<![a-z\d])" + _NUM + r"(?:\s*[-–]\s*" + _NUM + r")?[\s-]*(?:pages?|words?)\s+(?:or\s+(?:less|fewer|more)|"
        r"max(?:imum)?|limit|long|in\s+length)(?![a-z])"),
    _rx(r"(?<![a-z])(?:page|word)\s+(?:limit|count)\s*(?:is|:|of)?\s*" + _NUM + r"(?:\s*(?:pages?|words?))?"),
    _rx(r"(?<![a-z\d])" + _NUM + r"-page\s+(?:report|essay|paper|summary|write-?up|document)s?(?![a-z])"),
]


def _lengths(s: str) -> list[tuple[str, str]]:
    best = None
    for rx in _LEN:
        for m in rx.finditer(s):
            if best is None or len(m.group(0)) > len(best):
                best = m.group(0)
    if not best:
        return []
    phrase = re.sub(r"^\s*분량\s*(?:은|는|:|：)?\s*", "", best)
    phrase = re.sub(r"(?:로|으로)$", "", re.sub(r"\s+", " ", phrase)).strip()
    return [("length", f"분량: {phrase}")]


# ---------------------------------------------------------------- 포함할 것

_NOUNS = [
    ("코드", r"소스\s*코드|코드|테스트\s*벤치|(?<![a-z])(?:source\s+code|code|codes|source\s+files?|testbench(?:es)?)(?![a-z])"),
    ("스크린샷", r"스크린\s*샷|화면\s*(?:캡처|캡쳐|사진)|캡처|캡쳐|(?<![a-z])(?:screen\s*-?shots?|screen\s+captures?|captures?)(?![a-z])"),
    ("파형", r"파형|(?<![a-z])(?:waveforms?|timing\s+diagrams?)(?![a-z])"),
    ("결과", r"(?:실행|시뮬레이션|실험|측정)\s*결과|결과\s*(?:값|그래프|표)|(?<![a-z])(?:results?|outputs?)(?![a-z])"),
    ("그래프·표", r"그래프|(?<![a-z])(?:graphs?|plots?|charts?|tables?)(?![a-z])"),
    ("사진", r"사진|(?<![a-z])(?:photos?|photographs?|pictures?|images?)(?![a-z])"),
    ("보고서", r"보고서|레포트|리포트|(?<![a-z])(?:reports?|write\s*-?\s*ups?)(?![a-z])"),
    ("참고문헌", r"참고\s*문헌|(?<![a-z])(?:references?|citations?|bibliography)(?![a-z])"),
    ("표지", r"표지|(?<![a-z])(?:cover\s+(?:page|sheet)|title\s+page)(?![a-z])"),
    ("이름·학번", r"(?:이름|성명)\s*(?:과|와|,|및|/)?\s*학번|학번\s*(?:과|와|,|및|/)?\s*(?:이름|성명)|"
                 r"(?<![a-z])(?:your\s+)?(?:full\s+)?name\s+and\s+(?:student\s+)?(?:id|number)(?![a-z])"),
    ("서명", r"서명|(?<![a-z])signatures?(?![a-z])"),
    ("README", r"(?<![a-z])readme(?![a-z])"),
]
_NOUN_RX = [(label, _rx(p)) for label, p in _NOUNS]
INCLUDE = _rx(r"포함|넣어|넣고|넣을|넣으|담아|담고|첨부(?:하|해)|함께|(?<!과 )(?<!와 )(?<!과)(?<!와)같이|"
              r"들어가(?:야|도록|게)|기재|적어|적고|적으|명시|"
              r"(?<![a-z])(?:includ\w*|attach\s+(?:your|the|all|a|an)|along\s+with|together\s+with|contain\w*|"
              r"consist\w*\s+of|should\s+have|must\s+have)(?![a-z])")
_CONTAINER_AFTER = re.compile(r"^\s*(?:에|에는|안에|속에|내에|의\s*(?:끝|앞|마지막|첫|맨))")
_CONTAINER_BEFORE = _rx(r"(?<![a-z])(?:in|into|within|to|inside)\s+(?:the|your|each|a|an)\s+(?:\w+\s+)?$")
_NOT_INCLUDE = _rx(r"않아도|안\s*해도|필요\s*없|불필요|제외|빼고|(?<![a-z])(?:not\s+(?:need|required|necessary)|no\s+need|"
                   r"do\s+not\s+(?:include|submit|attach)|don't\s+(?:include|submit|attach))(?![a-z])")


def _includes(s: str, submit: bool, title: str | None) -> list[tuple[str, str]]:
    verb = bool(INCLUDE.search(s))
    if not verb and not submit:
        return []
    taken: list[tuple[int, int]] = []
    labels = []
    for label, rx in _NOUN_RX:
        for m in rx.finditer(s):
            a, b = m.start(), m.end()
            if any(x < b and a < y for x, y in taken):
                continue
            if _CONTAINER_AFTER.match(s[b:b + 8]) or _CONTAINER_BEFORE.search(s[max(0, a - 30):a]):
                continue
            if title and rx.search(title):
                continue          # 과제 자체('LAB4 보고서'의 보고서)
            if label == "이름·학번" and (FILE_KW.search(s) or SUBJECT_KW.search(s)):
                continue          # 파일 이름·메일 제목에 넣는 것
            taken.append((a, b))
            labels.append((a, label))
            break
    labels = [lb for _, lb in sorted(labels)]       # 문장에 나온 순서
    if not labels or (not verb and len(labels) < 2):
        return []
    neg = bool(_NOT_INCLUDE.search(s))
    return [("include", f"넣지 않음: {lb}" if neg else f"{lb} 포함") for lb in labels]


# ---------------------------------------------------------------- 제출처

WHERE_EMAIL = _rx(r"(?:이메일|메일|e-?mail)\s*(?:주소)?\s*(?:로|으로|을\s*통해|를\s*통해)|"
                  r"(?<![a-z])(?:via|by|through|over|per)\s+e-?mail(?![a-z])|(?<![a-z])e-?mail\s+(?:it|them|your|the|a\s+copy)(?![a-z])")
LMS_WORDS = _rx(r"canvas|캔버스|(?<![a-z])(?:lms|e-?class|blackboard|moodle|learningx)(?![a-z])|이클래스|블랙보드|"
                r"과제\s*함|과제\s*게시판|학습\s*관리\s*시스템")
_LMS_NEG = re.compile(r"(?i)^\s*(?:\S+\s*)?(?:이|가)?\s*아니(?:라|고)|^\s*(?:\S+\s*)?말고|^\s*(?:으로|로)는\s*(?:받지|안)")
_LMS_NEG_BEFORE = _rx(r"(?:not|instead\s+of)\s+(?:(?:via|on|through|to|in|into)\s+)?(?:the\s+)?$")
LMS_ACT = _rx(r"제출|업로드|올려|올리|(?<![a-z])(?:submit\w*|upload\w*|post|turn(?:ed)?\s+in)(?![a-z])")
PAPER = _rx(r"출력(?:해|하여|한|물|본)|프린트(?:해|하여|한|물|본)?|인쇄(?:해|하여|한|물|본)|하드\s*카피|종이(?:로|에|\s*보고서)|"
            r"(?<![a-z])(?:hard\s*-?\s*cop(?:y|ies)|printed\s+(?:copy|copies|version|report)|"
            r"print(?:ed)?\s+(?:out|it|them|your)|paper\s+cop(?:y|ies)|on\s+paper|in\s+paper)(?![a-z])")
PAPER_ACT = _rx(r"제출|내\s*(?:주|세요|야|시)|가져|넣|(?<![a-z])(?:submit\w*|bring|hand\w*|turn(?:ed)?\s+in|drop)(?![a-z])")
PLACE = _rx(r"(?:[가-힣A-Za-z0-9]+\s*(?:앞|옆|안)의?\s*)?(?:과제\s*)?(?:우편함|제출함|보관함|사물함)|조교실|과\s*사무실|학과\s*사무실|"
            r"행정실|교수\s*연구실|연구실(?=\s*(?:에|으로|로|앞에|옆에|안에)(?![가-힣]))|"
            r"(?<![a-z])(?:(?:(?:ta|instructor'?s?|professor'?s?|department)\s+office\s+)?(?:mail\s*box(?:es)?|"
            r"drop\s*-?\s*box|submission\s+box)|(?:ta|instructor'?s?|professor'?s?|department)\s+office)(?![a-z])")
PLACE_ACT = _rx(r"제출|내\s*(?:주|세요|야|시)|넣|두(?:세요|고|면)|(?<![a-z])(?:submit\w*|drop|leave|place|put|bring|"
                r"hand\w*|turn(?:ed)?\s+in)(?![a-z])")
INCLASS = _rx(r"수업\s*(?:시간|중|때|시작\s*전)|강의\s*시간|(?<![a-z])(?:in\s+class|at\s+the\s+(?:beginning|start|end)\s+of\s+"
              r"(?:the\s+)?(?:class|lecture)|during\s+(?:the\s+)?(?:class|lecture)|before\s+(?:the\s+)?(?:class|lecture))(?![a-z])")
INCLASS_ACT = _rx(r"제출|걷|내\s*(?:주|세요|야|시)|(?<![a-z])(?:submit\w*|hand\w*|turn(?:ed)?\s+in|collect\w*)(?![a-z])")


def _wheres(s: str, submit: bool) -> list[tuple[str, str]]:
    out = []
    contact_only = bool(CONTACT.search(s)) and not SUBMIT_STRONG.search(s) and not DELIVERABLE.search(s)
    addrs = [m.group(0) for m in ADDR.finditer(s)]
    if not contact_only:
        if addrs and (submit or WHERE_EMAIL.search(s)):
            out += [("where", f"이메일로 제출: {a}") for a in dict.fromkeys(addrs)]
        elif WHERE_EMAIL.search(s) and submit:
            out.append(("where", "이메일로 제출"))
    m = LMS_WORDS.search(s)
    if m and LMS_ACT.search(s) and not _LMS_NEG.match(s[m.end():m.end() + 16]) \
            and not _LMS_NEG_BEFORE.search(s[max(0, m.start() - 24):m.start()]):
        out.append(("where", "LMS(Canvas)에 제출"))
    if PAPER.search(s) and PAPER_ACT.search(s):
        out.append(("where", "출력해서 종이로 제출"))
    m = PLACE.search(s)
    if m and PLACE_ACT.search(s):
        place = re.sub(r"\s+", " ", m.group(0)).strip()
        out.append(("where", place + "에 직접 제출"))
    if INCLASS.search(s) and INCLASS_ACT.search(s) and not any(t.startswith("수업") for _, t in out):
        out.append(("where", "수업 시간에 직접 제출"))
    return out


# ---------------------------------------------------------------- 팀/개인

_WORD_NUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
TEAM_N = _rx(r"(\d+)\s*인\s*(?:1\s*)?(?:조|팀)|(\d+)\s*명\s*(?:이|씩)?\s*(?:한\s*|1\s*)?(?:조|팀)|"
             r"(?<![a-z])(?:teams?|groups?)\s+of\s+(\d+|two|three|four|five|six)(?![a-z])|"
             r"(?<![\d])(\d+)\s*-?\s*(?:person|member|people)\s+(?:teams?|groups?)")
TEAM = _rx(r"팀\s*(?:별|과제|프로젝트|으로|단위|당|원|장|활동|작업)|조별|조\s*(?:과제|단위|당|원|장|활동|작업)|"
           r"그룹\s*(?:별|과제|으로|단위|당|활동)|짝\s*(?:과제|활동|으로|을\s*지어)|"
           r"(?<![a-z])(?:team\s*(?:project|assignment|work|report|submission|members?)|"
           r"group\s*(?:project|assignment|work|report|submission|members?)|in\s+(?:teams|groups|pairs)|per\s+(?:team|group)|"
           r"pair\s*-?\s*(?:work|programming|project)|with\s+(?:a|your)\s+partner)(?![a-z])")
INDIV = _rx(r"개인\s*(?:과제|별|별로|으로|적으로|제출|작업|프로젝트|보고서)|개별\s*(?:과제|제출|작성|적으로\s*(?:제출|작성))|"
            r"각자\s*(?:작성|제출|해|하|풀)|혼자\s*(?:작성|해|하|풀)|"
            r"(?<![a-z])(?:individual\s+(?:assignment|project|work|report|submission|task|effort)|"
            r"(?:is|are)\s+(?:an\s+)?individual(?:\s+(?:assignment|work|task))?|own\s+work|"
            r"(?:submit|work|complete|do|write|solve)\w*\s+(?:\w+\s+){0,3}?(?:individually|on\s+your\s+own|by\s+yourself|"
            r"independently|alone))(?![a-z])")


def _teams(s: str) -> list[tuple[str, str]]:
    out = []
    m = TEAM_N.search(s)
    if m:
        n = next(g for g in m.groups() if g)
        n = _WORD_NUM.get(n.lower(), n)
        out.append(("team", f"{n}인 1조(팀) 과제"))
    elif TEAM.search(s):
        out.append(("team", "팀(조) 과제"))
    if INDIV.search(s):
        out.append(("team", "개인 과제(혼자 작성)"))
    return out


# ---------------------------------------------------------------- 지각·감점·표절 규정

LATE = _rx(r"지각\s*제출|늦게\s*(?:제출|내|낸|낼)|늦은\s*제출|기한\s*(?:후|이후|이\s*지난|을\s*넘|를\s*넘|초과|외|경과)|"
           r"마감\s*(?:후|이후|이\s*지난|을\s*넘|를\s*넘|시간\s*(?:이후|을\s*넘|후)|초과|경과)|연체|"
           r"(?<![a-z])(?:late|lateness|after\s+the\s+(?:deadline|due)|past\s+(?:the\s+)?(?:deadline|due)|overdue|"
           r"grace\s+period)(?![a-z])")
CONSEQUENCE = _rx(r"감점|0점|영점|받지\s*않|인정하지\s*않|인정되지\s*않|불가|안\s*받|받지\s*못|채점하지|반영하지|허용|%|점|"
                  r"(?<![a-z])(?:penalt\w*|deduct\w*|zero|no\s+credit|not\s+(?:be\s+)?(?:accepted|graded|allowed)|"
                  r"reduc\w*|lose|loss|accept\w*)(?![a-z])")
PLAGIARISM = _rx(r"표절|베끼|베낀|부정\s*행위|카피\s*킬|copy\s*kill|"
                 r"(?<![a-z])(?:plagiar\w*|cheat\w*|cop(?:y|ying|ied)\s+(?:from|others|another|someone|code|each)|"
                 r"academic\s+(?:integrity|dishonesty|misconduct)|(?:do|must)\s+not\s+share|no\s+collaboration|"
                 r"collaboration\s+is\s+not)(?![a-z])")
PENALTY = _rx(r"감점|0점\s*처리|(?<![a-z])(?:penalt\w*|deduct\w*|zero\s+(?:points|credit))(?![a-z])")
ATTEND = _rx(r"출석|결석|(?<![a-z])(?:attendance|absen\w*)(?![a-z])")


def _policies(s: str, submit: bool) -> list[tuple[str, str]]:
    if LATE.search(s) and (CONSEQUENCE.search(s) or submit or DELIVERABLE.search(s)):
        return [("policy", f"지각 규정: {_line(s)}")]
    if PLAGIARISM.search(s):
        return [("policy", f"표절 금지: {_line(s)}")]
    if PENALTY.search(s) and not ATTEND.search(s):
        return [("policy", f"감점 규정: {_line(s)}")]
    return []


# ---------------------------------------------------------------- 날짜 빼기·문장 다듬기

_TIME_RX = (RX_T_COLON, RX_T_DOT_AP, RX_T_KO, RX_T_EN, RX_T_WORD)
_DATE_TAIL = re.compile(r"^\s*(?:\(\s*[^()]{1,4}\s*\))?\s*(?:까지|전까지|부터|에는|에|이내|until|by)?")
_DATE_HEAD = _rx(r"(?:(?<![a-z])(?:by|until|on|before|due|at)\s*[:：]?\s*|마감\s*[:：]?\s*|기한\s*[:：]?\s*)$")
_BULLET = re.compile(r"^[\s\-–•·*▶▷※○●◦>]+")


def strip_dates(s: str) -> str:
    """문장에서 날짜·시각 말을 뺀다(체크 문장에는 마감을 넣지 않는다)."""
    spans = [(h.start, h.end) for h in find_dates(s, date.today())]
    for rx in _TIME_RX:
        spans += [(m.start(), m.end()) for m in rx.finditer(s)]
    if not spans:
        return s
    spans.sort()
    keep, pos = [], 0
    for a, b in spans:
        if a < pos:
            pos = max(pos, b)
            continue
        head = _DATE_HEAD.search(s[pos:a])
        keep.append(s[pos:a - (len(head.group(0)) if head else 0)])
        tail = _DATE_TAIL.match(s[b:])
        pos = b + (tail.end() if tail else 0)
    keep.append(s[pos:])
    out = "".join(keep)
    out = re.sub(r"\(\s*[,~\-–]?\s*\)|\[\s*\]", " ", out)
    out = re.sub(r"\s+([,.)!?])", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ,;:~-–")


def _line(s: str) -> str:
    t = strip_dates(_BULLET.sub("", s)).strip()
    return t if len(t) <= LINE_MAX else t[:LINE_MAX - 1].rstrip() + "…"


def _quote(s: str) -> str:
    s = s.strip()
    return s if len(s) <= QUOTE_MAX else s[:QUOTE_MAX - 1] + "…"


_LOOKS_HTML = re.compile(r"(?i)<\s*/?\s*(?:p|br|div|li|ul|ol|tr|td|table|span|a|h[1-6]|strong|b|em)\b[^>]*>")


def _plain(t: dict) -> str:
    text = str(t.get("text") or "")
    if t.get("is_html") or _LOOKS_HTML.search(text):
        return html_to_text(text)
    return text


def other_task(sentence: str, title: str | None) -> bool:
    """문장이 번호 붙은 다른 과제를 말하나('LAB4' 항목에 'Lab0, Lab1 점수'). 이름 없는 문장('PDF로 제출')은 아니다."""
    if not title:
        return False
    numbered = {t for t in title_tokens(sentence) if t[1] is not None}
    return bool(numbered) and not same_task(sentence, title)


# ---------------------------------------------------------------- 찾기

def requirements(sentence: str, title: str | None = None) -> list[tuple[str, str]]:
    """문장 하나의 제출 요건 [(종류, 체크 문장)]."""
    s = sentence.strip()
    if len(s) < 4:
        return []
    submit = bool(SUBMIT.search(s))
    material = bool(MATERIAL.search(s))
    ref_only = bool(REFERENCE.search(s)) and not submit
    out: list[tuple[str, str]] = []
    if not material and not ref_only:
        templates = _template_tokens(s)
        out += _formats(s, submit, templates)
        out += _names(s, templates)
        out += _lengths(s)
        out += _includes(s, submit, title)
    if not material:
        out += _wheres(s, submit)
    out += _teams(s)
    out += _policies(s, submit)
    return out


def rule_id(kind: str, text: str) -> str:
    return "r-" + hashlib.sha1(f"{kind}|{text}".encode("utf-8")).hexdigest()[:10]


def extract(texts: list, title: str | None = None) -> list[dict]:
    """texts: [{"text", "source_title", "url"(, "is_html")}] → [{"id", "kind", "text", "quote", "source_title", "url"}].
    title(항목 제목)을 주면 번호 붙은 다른 과제를 말하는 문장은 건너뛴다. 같은 (종류, 체크 문장)은 처음 것만,
    주소가 있는 '이메일로 제출'이 있으면 주소 없는 것은 뺀다. 종류 순(제출처·형식·이름·분량·포함·팀·규정)."""
    found: list[dict] = []
    seen: set = set()
    for t in texts or []:
        if not isinstance(t, dict):
            continue
        for s in split_sentences(_plain(t)):
            if other_task(s, title):
                continue
            for kind, text in requirements(s, title):
                if (kind, text) in seen:
                    continue
                seen.add((kind, text))
                found.append({"id": rule_id(kind, text), "kind": kind, "text": text, "quote": _quote(s),
                              "source_title": t.get("source_title"), "url": t.get("url")})
    if any(f["text"].startswith("이메일로 제출: ") for f in found):
        found = [f for f in found if f["text"] != "이메일로 제출"]
    found.sort(key=lambda f: KIND_ORDER.index(f["kind"]))   # 안정 정렬: 같은 종류는 글 순서
    return found[:RULES_MAX]


# ---------------------------------------------------------------- 항목마다 읽을 글

def _flat(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _assignment_ids(item: dict, items_all: list[dict]) -> list[str]:
    """그 항목의 Canvas 과제(자기 자신·연결된 과제·같은 과제 묶음이 연결된 과제)."""
    ids = []
    for v in [item] + [x for x in items_all if item.get("group") and x.get("group") == item["group"]]:
        for cid in (v["id"] if v.get("origin") == "canvas" else None, v.get("linked_to"), v.get("duplicate_of")):
            if cid and str(cid).startswith("canvas-") and cid not in ids:
                ids.append(cid)
    return ids


def _evidence(item: dict, items_all: list[dict]) -> list[dict]:
    """근거 사슬: 그 항목 + 같은 과제 묶음 + (Canvas 과제면) 그 과제와 겹치거나 연결된 찾은 후보의 근거."""
    out = list(item.get("evidence") or [])
    for v in items_all:
        if v["id"] == item["id"]:
            continue
        if (item.get("group") and v.get("group") == item["group"]) or \
                (item.get("origin") == "canvas" and item["id"] in (v.get("duplicate_of"), v.get("linked_to"))):
            out += list(v.get("evidence") or [])
    return [e for e in out if isinstance(e, dict)]


def _attached_pdfs(srcs: list[dict], course: str, aid: str):
    """그 과제(설명에 링크된 파일 = 가상 게시판 'assignments'의 글, 계약 24-3)에 붙은 PDF 글과 그 자리."""
    for s in srcs:
        if s.get("type") != "pdf" or str(s.get("course_id")) != course:
            continue
        ref = next((r for r in s.get("attached_to") or [] if isinstance(r, dict)
                    and str(r.get("board_id")) == "assignments" and str(r.get("post")) == aid), None)
        if ref is not None:
            yield s, ref


def texts_for(item: dict, srcs: list[dict], items_all: list[dict] | None = None) -> list[dict]:
    """항목 하나의 대상 글(계약 22-6·24-3): Canvas 과제 설명과 그 설명에 링크된 PDF → 근거 사슬의 글 →
    같은 과목 공지·게시판·첨부 중 제목이 맞는 것. srcs는 agent.sources(store) 모양(key, type, course_id, title, plain, url,
    PDF는 attached_to). 글을 못 찾은 근거는 그 문장만 쓴다."""
    items_all = items_all or [item]
    by_key = {s["key"]: s for s in srcs}
    out, seen = [], set()

    def add(s, url=None):
        if s["key"] in seen:
            return
        seen.add(s["key"])
        out.append({"text": s.get("plain") or "", "source_title": s.get("title"), "url": url or s.get("url"),
                    "key": s["key"]})

    for cid in _assignment_ids(item, items_all):
        course, aid = cid.split("-")[1], cid.rsplit("-", 1)[-1]
        s = by_key.get("assignment:" + aid)
        if s and str(s.get("course_id")) != course:
            s = None
        if s:
            add(s)
        for pdf, ref in _attached_pdfs(srcs, course, aid):
            add(pdf, url=ref.get("url") or (s or {}).get("url"))     # [원문]은 그 과제 화면
    course = str(item.get("course_id")) if item.get("course_id") is not None else None
    for e in _evidence(item, items_all):
        sentence = _flat(e.get("text"))
        hit = None
        for s in srcs:
            if s.get("type") != e.get("source_type"):
                continue
            if course and s.get("course_id") is not None and str(s["course_id"]) != course:
                continue
            if (e.get("url") and s.get("url") == e.get("url")) or (sentence and sentence in _flat(s.get("plain"))):
                hit = s
                break
        if hit:
            add(hit)
        elif sentence:
            key = "evidence:" + hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:10]
            if key not in seen:
                seen.add(key)
                out.append({"text": e.get("text") or "", "source_title": e.get("source_title"), "url": e.get("url"),
                            "key": key})
    title = item.get("title") or ""
    for s in srcs:
        if s.get("type") in ("announcement", "board", "pdf") and course and str(s.get("course_id")) == course \
                and title_matches(s.get("title") or "", title):
            add(s)
    return out


def title_matches(notice_title: str, item_title: str) -> bool:
    """공지 제목이 그 항목을 가리키나: 같은 과제(extract.same_task — 'HW5 안내'↔'Homework 5'), 또는 번호·여러 낱말로 된
    항목 제목이 공지 제목 안에 그대로 있음('Essay 1 guide'↔'Essay 1' — 'Essay 12'는 아님)."""
    if not item_title:
        return False
    if title_tokens(item_title) and same_task(notice_title, item_title):
        return True
    it = re.sub(r"\s+", "", item_title).lower()
    if len(it) < 4 or not (re.search(r"\d", it) or len(item_title.split()) >= 2):
        return False
    rx = (r"(?<![a-z0-9])" if re.match(r"[a-z0-9]", it) else "") + re.escape(it) + r"(?!\d)"
    return bool(re.search(rx, re.sub(r"\s+", "", notice_title).lower()))


def rules_for(item: dict, srcs: list[dict], items_all: list[dict] | None = None) -> list[dict]:
    return extract(texts_for(item, srcs, items_all), title=item.get("title"))


def all_sources(store) -> list[dict]:
    """agent.sources와 같은 글 묶음(제외한 과목·점수표 PDF는 빠짐)."""
    from . import agent
    return agent.sources(store)


def compute(store, items: list[dict], srcs: list[dict] | None = None) -> dict:
    """화면에 보이는 항목(review·scheduled)마다 규칙 칸. {item_id: [칸…]} (칸이 없는 항목은 빈 목록)."""
    visible = [v for v in items if v.get("status") in VISIBLE]
    if not visible:
        return {}
    srcs = all_sources(store) if srcs is None else srcs
    out = {}
    for v in visible:
        try:
            out[v["id"]] = rules_for(v, srcs, items)
        except Exception:          # 이상한 글 하나가 화면 상태 전체(/api/state)를 막지 않게 — 그 항목만 칸 없음
            out[v["id"]] = []
    return out


# ---------------------------------------------------------------- 저장·화면

def _box(store) -> dict:
    box = store.data.get("checklists")
    if not isinstance(box, dict):
        box = store.data["checklists"] = {}
    return box


def _entry(store, item_id: str, create: bool = False) -> dict:
    box = _box(store)
    e = box.get(item_id)
    if not isinstance(e, dict):
        e = {}
        if create:
            box[item_id] = e
    if create:
        e.setdefault("done", {})
        e.setdefault("added", [])
        e.setdefault("removed", [])
    return e


def _later(a: str | None, b: str | None) -> str | None:
    da, db = parse_iso(a), parse_iso(b)
    if da is None:
        return b if db else None
    if db is None:
        return a
    return a if da >= db else b


def merged(store, item_id: str, rules: list[dict]) -> list[dict]:
    """규칙 칸(지운 것 빼고) + 사용자 칸, 체크 상태를 붙여서."""
    e = _entry(store, item_id)
    done = e.get("done") if isinstance(e.get("done"), dict) else {}
    removed = set(e.get("removed") or [])
    out = [{"id": r["id"], "kind": r["kind"], "text": r["text"], "quote": r["quote"], "source_title": r["source_title"],
            "url": r["url"], "done": bool(done.get(r["id"])), "by": "rules"} for r in rules if r["id"] not in removed]
    for u in e.get("added") or []:
        if isinstance(u, dict) and isinstance(u.get("id"), str) and isinstance(u.get("text"), str):
            out.append({"id": u["id"], "kind": "user", "text": u["text"], "quote": None, "source_title": None,
                        "url": None, "done": bool(u.get("done")), "by": "user"})
    return out


def view(store, items: list[dict], rules: dict) -> dict:
    """state.checklists — 보이는 항목 중 칸이 하나라도 있는 것만(계약 22-6)."""
    out = {}
    last_sync = store.data.get("last_sync")
    for v in items:
        if v.get("status") not in VISIBLE:
            continue
        rows = merged(store, v["id"], rules.get(v["id"]) or [])
        if rows:
            out[v["id"]] = {"items": rows, "updated_at": _later(_entry(store, v["id"]).get("updated_at"), last_sync)}
    return out


def clean_text(text) -> str:
    if not isinstance(text, str):
        raise ValueError("text(체크할 내용)는 문자열이어야 해요")
    t = re.sub(r"\s+", " ", text).strip()
    if not t:
        raise ValueError("체크할 내용을 적어 주세요")
    if len(t) > TEXT_MAX:
        raise ValueError(f"체크할 내용은 {TEXT_MAX}자 이하로 적어 주세요")
    return t


def apply(store, item_id: str, rules: list[dict], body: dict) -> dict:
    """{"op": "toggle", "id", "done"?} | {"op": "add", "text"} | {"op": "remove", "id"} | {"op": "refresh"}.
    잘못된 입력은 ValueError(400), 없는 칸은 LookupError(404). 저장은 부르는 쪽이."""
    op = body.get("op")
    if op not in OPS:
        raise ValueError("op는 toggle, add, remove, refresh 중 하나여야 해요")
    now = iso(now_local())
    if op == "refresh":
        e = _entry(store, item_id, create=True)
        e["removed"] = []
        ids = {r["id"] for r in rules} | {u.get("id") for u in e["added"] if isinstance(u, dict)}
        e["done"] = {k: v for k, v in e["done"].items() if k in ids}
        e["updated_at"] = now
        return {"op": op}
    if op == "add":
        text = clean_text(body.get("text"))
        e = _entry(store, item_id, create=True)
        if len(e["added"]) >= USER_MAX:
            raise ValueError(f"직접 더한 칸은 {USER_MAX}개까지예요")
        row = {"id": "u-" + uuid.uuid4().hex[:10], "text": text, "done": False, "at": now}
        e["added"].append(row)
        e["updated_at"] = now
        return {"op": op, "id": row["id"]}
    cid = body.get("id")
    if not isinstance(cid, str) or not _ID.fullmatch(cid):
        raise ValueError("id(체크리스트 칸 id)가 필요해요")
    done_in = body.get("done")
    if op == "toggle" and done_in is not None and not isinstance(done_in, bool):
        raise ValueError("done은 true 또는 false여야 해요")
    e = _entry(store, item_id)
    removed = set(e.get("removed") or [])
    rule_ids = {r["id"] for r in rules if r["id"] not in removed}
    user = next((u for u in e.get("added") or [] if isinstance(u, dict) and u.get("id") == cid), None)
    if cid not in rule_ids and user is None:
        raise LookupError("체크리스트에 없는 칸이에요. 화면을 새로 고침하세요")
    e = _entry(store, item_id, create=True)
    if user is not None:      # create=True가 같은 dict를 돌려준다
        user = next(u for u in e["added"] if isinstance(u, dict) and u.get("id") == cid)
    if op == "toggle":
        if user is not None:
            user["done"] = (not bool(user.get("done"))) if done_in is None else done_in
            value = user["done"]
        else:
            value = (not bool(e["done"].get(cid))) if done_in is None else done_in
            e["done"][cid] = value
        e["updated_at"] = now
        return {"op": op, "id": cid, "done": value}
    # remove
    if user is not None:
        e["added"] = [u for u in e["added"] if not (isinstance(u, dict) and u.get("id") == cid)]
    else:
        e["removed"] = sorted(set(e["removed"]) | {cid})
    e["done"].pop(cid, None)
    e["updated_at"] = now
    return {"op": op, "id": cid}
