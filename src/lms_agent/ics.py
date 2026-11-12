"""캘린더 파일(.ics, RFC 5545) 만들기와 검사."""
from __future__ import annotations

from datetime import datetime

from .timeutil import now_local, parse_iso, to_utc_stamp

METHOD_LABEL = {"email": "이메일 제출", "lms": "LMS 제출", "paper": "종이 제출", "unknown": "제출 방법 확인 필요"}
SOURCE_LABEL = {"announcement": "공지", "inbox": "메시지", "assignment": "과제 설명",
                "syllabus": "강의계획서", "manual": "직접 입력"}


def _esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """한 줄이 75옥텟을 넘으면 접는다. UTF-8 글자 중간에서 자르지 않는다."""
    out, cur, size, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, 75
        cur += ch
        size += n
    out.append(cur)
    return out


def _short(name: str, n: int = 16) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def event_summary(item: dict) -> str:
    s = f"[{_short(item.get('course_name') or '과목 미상')}] {item.get('title') or '마감'}"
    if item.get("method") == "email":
        s += " (메일 제출)"
    if item.get("submitted"):
        s = "✔ " + s
    return s


def event_description(item: dict) -> str:
    parts = [METHOD_LABEL.get(item.get("method") or "unknown", "")]
    if item.get("email_to"):
        parts.append(f"제출처: {item['email_to']}")
    if item.get("submitted"):
        parts.append("제출함")
    for ev in item.get("evidence") or []:
        label = SOURCE_LABEL.get(ev.get("source_type"), "근거")
        title = f" '{ev['source_title']}'" if ev.get("source_title") else ""
        parts.append(f"근거({label}{title}): {ev.get('text', '')}")
    for note in item.get("notes") or []:
        parts.append(f"※ {note}")
    if item.get("url"):
        parts.append(item["url"])
    parts.append("LMS Agent로 만든 일정")
    return "\n".join(p for p in parts if p)


def build_ics(items: list[dict], now: datetime | None = None, summaries: list[dict] | None = None) -> str:
    """items: 마감 일정. summaries: 주간 요약 {uid, start, end, summary, description} (알림 없음)."""
    stamp = to_utc_stamp(now or now_local())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//LMS Agent//lms-agent//KO",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:LMS 마감"]
    for it in items:
        due = parse_iso(it.get("due"))
        if not due:
            continue
        summary = event_summary(it)
        t = to_utc_stamp(due)
        lines += ["BEGIN:VEVENT", f"UID:{it['id']}@lms-agent", f"DTSTAMP:{stamp}",
                  f"DTSTART:{t}", f"DTEND:{t}",
                  f"SUMMARY:{_esc(summary)}", f"DESCRIPTION:{_esc(event_description(it))}",
                  f"CATEGORIES:{_esc(it.get('kind') or '과제')}", "TRANSP:TRANSPARENT"]
        url = it.get("url") or next((e.get("url") for e in it.get("evidence") or [] if e.get("url")), None)
        if url:
            lines.append(f"URL:{url}")
        for trig in ("-P1D", "-PT3H"):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"TRIGGER:{trig}",
                      f"DESCRIPTION:{_esc(summary)}", "END:VALARM"]
        lines.append("END:VEVENT")
    for s in summaries or []:
        lines += ["BEGIN:VEVENT", f"UID:{s['uid']}", f"DTSTAMP:{stamp}",
                  f"DTSTART:{to_utc_stamp(s['start'])}", f"DTEND:{to_utc_stamp(s['end'])}",
                  f"SUMMARY:{_esc(s['summary'])}", f"DESCRIPTION:{_esc(s['description'])}",
                  "CATEGORIES:주간 요약", "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    out: list[str] = []
    for ln in lines:
        out.extend(_fold(ln))
    return "\r\n".join(out) + "\r\n"


def validate_ics(text: str) -> list[str]:
    """문제 목록을 돌려준다. 빈 목록이면 통과."""
    problems: list[str] = []
    if "\n" in text.replace("\r\n", ""):
        problems.append("줄 끝이 CRLF가 아닌 곳이 있습니다")
    raw = text.split("\r\n")
    for i, ln in enumerate(raw, 1):
        if len(ln.encode("utf-8")) > 75:
            problems.append(f"{i}번째 줄이 75옥텟을 넘습니다")
    unfolded: list[str] = []
    for ln in raw:
        if ln.startswith(" ") and unfolded:
            unfolded[-1] += ln[1:]
        elif ln:
            unfolded.append(ln)
    stack: list[str] = []
    event: set[str] | None = None
    uids: set[str] = set()
    for ln in unfolded:
        name, _, value = ln.partition(":")
        key = name.split(";")[0].upper()
        if key == "BEGIN":
            stack.append(value)
            if value == "VEVENT":
                event = set()
        elif key == "END":
            if not stack or stack[-1] != value:
                problems.append(f"BEGIN/END 짝이 맞지 않습니다: END:{value}")
                return problems
            stack.pop()
            if value == "VEVENT" and event is not None:
                for req in ("UID", "DTSTAMP", "DTSTART", "SUMMARY"):
                    if req not in event:
                        problems.append(f"VEVENT에 {req}가 없습니다")
                event = None
        elif event is not None and stack and stack[-1] == "VEVENT":
            event.add(key)
            if key == "UID":
                if value in uids:
                    problems.append(f"UID가 겹칩니다: {value}")
                uids.add(value)
    if stack:
        problems.append("닫히지 않은 BEGIN이 있습니다")
    if not unfolded or unfolded[0] != "BEGIN:VCALENDAR":
        problems.append("BEGIN:VCALENDAR로 시작하지 않습니다")
    return problems
