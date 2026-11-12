"""상태 파일(state.json): 마지막으로 받은 Canvas 원본, 만든 항목, 사용자의 결정."""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .extract import title_tokens
from .timeutil import iso, now_local, parse_iso, parse_local_input

DECISIONS = ("approved", "rejected", "pending")
COURSE_MODES = ("normal", "pass_fail", "hidden")


def course_mode(data: dict, course_id) -> str:
    """과목 종류(계약 15장): store.data["course_prefs"][cid]["mode"] — 없으면 "normal"."""
    if course_id is None:
        return "normal"
    p = (data.get("course_prefs") or {}).get(str(course_id)) or {}
    m = p.get("mode") if isinstance(p, dict) else None
    return m if m in COURSE_MODES else "normal"


def hidden_courses(data: dict) -> set[str]:
    """제외(hidden)한 과목 id들. 항목은 계속 만들되 화면·캘린더·성적·알림·AI 정리에서 뺀다(계약 15장)."""
    return {str(cid) for cid, p in (data.get("course_prefs") or {}).items()
            if isinstance(p, dict) and p.get("mode") == "hidden"}


def _specific_title(title: str) -> bool:
    """제목만으로 한 과제를 가리키는지: 번호가 붙은 과제(LAB4, 과제 2) 또는 중간·기말. '마감'·'공지' 같은 제목은 묶지 않는다."""
    return any(n is not None or k in ("midterm", "final") for k, n in title_tokens(title))


def group_key(v: dict) -> str | None:
    """같은 과제의 '찾은 후보' 묶음 키(계약 13-1). agent_cli(AI 정리)와 화면(store.view)이 함께 쓰는 한 규칙:
    같은 linked_to(마감 없는 Canvas 과제에 연결됨), 없으면 같은 과목 + 정규화한 제목(공백 없앰·소문자).
    제목이 한 과제를 가리키지 않으면(번호·중간·기말 없음) 제목으로는 묶지 않는다. 찾은 후보가 아니면 None."""
    if v.get("origin", "found") != "found":
        return None
    if v.get("linked_to"):
        return f"link:{v['linked_to']}"
    title = str(v.get("title") or "")
    if not _specific_title(title):
        return None
    course = v.get("course_id") or v.get("course_name") or ""
    return f"title:{course}:" + "".join(title.split()).lower()


def _latest_evidence(v: dict) -> dict:
    """가장 최근 근거(게시 시각 기준). 근거가 없으면 빈 dict."""
    ev = [e for e in v.get("evidence") or [] if isinstance(e, dict)]
    if not ev:
        return {}
    return max(ev, key=lambda e: (parse_iso(e.get("posted_at")) or _OLD).timestamp())


def _md_hm(due: str | None) -> str:
    d = parse_iso(due)
    return f"{d.month}/{d.day} {d:%H:%M}" if d else "날짜 없음"


