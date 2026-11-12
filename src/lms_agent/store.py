"""상태 파일(state.json): 마지막으로 받은 Canvas 원본, 만든 항목, 사용자의 결정."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .timeutil import iso, now_local, parse_iso, parse_local_input

DECISIONS = ("approved", "rejected", "pending")


def _default() -> dict:
    return {
        "version": 1,
        "cache": None,          # 마지막 동기화로 받은 Canvas 원본 (core.collect 결과)
        "items": [],            # core.build_items 결과
        "user": {},             # item id -> {decision, submitted, title, due}
        "manual": [],           # 직접 붙여 넣은 글
        "report": None,
        "last_sync": None,
        "settings": {"include_canvas": True, "weekly_summary": True, "lookback_days": 120},
    }


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = _default()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
                    self.data["settings"] = {**_default()["settings"], **(loaded.get("settings") or {})}
            except (OSError, ValueError):
                broken = self.path.with_suffix(".broken.json")
                try:
                    os.replace(self.path, broken)  # 깨진 파일은 지우지 않고 옆으로 치운다
                except OSError:
                    pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 사용자 결정

    def update_user(self, item_id: str, changes: dict) -> None:
        if not any(it["id"] == item_id for it in self.data["items"]):
            raise KeyError(item_id)
        u = dict(self.data["user"].get(item_id) or {})
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

    def view(self) -> list[dict]:
        seen = self.data.get("seen") or {}
        export = self.data.get("last_export") or {"at": None, "items": {}}
        gcal = self.data.get("gcal") or {}
        gcal_events = gcal.get("events") or {}
        out = []
        for it in self.data["items"]:
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
                # 근거 사슬: 처음 발견 → 승인 → 캘린더 파일 포함(받은 뒤 바뀌었으면 export_stale)
                "first_seen": seen.get(it["id"]), "decided_at": u.get("decided_at"),
                "exported_at": export["at"] if it["id"] in export["items"] else None,
                "export_stale": (it["id"] in export["items"]
                                 and export["items"][it["id"]] != f"{due}|{title}"),
                # 구글 캘린더 반영 증거: 구글이 돌려준 일정 id·수정 시각·링크(마지막 반영에 들어 있었으면)
                "gcal_event_id": (gcal_events.get(it["id"]) or {}).get("event_id"),
                "gcal_updated": (gcal_events.get(it["id"]) or {}).get("updated"),
                "gcal_link": (gcal_events.get(it["id"]) or {}).get("html_link"),
                "gcal_synced_at": gcal.get("at") if it["id"] in gcal_events else None,
            })
        out.sort(key=lambda x: (parse_iso(x["due"]) is None, x["due"] or "", x["course_name"], x["title"]))
        return out

    def calendar_items(self) -> list[dict]:
        include_canvas = self.data["settings"].get("include_canvas", True)
        return [v for v in self.view()
                if v["status"] == "scheduled" and v["due"] and (include_canvas or v["origin"] == "found")]
