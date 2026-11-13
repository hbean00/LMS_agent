"""앱과 구글 캘린더 사이: 연결(OAuth) · 자동 반영 · 반영 기록 · 다른 일정 가져오기.

- 연결: start(redirect_uri) → 브라우저가 구글 동의 후 redirect_uri로 돌아옴 → finish(query).
  redirect_uri는 앱 서버 주소의 루트(http://127.0.0.1:포트/). 루트에 ?state=&code=가 붙어 오면 콜백으로 처리한다.
- 반영: push()가 승인된 일정(+주간 요약)을 'LMS 마감' 캘린더와 맞춘다. gcal.sync는 바뀐 것만 쓴다.
- 근거: 구글이 돌려준 일정 id·updated·링크를 store.data["gcal"]에 남긴다 → 화면의 근거 사슬 마지막 고리.
- 다른 일정 보기(계약 23장): start(read=True)로 읽기 권한만 증분 동의, fetch()/fetch_soon()이 gevents.fetch로 가져온다.
  허용 여부는 토큰의 scope(read_granted) — 구글이 403으로 거절하면 다시 동의할 때까지 허용 안 됨으로 본다(google.read_denied).
토큰은 config.json(google.tokens)에만 저장하고 화면·로그에 내보내지 않는다.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import core
from . import gevents
from .config import save_config
from .gcal import GoogleCalendar, GoogleError, OAuthFlow, deadline_window, read_granted, revoke
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
        # 다른 일정 가져오기(계약 23-2): 백그라운드 한 번에 하나, 도중에 또 부르면 끝난 뒤 한 번 더
        self.fetching = False
        self.fetch_again = False
        self.fetch_started: float | None = None   # 마지막 가져오기 시작(time.monotonic) — 동기화 뒤·15분마다 가져오기의 최소 간격

    # ------------------------------------------------------------ 상태

    @property
    def available(self) -> bool:
        return bool(self.client_id) and not str(self.client_id).startswith("AIza")

    @property
    def connected(self) -> bool:
        return bool((self.cfg.get("google") or {}).get("tokens"))

    @property
    def read_granted(self) -> bool:
        """토큰에 다른 일정 읽기 권한(목록·일정)이 둘 다 있고, 구글이 그 뒤로 403으로 거절하지 않았음(계약 23-1)."""
        g = self.cfg.get("google") or {}
        return bool(self.connected and read_granted((g.get("tokens") or {}).get("scope"))
                    and not g.get("read_denied"))

    @property
    def flow_purpose(self) -> str | None:
        """진행 중인 동의의 목적: "read"(다른 일정 보기) | "connect" | None."""
        return None if self.flow is None else ("read" if self.flow.read else "connect")

    def status(self, store) -> dict:
        g = store.data.get("gcal") or {}
        return {"available": self.available, "connected": self.connected, "pushing": self.pushing,
                "error": self.error, "last_push": g.get("at"),
                "last_result": {k: g.get(k) for k in ("created", "updated", "deleted", "unchanged")} if g else None,
                "desired": desired_counts(store), "calendar_link": None,
                "read": gevents.read_state(self, store)}

    # ------------------------------------------------------------ 연결

    def start(self, redirect_uri: str, read: bool = False) -> str:
        """동의 화면 주소. read=True면 연결된 상태에서 읽기 권한만 더 묻는다(증분 동의, 계약 23-1)."""
        if not self.available:
            raise GoogleError("구글 연결용 클라이언트 ID가 없습니다(.env의 LMS_AGENT_GOOGLE_CLIENT_ID)")
        if read and not self.connected:
            raise GoogleError(gevents.NOT_CONNECTED)
        self.flow = OAuthFlow(self.client_id, redirect_uri, client_secret=self.client_secret,
                              **{k: self.urls[k] for k in ("auth_url", "token_url") if k in self.urls})
        return self.flow.authorization_url(read=read)

    def finish(self, query: dict) -> dict:
        """동의에서 돌아옴 → {"purpose": "connect"|"read", "read_granted": bool}. 실패는 GoogleError(한국어).
        읽기 동의가 실패·취소돼도 지금의 쓰기 연결(토큰)은 그대로 둔다."""
        purpose = self.flow_purpose or "connect"
        if query.get("error"):
            self.flow = None
            if purpose == "read":
                raise GoogleError("다른 일정 보기 권한 요청이 취소됐어요(구글 캘린더 반영은 그대로예요)"
                                  if query["error"] == "access_denied" else "구글이 다른 일정 보기 권한을 거절했어요")
            raise GoogleError("구글 연결이 취소됐습니다" if query["error"] == "access_denied"
                              else "구글이 연결을 거절했습니다")
        if not self.flow:
            raise GoogleError("연결을 다시 시작하세요(앱을 다시 켰다면 처음부터)")
        old = (self.cfg.get("google") or {}).get("tokens") if purpose == "read" else None
        tokens = self.flow.exchange(query.get("code", ""), query.get("state", ""), old=old)
        self.flow = None
        g = {k: v for k, v in (self.cfg.get("google") or {}).items() if k != "read_denied"}
        self.cfg["google"] = {**g, "tokens": tokens}   # 다시 동의했다 — 예전 거절 표시는 지운다
        self._save()
        self.error = None
        return {"purpose": purpose, "read_granted": self.read_granted}

    def disconnect(self, store=None) -> None:
        """토큰 폐기·삭제(구글 캘린더의 일정은 남김). store를 주면 가져온 다른 일정과 '다른 일정 보기' 설정도 지운다(계약 23-1)."""
        g = self.cfg.get("google") or {}
        tok = (g.get("tokens") or {}).get("refresh_token") or (g.get("tokens") or {}).get("access_token")
        if tok:
            revoke(tok, **({"revoke_url": self.urls["revoke_url"]} if "revoke_url" in self.urls else {}))
        self.cfg["google"] = {k: v for k, v in g.items() if k not in ("tokens", "read_denied")}
        self._save()
        if store is not None:
            self._wipe_events(store, everything=True)

    def _set_tokens(self, tokens: dict) -> None:
        self.cfg["google"] = {**(self.cfg.get("google") or {}), "tokens": tokens}
        self._save()

    def calendar(self) -> GoogleCalendar:
        """지금 토큰으로 만든 API 클라이언트(갱신한 토큰은 config에 저장된다)."""
        g = self.cfg.get("google") or {}
        return GoogleCalendar(g.get("tokens"), self.client_id, client_secret=self.client_secret,
                              on_tokens=self._set_tokens,
                              **{k: self.urls[k] for k in ("api_base", "token_url") if k in self.urls})

    def drop_tokens(self, store=None) -> None:
        """구글이 연결을 끊었다(401·invalid_grant): 토큰을 지우고, store를 주면 가져온 다른 일정도 지운다(설정은 남김)."""
        self.cfg["google"] = {k: v for k, v in (self.cfg.get("google") or {}).items()
                              if k not in ("tokens", "read_denied")}
        self._save()
        if store is not None:
            self._wipe_events(store)

    def mark_read_denied(self) -> None:
        """구글이 읽기를 403으로 거절했다 → 다시 동의할 때까지 read_granted false."""
        self.cfg["google"] = {**(self.cfg.get("google") or {}), "read_denied": True}
        self._save()

    @staticmethod
    def _wipe_events(store, everything: bool = False) -> None:
        try:
            with store.locked():
                store.reload_if_changed()
                gevents.wipe(store, everything=everything)
                store.write()
        except (OSError, TimeoutError):
            gevents.wipe(store, everything=everything)   # 파일은 다음 저장 때(메모리에서는 이미 지움)

    # ------------------------------------------------------------ 반영

    def push(self, store) -> dict | None:
        """지금 상태를 구글에 맞춘다. 연결 안 됐으면 None."""
        if not (self.available and self.connected):
            return None
        g = self.cfg.get("google") or {}
        cal = self.calendar()
        try:
            cal_id = cal.ensure_calendar(g.get("calendar_id"))
            if cal_id != g.get("calendar_id"):
                self.cfg["google"] = {**(self.cfg.get("google") or {}), "calendar_id": cal_id}
                self._save()
            r = cal.sync(cal_id, desired_events(store))
        except GoogleError as e:
            self.error = str(e)
            if e.status == 401 and "다시 연결" in str(e):
                self.drop_tokens(store)   # 연결이 끊김 — 가져온 다른 일정도 지운다(계약 23-1)
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

    # ------------------------------------------------------------ 다른 일정 가져오기(계약 23-2)

    def fetch(self, store) -> dict:
        """지금 가져온다(이 스레드에서). gevents.fetch 참고."""
        return gevents.fetch(self, store)

    def fetch_soon(self, store, min_gap: float = 0) -> bool:
        """백그라운드로 가져온다(켜져 있고 연결·허용됐을 때만). 가져오는 중에 또 부르면 끝난 뒤 한 번 더.
        min_gap(초)을 주면 마지막 가져오기를 시작한 지 그만큼 지나지 않았거나 가져오는 중이면 건너뛴다(동기화 뒤·15분마다).
        시작했거나 예약했으면 True."""
        if not (self.available and self.read_granted and gevents.enabled(store)):
            return False
        with self.lock:
            if min_gap and (self.fetching or (self.fetch_started is not None
                                                and time.monotonic() - self.fetch_started < min_gap)):
                return False
            if self.fetching:
                self.fetch_again = True
                return True
            self.fetching = True
            self.fetch_started = time.monotonic()

        def run():
            while True:
                try:
                    gevents.fetch(self, store)
                except Exception:   # noqa: BLE001 — 가져오기 실패가 앱을 멈추게 하지 않는다(오류는 read.error에)
                    pass
                with self.lock:
                    if not self.fetch_again:
                        self.fetching = False
                        return
                    self.fetch_again = False
                    self.fetch_started = time.monotonic()

        threading.Thread(target=run, name="gcal-read", daemon=True).start()
        return True