def _apply_groups(out: list[dict], user: dict) -> None:
    """같은 과제 후보 묶음(계약 13-1): group·alternatives·newest·replaced_by, 승인된 후보가 있으면 나머지는 'replaced',
    conflict는 제외·대체된 후보를 빼고 다시 계산한다. 승인이 둘 이상이면(옛 기록) 가장 나중에 승인한 것만 남긴다."""
    groups: dict[str, list[dict]] = {}
    for v in out:
        v.update(group=None, alternatives=[], newest=False, replaced_by=None)
        k = group_key(v)
        if k:
            groups.setdefault(k, []).append(v)
        if v["origin"] == "found":
            v["conflict"] = None   # 묶음에서 다시 계산(빌드 때 적은 문장은 저장된 항목에 남아 있다)
    for k, members in groups.items():
        if len(members) < 2:
            continue
        latest = {}
        for v in members:
            e = _latest_evidence(v)
            latest[v["id"]] = (parse_iso(e.get("posted_at")) or _OLD, e)
        top = max(t for t, _ in latest.values())
        approved = [v for v in members if v["decision"] == "approved" and v["due"]]
        winner = None
        if approved:
            winner = max(approved, key=lambda v: (str((user.get(v["id"]) or {}).get("decided_at") or ""),
                                                  latest[v["id"]][0].timestamp(), v["id"]))
        for v in members:
            v["group"] = k
            v["newest"] = latest[v["id"]][0] == top
            if winner is not None and v is not winner and v["status"] in ("review", "scheduled"):
                v["status"], v["replaced_by"] = "replaced", winner["id"]
        if winner is None:
            active = [v for v in members if v["status"] not in ("rejected", "replaced", "duplicate")]
            if len({v["due"] for v in active}) >= 2:
                for v in active:
                    mine = latest[v["id"]][0]
                    parts = []
                    for b in active:
                        if b is v or b["due"] == v["due"]:
                            continue
                        theirs, src = latest[b["id"]]
                        when = "더 최근 글" if theirs > mine else "이전 글"
                        parts.append(f"{when}('{src.get('source_title') or '근거'}')에는 {_md_hm(b['due'])}로 적혀 있습니다")
                    if parts:
                        v["conflict"] = "같은 과제에 다른 마감이 있습니다: " + "; ".join(parts)
        order = sorted(members, key=lambda v: (latest[v["id"]][0].timestamp(), v["id"]), reverse=True)
        for v in members:
            v["alternatives"] = [{"id": b["id"], "due": b["due"], "status": b["status"], "decision": b["decision"],
                                  "posted_at": latest[b["id"]][1].get("posted_at"),
                                  "source_type": latest[b["id"]][1].get("source_type"),
                                  "source_title": latest[b["id"]][1].get("source_title"),
                                  "newest": b["newest"]} for b in order if b is not v]


def _default() -> dict:
    return {
        "version": 1,
        "cache": None,          # 마지막 동기화로 받은 Canvas 원본 (core.collect 결과)
        "items": [],            # core.build_items 결과
        "user": {},             # item id -> {decision, submitted, title, due}
        "manual": [],           # 직접 붙여 넣은 글
        "report": None,
        "last_sync": None,
        "settings": {"include_canvas": True, "weekly_summary": True, "tutorial_done": False, "lookback_days": 120},
    }


_OLD = datetime(2000, 1, 1, tzinfo=timezone.utc)   # 시각을 모르는 근거(Windows는 1970 근처 timestamp가 안 됨)

LOCK_WAIT = 10.0    # 다른 프로세스(백그라운드 동기화)가 저장 중이면 기다리는 최대 시간(초)
LOCK_STALE = 60.0    # 이보다 오래된 잠금 파일은 죽은 프로세스가 남긴 것으로 보고 지운다


