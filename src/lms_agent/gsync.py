"""앱과 구글 캘린더 사이: 연결(OAuth) · 자동 반영 · 반영 기록.

- 연결: start(redirect_uri) → 브라우저가 구글 동의 후 redirect_uri로 돌아옴 → finish(query).
  redirect_uri는 앱 서버 주소의 루트(http://127.0.0.1:포트/). 루트에 ?state=&code=가 붙어 오면 콜백으로 처리한다.
- 반영: push()가 승인된 일정(+주간 요약)을 'LMS 마감' 캘린더와 맞춘다. gcal.sync는 바뀐 것만 쓴다.
- 근거: 구글이 돌려준 일정 id·updated·링크를 store.data["gcal"]에 남긴다 → 화면의 근거 사슬 마지막 고리.
토큰은 config.json(google.tokens)에만 저장하고 화면·로그에 내보내지 않는다.
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import core
from .config import save_config
from .gcal import GoogleCalendar, GoogleError, OAuthFlow, deadline_window, revoke
from .ics import event_description
from .timeutil import iso, now_local, parse_iso


def desired_events(store) -> list[dict]:
    """구글에 있어야 할 일정: 캘린더 파일과 같은 대상 + (설정이 켜져 있으면) 주간 요약."""
    from .ics import event_summary

    items, summaries = core.calendar_payload(store)  # .ics와 같은 대상
    out = []
    for it in items:
        due = parse_iso(it.get("due"))
        if not due:
            continue
        start, end = deadline_window(due)
        it = {**it, "course_name": it.get("course_short") or it.get("course_name")}
        out.append({"lms_id": it["id"], "summary": event_summary(it), "description": event_description(it),
                    "start": start, "end": end, "url": it.get("url"), "reminders": [1440, 180]})
    for s in summaries:   # 주간 요약·확인 알림(알림 없음)과 공부 블록(길이 있음, 30분 전 알림 — 계약 22-5)
        out.append({"lms_id": s["uid"].split("@")[0], "summary": s["summary"], "description": s["description"],
                    "start": s["start"], "end": s["end"], "url": s.get("url") or None,
                    "reminders": list(s.get("reminders") or [])})
    return out


def desired_counts(store) -> dict:
    """구글에 있어야 할 일정 수(계약 13-5): {"deadlines": 마감, "summaries": 주간 요약·확인 알림} — desired_events와 같은 대상.
    공부 블록(계약 22-5)이 있으면 "study": 블록 수가 더 붙는다(합 = desired_events 수)."""
    items, summaries = core.calendar_payload(store)
    study = sum(1 for s in summaries if s.get("kind") == "study")
    out = {"deadlines": sum(1 for it in items if parse_iso(it.get("due"))), "summaries": len(summaries) - study}
    if study:
        out["study"] = study
    return out


class GoogleLink:
    def __init__(self, cfg: dict, home: Path, client: tuple[str | None, str | None],
                 urls: dict | None = None, save=None):
        self.cfg = cfg
        self.home = home
        self.client_id, self.client_secret = client
        self.urls = urls or {}
        self._save = save or (lambda: save_config(self.cfg, self.home))
        self.flow: OAuthFlow | None = None
        self.lock = threading.Lock()
        self.pushing = False
        self.again = False
        self.error: str | None = None

    # ------------------------------------------------------------ 상태

    @property
    def available(self) -> bool:
        return bool(self.client_id) and not str(self.client_id).startswith("AIza")

    @property
    def connected(self) -> bool:
        return bool((self.cfg.get("google") or {}).get("tokens"))

    def status(self, store) -> dict:
        g = store.data.get("gcal") or {}
        return {"available": self.available, "connected": self.connected, "pushing": self.pushing,
                "error": self.error, "last_push": g.get("at"),
                "last_result": {k: g.get(k) for k in ("created", "updated", "deleted", "unchanged")} if g else None,
                "desired": desired_counts(store), "calendar_link": None}

    # ------------------------------------------------------------ 연결

    def start(self, redirect_uri: str) -> str:
        if not self.available:
            raise GoogleError("구글 연결용 클라이언트 ID가 없습니다(.env의 LMS_AGENT_GOOGLE_CLIENT_ID)")
        self.flow = OAuthFlow(self.client_id, redirect_uri, client_secret=self.client_secret,
                              **{k: self.urls[k] for k in ("auth_url", "token_url") if k in self.urls})
        return self.flow.authorization_url()

    def finish(self, query: dict) -> None:
        if query.get("error"):
            self.flow = None
            raise GoogleError("구글 연결이 취소됐습니다" if query["error"] == "access_denied"
                              else "구글이 연결을 거절했습니다")
        if not self.flow:
            raise GoogleError("연결을 다시 시작하세요(앱을 다시 켰다면 처음부터)")
        tokens = self.flow.exchange(query.get("code", ""), query.get("state", ""))
        self.flow = None
        self._set_tokens(tokens)
        self.error = None

    def disconnect(self) -> None:
        g = self.cfg.get("google") or {}
        tok = (g.get("tokens") or {}).get("refresh_token") or (g.get("tokens") or {}).get("access_token")
        if tok:
            revoke(tok, **({"revoke_url": self.urls["revoke_url"]} if "revoke_url" in self.urls else {}))
        self.cfg["google"] = {k: v for k, v in g.items() if k not in ("tokens",)}
        self._save()

    def _set_tokens(self, tokens: dict) -> None:
        self.cfg["google"] = {**(self.cfg.get("google") or {}), "tokens": tokens}
        self._save()

    # ------------------------------------------------------------ 반영

    def push(self, store) -> dict | None:
        """지금 상태를 구글에 맞춘다. 연결 안 됐으면 None."""
        if not (self.available and self.connected):
            return None
        g = self.cfg.get("google") or {}
        cal = GoogleCalendar(g["tokens"], self.client_id, client_secret=self.client_secret,
                             on_tokens=self._set_tokens,
                             **{k: self.urls[k] for k in ("api_base", "token_url") if k in self.urls})
        try:
            cal_id = cal.ensure_calendar(g.get("calendar_id"))
            if cal_id != g.get("calendar_id"):
                self.cfg["google"] = {**(self.cfg.get("google") or {}), "calendar_id": cal_id}
                self._save()
            r = cal.sync(cal_id, desired_events(store))
        except GoogleError as e:
            self.error = str(e)
            if e.status == 401 and "다시 연결" in str(e):
                self.cfg["google"] = {k: v for k, v in (self.cfg.get("google") or {}).items() if k != "tokens"}
                self._save()
            raise
        self.error = "; ".join(r["errors"][:3]) or None
        store.data["gcal"] = {"at": iso(now_local()), **{k: r[k] for k in ("created", "updated", "deleted", "unchanged")},
                              "errors": r["errors"], "events": r["events"]}
        store.save()
        return r

    def push_soon(self, store) -> None:
        """백그라운드로 반영. 반영 중에 또 바뀌면 끝난 뒤 한 번 더 한다."""
        if not (self.available and self.connected):
            return
        with self.lock:
            if self.pushing:
                self.again = True
                return
            self.pushing = True

        def run():
            while True:
                try:
                    self.push(store)
                except (GoogleError, OSError):
                    pass  # self.error에 남았다
                with self.lock:
                    if not self.again:
                        self.pushing = False
                        return
                    self.again = False

        threading.Thread(target=run, daemon=True).start()
