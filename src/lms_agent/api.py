"""기능 모듈이 자기 API를 등록하는 곳. 서버(server.py)는 보안 검사와 HTTP만 맡고, 일은 여기 등록된 것에 넘긴다.

기능 하나 추가하기(예: features/notes.py):

    from ..api import router, ApiError

    @router.route("POST", r"/api/notes/(?P<cid>[\\w-]+)")
    def save_note(app, body, cid):          # 경로의 이름 붙은 그룹이 키워드 인자로 온다
        ...
        app.emit("changed")                  # 다른 기능(구글 반영 등)이 따라 움직인다
        return app.state()                   # dict → 200 JSON. Response로 직접 줄 수도 있다

    @router.state_section("notes")           # GET /api/state 의 "notes" 칸
    def notes_state(app, ctx):
        return {...}

그리고 features/__init__.py의 FEATURES에 모듈 이름을 넣는다. 계약은 docs/CONTRACTS.md에 적는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


class ApiError(Exception):
    """화면에 보여 줄 한국어 오류. status는 HTTP 코드."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    ctype: str = "text/plain; charset=utf-8"
    headers: dict = field(default_factory=dict)


@dataclass
class Route:
    method: str
    pattern: re.Pattern
    handler: Callable
    auth: bool          # True면 X-LMS-Agent 세션 값이 맞아야 한다
    name: str
    priority: int = 0   # 같은 경로에 여럿이면 큰 것부터(예: 루트의 구글 콜백 > 페이지)


class Router:
    def __init__(self):
        self.routes: list[Route] = []
        self.sections: list[tuple[str | None, Callable]] = []
        self.listeners: dict[str, list[Callable]] = {}
        self.init_hooks: list[Callable] = []

    def route(self, method: str, pattern: str, auth: bool = True, priority: int = 0):
        """핸들러: fn(app, body, **경로그룹) → dict | Response | None(None이면 다음 경로로 넘김).
        GET 핸들러의 body는 쿼리 문자열 dict다."""
        def deco(fn):
            self.routes.append(Route(method.upper(), re.compile(pattern), fn, auth,
                                     fn.__module__ + "." + fn.__name__, priority))
            self.routes.sort(key=lambda r: -r.priority)  # 안정 정렬: 같은 우선순위는 등록 순서
            return fn
        return deco

    def state_section(self, key: str | None):
        """GET /api/state에 칸을 더한다. key=None이면 돌려준 dict를 최상위에 합친다. fn(app, ctx)."""
        def deco(fn):
            self.sections.append((key, fn))
            return fn
        return deco

    def on(self, event: str):
        """app.emit(event, **kw)가 불리면 실행된다. fn(app, **kw)."""
        def deco(fn):
            self.listeners.setdefault(event, []).append(fn)
            return fn
        return deco

    def on_init(self, fn):
        """App이 만들어질 때 실행된다. fn(app, options) — 기능이 쓸 서비스를 app에 붙인다."""
        self.init_hooks.append(fn)
        return fn

    def match(self, method: str, path: str):
        for r in self.routes:
            if r.method == method:
                m = r.pattern.fullmatch(path)
                if m:
                    yield r, m.groupdict()


router = Router()


class StateContext:
    """한 번의 /api/state 계산에서 여러 칸이 같이 쓰는 값(항목·학기·과목)을 한 번만 만든다."""

    def __init__(self, app):
        self.app = app
        self._cache: dict = {}

    def get(self, key: str, make: Callable):
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]