class Store:
    """state.json 하나. 앱 화면 서버와 백그라운드 동기화가 같은 파일을 쓴다:
    저장은 잠금 파일로 한 번에 하나씩, 읽는 쪽은 reload_if_changed()로 다른 프로세스의 저장을 따라간다."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.mtime: int | None = None
        self.data = _default()
        self._load()

    def _load(self) -> None:
        data = _default()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
                    data["settings"] = {**_default()["settings"], **(loaded.get("settings") or {})}
                self.mtime = self.path.stat().st_mtime_ns
            except (OSError, ValueError):
                broken = self.path.with_suffix(".broken.json")
                try:
                    os.replace(self.path, broken)  # 깨진 파일은 지우지 않고 옆으로 치운다
                except OSError:
                    pass
        self.data = data

    def reload_if_changed(self) -> bool:
        """다른 프로세스가 저장했으면 다시 읽는다. 읽었으면 True."""
        try:
            m = self.path.stat().st_mtime_ns
        except OSError:
            return False
        if m == self.mtime:
            return False
        self._load()
        return True

    @contextmanager
    def locked(self):
        lock = self.path.with_suffix(".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        end = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE:
                        os.remove(lock)
                        continue
                except OSError:
                    continue
                if time.monotonic() > end:
                    raise TimeoutError("다른 동기화가 저장 중입니다. 잠시 뒤 다시 시도하세요")
                time.sleep(0.05)
        try:
            os.close(fd)
            yield
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass

    def save(self) -> None:
        with self.locked():
            self.write()

    def write(self) -> None:
        """잠금을 이미 잡은 상태에서 쓴다(locked() 안에서만). 평소에는 save()."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)
        self.mtime = self.path.stat().st_mtime_ns

    # ------------------------------------------------------------ 사용자 결정

    def update_user(self, item_id: str, changes: dict, by: str = "user", reason: str | None = None) -> None:
        """사용자 결정(또는 AI 에이전트의 결정, by="ai"·reason). 누가 결정했는지 근거 사슬에 남는다(계약 12장).
        AI가 바꾼 항목을 사용자가 다시 손대면 decided_by는 "user"가 된다."""
        if by not in ("user", "ai"):
            raise ValueError("by는 user 또는 ai")
        if not any(it["id"] == item_id for it in self.data["items"]):
            raise KeyError(item_id)
        u = dict(self.data["user"].get(item_id) or {})
        if changes:
            u["decided_by"] = by
            u["ai_reason"] = (str(reason)[:300] if reason else None) if by == "ai" else None
        if "decision" in changes:
            d = changes["decision"]
            if d not in DECISIONS:
                raise ValueError("decision은 approved, rejected, pending 중 하나여야 합니다")
            u["decision"] = d
            u["decided_at"] = iso(now_local())  # 근거 사슬: 사용자가 언제 승인·제외했는지
        if "submitted" in changes:
            s = changes["submitted"]
            if s is not None and not isinstance(s, bool):
                raise ValueError("submitted는 true, false, null 중 하나여야 합니다")
            u["submitted"] = s
        if "title" in changes:
            t = changes["title"]
            if t is not None and (not isinstance(t, str) or len(t) > 200):
                raise ValueError("제목은 200자 이하 문자열이어야 합니다")
            u["title"] = t.strip() if isinstance(t, str) and t.strip() else None
        if "due" in changes:
            d = changes["due"]
            if d is None or d == "":
                u["due"] = None
            else:
                try:
                    u["due"] = iso(parse_local_input(str(d)))
                except ValueError:
                    raise ValueError("마감은 YYYY-MM-DDTHH:MM 형식이어야 합니다") from None
        self.data["user"][item_id] = {k: v for k, v in u.items() if v is not None}

    def add_manual(self, course_name: str, text: str, posted_at: str | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("붙여 넣은 글이 비어 있습니다")
        if len(text) > 50_000:
            raise ValueError("글이 너무 깁니다(5만 자 이하)")
        posted = now_local()
        if posted_at:
            try:
                posted = parse_local_input(posted_at[:10] + "T12:00")
            except ValueError:
                raise ValueError("게시일은 YYYY-MM-DD 형식이어야 합니다") from None
        entry = {"id": uuid.uuid4().hex[:10], "course_name": (course_name or "").strip() or "과목 미상",
                 "text": text, "posted_at": iso(posted)}
        self.data["manual"].append(entry)
        return entry

    # ------------------------------------------------------------ 화면용

    def record_export(self, items: list[dict]) -> None:
        """캘린더 파일을 내보낸 시각과, 그때 각 일정의 제목·마감을 남긴다."""
        self.data["last_export"] = {"at": iso(now_local()),
                                    "items": {it["id"]: f"{it.get('due')}|{it.get('title')}" for it in items}}

    def view(self, include_hidden: bool = False) -> list[dict]:
        """화면용 항목(사용자 결정 반영). 제외(hidden)한 과목의 항목은 빼고 돌려준다 — 대시보드·확인함·학기·캘린더(.ics·구글)·
        보상·알림·AI 정리가 모두 이것을 쓰므로 한곳에서 빠진다(계약 15장). 저장된 항목·결정은 그대로라 다시 보이면 바로 돌아온다."""
        seen = self.data.get("seen") or {}
        export = self.data.get("last_export") or {"at": None, "items": {}}
        gcal = self.data.get("gcal") or {}
        gcal_events = gcal.get("events") or {}
        hidden = set() if include_hidden else hidden_courses(self.data)
        out = []
        for it in self.data["items"]:
            if hidden and str(it.get("course_id")) in hidden:
                continue
            u = self.data["user"].get(it["id"], {})
            title = u.get("title") or it["title"]
            due = u.get("due") or it.get("due")
            decision = u.get("decision") or it.get("default_decision") or "pending"
            if u.get("submitted") is not None:
                submitted, sub_src = bool(u["submitted"]), "user"
            elif it.get("canvas_submitted"):
                submitted, sub_src = True, "canvas"
            else:
                submitted, sub_src = False, None
            if decision == "rejected":
                status = "rejected"
            elif it["origin"] == "found" and it.get("duplicate_of") and decision != "approved":
                status = "duplicate"
            elif it["origin"] == "canvas" and not due:
                status = "no_deadline"
            elif decision == "approved" and due:
                status = "scheduled"
            else:
                status = "review"
            out.append({
                "id": it["id"], "origin": it["origin"],
                "course_id": it.get("course_id"), "course_name": it.get("course_name") or "과목 미상",
                "title": title, "due": due, "kind": it.get("kind") or "과제",
                "method": it.get("method") or "unknown", "email_to": it.get("email_to"),
                "confidence": it.get("confidence"), "status": status, "decision": decision,
                "submitted": submitted, "submitted_source": sub_src,
                "duplicate_of": it.get("duplicate_of"), "linked_to": it.get("linked_to"),
                "conflict": it.get("conflict"), "notes": it.get("notes") or [],
                "evidence": it.get("evidence") or [], "url": it.get("url"),
                "edited": bool(u.get("title") or u.get("due")),
                # 직접 고쳤으면 원래 값(근거 사슬의 "직접 고침 (원문 11/25)")
                "orig_due": it.get("due") if u.get("due") else None,
                "orig_title": it["title"] if u.get("title") else None,
                # 근거 사슬: 처음 발견 → 승인 → 캘린더 파일 포함(받은 뒤 바뀌었으면 export_stale)
                "first_seen": seen.get(it["id"]), "decided_at": u.get("decided_at"),
                "decided_by": u.get("decided_by"), "ai_reason": u.get("ai_reason"),
                "exported_at": export["at"] if it["id"] in export["items"] else None,
                "export_stale": (it["id"] in export["items"]
                                 and export["items"][it["id"]] != f"{due}|{title}"),
                # 구글 캘린더 반영 증거: 구글이 돌려준 일정 id·수정 시각·링크(마지막 반영에 들어 있었으면)
                "gcal_event_id": (gcal_events.get(it["id"]) or {}).get("event_id"),
                "gcal_updated": (gcal_events.get(it["id"]) or {}).get("updated"),
                "gcal_link": (gcal_events.get(it["id"]) or {}).get("html_link"),
                "gcal_synced_at": gcal.get("at") if it["id"] in gcal_events else None,
            })
        _apply_groups(out, self.data["user"])
        out.sort(key=lambda x: (parse_iso(x["due"]) is None, x["due"] or "", x["course_name"], x["title"]))
        return out

    def release_group(self, item_id: str) -> list[str]:
        """item_id를 승인할 때 부른다: 같은 과제 묶음에서 이미 승인돼 있던 다른 후보를 pending으로 돌린다
        (한 과제 = 캘린더 일정 하나, 계약 13-1). 돌린 id 목록."""
        view = self.view()
        me = next((v for v in view if v["id"] == item_id), None)
        if me is None or not me.get("group"):
            return []
        others = [v["id"] for v in view if v["id"] != item_id and v.get("group") == me["group"]
                  and v["decision"] == "approved"]
        for oid in others:
            self.update_user(oid, {"decision": "pending"}, by="user")
        return others

    def calendar_items(self) -> list[dict]:
        """캘린더에 넣을 것: 일정(scheduled)만. 같은 과제 묶음에서는 승인된 하나만 scheduled이다(view가 보장)."""
        include_canvas = self.data["settings"].get("include_canvas", True)
        return [v for v in self.view()
                if v["status"] == "scheduled" and v["due"] and (include_canvas or v["origin"] == "found")]
