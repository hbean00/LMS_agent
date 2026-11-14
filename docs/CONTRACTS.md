# 모듈 사이 계약

각 모듈은 이 문서의 모양만 믿고 따로 만든다. 바꿔야 하면 리더(통합 담당)에게 먼저 알린다.
날짜는 모두 ISO 8601 문자열이다. Canvas 원본은 UTC(`...Z`), 앱 내부·화면은 PC 로컬 시간대 오프셋(`2026-11-20T23:59:00+09:00`).

## 1. `src/lms_agent/canvas.py` — Canvas API 클라이언트 (표준 라이브러리만)

```python
class CanvasError(Exception): ...   # 메시지는 한국어, 토큰은 절대 포함하지 않는다

class Canvas:
    def __init__(self, base_url: str, token: str, timeout: float = 20): ...
    def whoami(self) -> dict                      # GET /api/v1/users/self  -> {"id", "name", ...}
    def courses(self) -> list[dict]               # GET /api/v1/courses?enrollment_state=active&include[]=syllabus_body&per_page=100
                                                  #   접근 제한 과목(access_restricted_by_date) 등 name 없는 항목은 뺀다
    def assignments(self, course_id) -> list[dict]  # GET /api/v1/courses/:id/assignments?include[]=submission&per_page=100
    def announcements(self, course_ids: list, start: datetime, end: datetime) -> list[dict]
                                                  # GET /api/v1/announcements?context_codes[]=course_1&...&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
                                                  #   context_codes는 10개씩 나눠 요청
    def conversations(self, since: datetime, limit: int = 30) -> list[dict]
                                                  # GET /api/v1/conversations?scope=inbox&per_page=50 로 목록,
                                                  # last_message_at >= since 인 것만 최대 limit개,
                                                  # 각각 GET /api/v1/conversations/:id?auto_mark_as_read=false 로 본문을 받는다
                                                  # (auto_mark_as_read=false 필수: 읽음 처리하면 안 된다)
```

- 인증: `Authorization: Bearer <token>`.
- 페이지: 응답의 `Link` 헤더 `rel="next"`를 따라간다. **next URL의 호스트가 base_url과 다르면 따라가지 않는다**(토큰 유출 방지).
- 응답 앞에 `while(1);`이 붙어 있으면 떼고 JSON 파싱.
- 오류: 401 → "토큰이 올바르지 않거나 만료됐습니다", 403 → 권한 없음, 404, 연결 실패, 타임아웃을 각각 한국어 CanvasError로.

반환 dict는 Canvas 원본 필드를 그대로 둔다. 통합 쪽이 쓰는 필드:

| 메서드 | 쓰는 필드 |
|---|---|
| courses | `id, name, course_code, syllabus_body, start_at` |
| assignments | `id, name, due_at, description, html_url, submission_types, created_at, updated_at, submission{workflow_state, submitted_at}` |
| announcements | `id, title, message, posted_at, context_code ("course_101"), html_url` |
| conversations | `id, subject, context_code, context_name, last_message_at, messages[{id, body, created_at}]` |

## 2. `src/lms_agent/mock_canvas.py` — 데모·테스트용 가짜 Canvas

```python
DEMO_TOKEN = "demo-token"
def build_demo_data(today: date, tz: tzinfo) -> dict       # 아래 시나리오, 날짜는 today 기준 상대값
def start_mock_canvas(port: int = 0, today=None, tz=None) -> tuple[ThreadingHTTPServer, str]
                                                           # 백그라운드 스레드로 띄우고 (server, base_url) 반환. 127.0.0.1에만 바인딩
```

- 위 1번의 엔드포인트를 같은 모양으로 흉내 낸다. `/api/v1/...`만 Canvas 토큰(`demo-token`)을 요구하고, 아니면 401.
- 과제 목록은 페이지 크기 2로 잘라 `Link: <...&page=2>; rel="next"`를 준다(페이지 처리 검증용).
- 받은 요청: `server.requests: list[str]`(GET은 경로만, POST는 `"POST /경로"`), `server.header_log: list[(요청, Authorization, Cookie)]`
  (테스트가 "Canvas 토큰은 /api/v1에만, 다운로드에는 인증 없음"을 확인).
- LearningX 흉내(실제 고려대 흐름과 같은 순서, 10장):
  - `GET /api/v1/courses/:id/external_tools` — 과목 101에만 게시판 도구 5 `게시판`(`/learningx/lti/learningx_board/boards`, course_navigation 있음)과
    미끼 도구(3 출석, 6 전체 게시판). `include_parents=true`면 모든 과목에 다른 호스트의 Zoom 도구. 없는 과목 404.
  - `GET /api/v1/courses/:id/external_tools/sessionless_launch?id=5` → `{id, name, url}`(한 번만 쓰는 verifier).
  - `GET /courses/:id/external_tools/sessionless_launch?verifier=` → 미끼 로그아웃 폼 + LTI 1.1 폼(진짜 HMAC-SHA1 `oauth_signature`).
  - `POST /learningx/lti/learningx_board/boards` — 서명·nonce(한 번만) 확인 → 쿠키 `laravel_session`, `XSRF-TOKEN`, `xn_api_token`(과목별, `server.lx_tokens`) → 302.
  - `/learningx/api/v1/learningx_board/courses/:cid/boards`, `/boards/:bid/posts?page=N`(`{items, pagination}`, 본문·`attachment_count`만),
    `/posts/:pid`(`{item:{…, attachments, post_url}}`). `xn` 토큰이 없거나 Canvas 토큰이면 401, 다른 과목 토큰이면 403.
  - `GET /files/:fid/download?verifier=` — Authorization이 있으면 403, verifier가 틀리면 `/login/canvas`(HTML)로 302, 맞으면 `{storage_base}/storage/:fid`로 302.
    `GET /storage/:fid`는 PDF 바이트(Authorization 있으면 400). `GET /api/v1/files/:fid` → `{id, filename, display_name, size, url}`.
  - 시험용 손잡이: `lx_fail`(`"sessionless" | "form" | "foreign" | "launch" | "cookie" | "api"`), `lx_page_size`(기본 `LX_PAGE_SIZE`=20),
    `lx_unwrapped`(글 상세를 `item`으로 감싸지 않음), `storage_base`(두 번째 가짜 서버를 가리키면 다른 호스트로 넘어가는 다운로드). 상수 `BOARD_TOOL_ID = 5`.

### 데모 시나리오 (T = today, 시각은 로컬 시간 → UTC로 변환해 `Z` 문자열)

| 과목 (id, name, course_code) | 데이터 | 기대 결과 |
|---|---|---|
| 101 ASIC설계 EE401 | 과제 1011 "LAB3 보고서" due T-5 23:59, online_upload, submission submitted | Canvas 일정, 제출함 |
| | 과제 1012 "LAB4 보고서" due null, submission_types ["none"], description: `<p>LAB4 보고서는 조교 이메일(<a href="mailto:asic.ta@example.ac.kr">asic.ta@example.ac.kr</a>)로 제출합니다.</p><p>마감은 공지사항을 확인하세요.</p>` | 마감 없음 → 후보와 연결 |
| | 과제 1013 "LAB5 예비보고서" due T+10 23:59, online_upload, unsubmitted | Canvas 일정 |
| | 공지 9001 posted T-3 10:00 "LAB4 보고서 제출 안내": `<p>LAB4 보고서 마감: {M}월 {D}일({요일}) 23:59</p><p>제출: asic.ta@example.ac.kr 로 PDF 첨부</p>` (날짜 T+6) | 후보: LAB4, T+6 23:59, 이메일 |
| | 메시지 7001 (context_code course_101, context_name ASIC설계) 메시지 1개 posted T-1 20:00: `LAB4 보고서 마감이 {M}월 {D}일 자정으로 연장되었습니다. 메일로 제출해 주세요.` (날짜 T+8) | 후보: LAB4, T+8 23:59, 이메일, 앞 후보와 충돌 표시 |
| 102 디지털신호처리 EE342 | 과제 1021 "Homework 5" due T+4 18:00, online_upload, unsubmitted | Canvas 일정 |
| | 공지 9002 posted T-2 09:00 "HW5 안내": `<p>Homework 5는 Canvas 과제함으로 제출하세요. 마감은 {M}/{D} 18:00입니다.</p>` (T+4) | Canvas와 겹침(중복) |
| | 공지 9003 posted T-1 13:00 "중간 프로젝트 보고서": `<p>중간 프로젝트 보고서는 이메일(dsp.prof@example.ac.kr)로 다음 주 금요일까지 보내 주세요.</p>` | 후보: 게시일 기준 다음 주 금요일 23:59 |
| | 공지 9004 posted T-1 08:00 "휴강 안내": `<p>{M}월 {D}일 휴강입니다. 보강은 추후 공지합니다.</p>` (T+2) | 후보 아님 |
| 103 반도체공학 EE361 | 과제 없음. syllabus_body: `<table><tr><td>1주차</td><td>{M}월 {D}일 오리엔테이션</td></tr><tr><td>과제 2</td><td>{M}월 {D}일까지 이메일 제출</td></tr><tr><td>기말고사</td><td>{M}월 {D}일 15:00</td></tr></table>` (각각 T-60, T+12, T+30) | 후보 2개: 과제 2(T+12), 기말고사(T+30 15:00, 시험) |
| 104 Capstone Design CAP400 | 과제 1041 "Weekly log 9" due null, submission_types ["none"], description "" | 마감 없음, 근거 못 찾음 |
| | 공지 9005 posted T-4 "Final report": `<p>Please email your final report to capstone@example.ac.kr by {Mon} {D}, 11:59 PM.</p>` ({Mon}=Jan..Dec 영문 약어, 날짜 T+18) | 후보: T+18 23:59, 이메일 |
| (과목 없음) | 메시지 7002 context_code "user_1", 메시지 posted T-2 "확인했습니다." | 후보 아님 |

영문 월 이름은 `strftime("%b")`를 쓰지 않는다(윈도 한국어 로캘에서 "11월"이 나온다). 직접 목록을 둔다.

## 3. 로컬 서버 API (`src/lms_agent/server.py`) ↔ 화면 (`src/lms_agent/web/index.html`)

- 서버는 `127.0.0.1`에만 뜬다. 화면은 HTML 한 장(외부 CDN·폰트 없음, 바닐라 JS, 인터넷 없어도 동작).
- HTML 안의 `__LMS_AGENT_TOKEN__` 문자열을 서버가 실행마다 바뀌는 값으로 바꿔 준다. 화면은
  `<meta name="lms-agent-token" content="__LMS_AGENT_TOKEN__">`에서 읽어 **모든 `/api/*` 요청에 `X-LMS-Agent: <값>` 헤더**를 붙인다.
- 오류 응답은 2xx가 아니고 본문이 `{"error": "한국어 메시지"}`.

### `GET /api/state`

```json
{
  "app_version": "0.1.0",
  "now": "2026-11-12T16:30:00+09:00",
  "demo": false,
  "configured": true,
  "canvas_url": "https://canvas.example.ac.kr",
  "last_sync": "2026-11-12T16:20:00+09:00",
  "report": {
    "finished_at": "...", "courses": 4, "assignments": 5, "announcements": 5, "messages": 2,
    "syllabi": 1, "manual": 0, "candidates": 7, "needs_review": 5, "no_deadline": 1,
    "errors": ["메시지함을 읽지 못했습니다: 권한 없음 (403)"]
  },
  "settings": {"include_canvas": true},
  "courses": [{"id": "101", "name": "ASIC설계"}],
  "items": [ Item, ... ]
}
```

`report`와 `last_sync`는 동기화 전에는 `null`. `configured`가 false면 화면은 설정 폼을 먼저 보여 준다.

### Item

```json
{
  "id": "f-3a9c1e2b7d10",
  "origin": "found",                 // "canvas" | "found"(공지·메시지 등에서 찾은 것)
  "course_id": "101", "course_name": "ASIC설계",
  "title": "LAB4 보고서",
  "due": "2026-11-18T23:59:00+09:00", // null 가능(Canvas 과제에 마감이 없을 때)
  "kind": "과제",                    // "과제" | "시험"
  "method": "email",                 // "lms" | "email" | "paper" | "unknown"
  "email_to": "asic.ta@example.ac.kr",
  "confidence": "high",              // "high" | "medium" | "low" | null(canvas)
  "status": "review",                // "review" 검토 필요 | "scheduled" 일정에 들어감 | "duplicate" Canvas와 겹침
                                     // | "no_deadline" 마감 근거 없음 | "rejected" 제외함
  "decision": "pending",             // "approved" | "rejected" | "pending"
  "submitted": false,
  "submitted_source": null,          // "canvas" | "user" | null
  "duplicate_of": null,              // status=duplicate일 때 겹치는 Canvas item id
  "linked_to": "canvas-101-1012",    // 마감 없는 Canvas 과제와 연결된 후보면 그 item id
  "conflict": "같은 과제에 다른 마감(11/20 23:59)이 적힌 근거가 있습니다",  // 없으면 null
  "notes": ["시간 미기재 → 23:59로 둠"],
  "evidence": [
    {"text": "LAB4 보고서 마감: 11월 18일(수) 23:59", "source_type": "announcement",
     "source_title": "LAB4 보고서 제출 안내", "url": "https://.../announcements/9001",
     "posted_at": "2026-11-09T10:00:00+09:00"}
  ],                                 // source_type: "announcement"|"inbox"|"assignment"|"syllabus"|"manual"
  "url": null,                       // Canvas 과제면 html_url
  "edited": false                    // 사용자가 제목·마감을 고쳤는지
}
```

### 쓰기 요청 (모두 성공 시 새 `/api/state` 전체를 돌려줌)

| 요청 | 본문 |
|---|---|
| `POST /api/item/<id>` | `{"decision"?: "approved"\|"rejected"\|"pending", "submitted"?: true\|false\|null, "title"?: str\|null, "due"?: "YYYY-MM-DDTHH:MM"\|null}` (null은 원래 값으로 되돌림) |
| `POST /api/sync` | `{}` — 몇 초 걸릴 수 있음. 실패 시 502 `{"error"}` |
| `POST /api/manual` | `{"course_name": str, "text": str, "posted_at"?: "YYYY-MM-DD"}` — 붙여 넣은 메일·PDF 글에서 찾기 |
| `POST /api/settings` | `{"include_canvas": bool}` |
| `POST /api/setup` | `{"canvas_url": str, "token": str}` → 성공 `{"ok": true}`, 실패 400 `{"error"}` (이것만 state를 안 돌려줌) |

### `GET /calendar.ics`

승인한 후보 + (include_canvas면) 마감 있는 Canvas 과제 중 제외 안 한 것. 파일 다운로드(`lms-deadlines.ics`). 이 경로는 헤더 없이 받을 수 있다.

## 4. v0.2 학기 관리자 확장 (3장에 더해지는 것)

마감 목록 앱에서 "개강부터 종강까지" 보는 앱으로 넓힌다. 보는 단위가 **주(week)**와 **과목**이 된다. 3장의 필드는 그대로 두고 아래를 더한다.

### `GET /api/state` 추가 필드

```json
{
  "semester": {
    "start": "2026-09-01",           // 개강일(로컬 날짜). 과목/학기 시작일 → 없으면 항목 날짜 → 없으면 통상 학기
    "end": "2026-12-19",             // 종강일
    "source": "canvas",              // "canvas" | "items" | "default" — 화면은 default면 "추정" 표시
    "week_now": 11,                  // 오늘이 몇 주차인지. 개강 전 0, 종강 후 weeks_total+1
    "weeks_total": 16,
    "weeks": [
      {"n": 1, "start": "2026-08-31", "end": "2026-09-06"}  // 월~일. 1주차는 개강일이 든 주의 월요일부터
    ]
  },
  "courses": [
    {
      "id": "101",
      "name": "262R (서울-학부)ASIC설계(영강)(ASIC DESIGN(English))-00분반",
      "short_name": "ASIC설계(영강)",       // 화면에는 이것을 쓴다
      "current_score": 92.5,                // Canvas가 계산한 현재 점수(%). 학교가 숨기면 null
      "graded": 3, "assignments": 5         // 채점된 과제 수 / 전체 과제 수
    }
  ],
  "settings": {"include_canvas": true, "weekly_summary": true}
}
```

### Item 추가 필드

```json
{"week": 11, "course_short": "ASIC설계(영강)"}   // week는 due가 없거나 학기 밖이면 null
```

### 쓰기

- `POST /api/settings` 본문에 `"weekly_summary": bool`도 받는다.

### 캘린더 파일 (휴대폰용 요약, 선택 b)

`weekly_summary`가 켜져 있으면 일정이 1개 이상 있는 주마다 **월요일 08:00~08:30 요약 일정**을 하나 더 넣는다.
제목 `11주차: 마감 4 · 시험 1`, 설명에 그 주 일정 목록. UID `week-<월요일 날짜>@lms-agent`.

### 화면 구성 (PC 우선, 휴대폰 화면은 만들지 않음)

| 화면 | 사용자가 내리는 결정 | 들어갈 것 |
|---|---|---|
| 이번 주 (기본) | 오늘 무엇부터 할까 | 한 줄 결론, 오늘·이번 주 일정, 바뀐 것(충돌), 확인 대기 수 |
| 학기 지도 | 어느 주가 몰리나, 무엇을 당겨 할까 | 1~N주 가로 막대(마감 수), 시험 주 표시, 지금 주 표시, 주를 누르면 그 주 목록 |
| 과목 | 이 과목에서 놓친 게 없나 | 과목별 다음 일정, 지난 과제 제출 여부, 현재 점수, 최근 근거 글 |
| 확인함 | 찾은 마감을 일정에 넣을까 | 3장의 검토 카드(근거·충돌·추정 표시), 겹친 항목, 제외한 항목, 직접 붙여넣기 |
| 설정 | — | Canvas 연결, 캘린더 파일 옵션, 휴대폰 캘린더에 넣는 법 |

## 5. 바뀐 곳만 읽기와 근거 사슬 (M1·M2)

앱은 **켤 때마다**(서버가 뜨자마자 백그라운드로) 그리고 [동기화]를 누를 때 바뀐 곳만 확인한다.
공지·과제·강의계획서는 목록 응답에 본문이 같이 오므로 매번 받아 **내용 해시**를 비교하고, 바뀐 글에만 마감 찾기를 돌린다.
메시지는 `last_message_at`이 바뀐 대화만 본문을 받는다. 처음 실행·규칙 업데이트·[전체 다시 확인]일 때만 전부 읽는다.

### `POST /api/sync`
본문 `{"full"?: bool}` — true면 전체 다시 확인.

### `GET /api/state` 추가 필드

```json
{
  "syncing": true,                  // 켤 때 자동 동기화 중이면 true. 화면은 true인 동안 2초마다 /api/state를 다시 불러 갱신
  "sync_error": null,               // 마지막 자동/수동 동기화 실패 메시지(한국어) 또는 null
  "last_export": "2026-11-12T17:05:00+09:00",   // 마지막으로 캘린더 파일을 받은 시각(없으면 null)
  "runs": [                          // 최근 실행 기록, 최신 먼저(최대 10)
    {"finished_at": "...", "mode": "incremental",       // "incremental" | "full"
     "reason": null,                                     // full일 때 "처음 실행" | "규칙 업데이트" | "전체 다시 확인"
     "checked": 3, "skipped": 41, "requests": 9, "conversations_fetched": 1,
     "candidates": 12, "new_items": 2, "errors": [],
     "targets": [{"key": "announcement:9100", "type": "announcement", "title": "LAB5 안내",
                  "course_name": "...", "reason": "새 글", "found": 1}]}   // reason: 새 글 | 내용 바뀜 | 규칙 업데이트 | 처음 실행 | 전체 다시 확인
  ]
}
```
`report`(마지막 실행)에도 같은 `mode, reason, checked, skipped, targets, requests, conversations_fetched`와 `new_items`(새로 생긴 item id 목록)가 있다.

### Item 추가 필드 — 근거 사슬

```json
{
  "first_seen": "2026-11-12T16:20:00+09:00",   // 이 항목을 처음 발견한 실행 시각
  "decided_at": "2026-11-12T16:31:00+09:00",   // 사용자가 승인·제외한 시각(없으면 null)
  "exported_at": "2026-11-12T17:05:00+09:00",  // 마지막으로 받은 캘린더 파일에 들어 있었으면 그 시각
  "export_stale": false                        // 파일을 받은 뒤 제목·마감이 바뀌었으면 true → "다시 받아 가져오세요"
}
```
화면은 항목마다 **출처 글(evidence) → 찾은 문장 → 추정(notes) → 승인(decided_at) → 파일 포함(exported_at)** 을 한 줄로 보여 준다.
구글 캘린더에 실제로 들어갔는지는 앱이 알 수 없으므로 "파일에 포함됨"까지만 표시한다.

## 6. 구글 캘린더 자동 반영

권한은 `calendar.app.created`(앱이 만든 "LMS 마감" 캘린더만). 연결되면 동기화·승인·제외·수정·설정 변경 뒤 백그라운드로 자동 반영한다.
반영 대상은 캘린더 파일과 같고(승인된 마감 + 설정에 따라 Canvas 과제), 주간 요약이 켜져 있으면 요약 일정도 간다. 마감 일정은 길이 0(시작=끝=마감, 알림 1일·3시간 전).

### `GET /api/state` 추가

```json
"google": {
  "available": true,        // 클라이언트 ID가 있음(.env). false면 연결 버튼 대신 설정 안내
  "connected": false,
  "pushing": false,         // 반영 중이면 true → 화면은 잠시 뒤 다시 불러 갱신
  "error": null,            // 마지막 반영 오류(한국어)
  "last_push": "2026-11-12T17:40:00+09:00",
  "last_result": {"created": 9, "updated": 0, "deleted": 0, "unchanged": 0}
}
```

Item 추가(근거 사슬의 마지막 고리):
`gcal_event_id`, `gcal_updated`(구글이 돌려준 수정 시각), `gcal_link`(구글 캘린더에서 열기), `gcal_synced_at`(그 반영 시각). 마지막 반영에 들어 있지 않으면 모두 null.

### 요청

| 요청 | 동작 |
|---|---|
| `POST /api/google/connect` | `{"url": 구글 동의 화면}` → 화면은 그 주소로 이동(같은 탭). 동의 후 앱 루트로 돌아오며 서버가 `/?google=ok#settings` 또는 `/?google=error&msg=…#settings`로 보낸다 |
| `POST /api/google/push` | 지금 바로 반영(동기 실행). 실패 502 `{"error"}` |
| `POST /api/google/disconnect` | 토큰 폐기·삭제(구글 캘린더의 일정은 남김) |

## 7. 성적

`GET /api/state`의 `"grades"`:

```json
{
  "grades": ["A+","A0","B+","B0","C+","C0","D+","D0","F"],
  "default_dist": {"A+":15,"A0":15,"B+":15,"B0":15,"C+":15,"C0":20,"D+":0,"D0":0,"F":5},
  "courses": [{
    "course_id": "102", "short_name": "디지털신호처리", "canvas_score": 81.0,
    "config": {"components": [{"name":"중간고사","weight":30,"score":78,"max_score":null,"mean":65.3,"sd":12.1,"fixed":false,"source":"메시지 '중간고사 성적 안내'"}],
               "dist": null, "class_size": 48},
    "using_default_dist": true,
    "estimate": {"top": 16.9, "range": [12.1, 22.0], "grade": "A0", "grade_range": ["A+","B+"], "rank": 8,
                 "completed_weight": 50, "components": [{"name","weight","top","status"}], "notes": ["정규분포 근사…"]},
    "error": null,
    "needed": [{"component":"기말고사","target":"A+","possible":true,"already":false,"need_top":9.8,"need_score":84.2,"notes":[…]}]
  }],
  "priorities": [{"course":"반도체공학","score":1.9,"grade":"B+","reason":"A0/B+ 경계까지 가까움 · 남은 비중 50%"}],
  "candidates": [{"course_id":"102","course_short":"디지털신호처리","source_type":"inbox","source_title":"중간고사 성적 안내",
                  "posted_at":"…","url":null,"label":"중간고사","score":78,"max_score":null,"mean":65.3,"sd":12.1,
                  "high":null,"low":null,"sentence":"…","notes":[]}]
}
```

- `POST /api/grades/<course_id>` 본문 `{"components": [...], "dist"?: {9단계 %} | null, "class_size"?: int | null}` → 새 state. 잘못되면 400 `{"error"}`(비중 합 100% 초과, 분포 합 ≠ 100 등).
- 점수 후보(candidates)는 사용자가 [항목에 넣기]로 골라 넣는다(자동으로 넣지 않음). 넣을 때 `source`에 출처를 적는다.
- 화면은 추정이라는 단서(`estimate.notes`)를 항상 함께 보여 준다. D+/D0은 0%라 결과로 나오지 않는다.

## 8. 계속 쓰게 만들기: 백그라운드 동기화·튜토리얼 (v0.3)

### 백그라운드 동기화 — 앱을 열지 않아도 구글 캘린더가 최신

Windows 작업 스케줄러에 작업을 등록해 **로그인할 때 + N시간마다** `lms-agent sync --push`(바뀐 곳만 읽기 → 구글 반영)를 실행한다.
결과는 같은 실행 기록(`runs`)에 `"by": "background"`로 남는다. 앱 화면이 열려 있어도 서버는 요청마다 state.json이 바뀌었는지 보고 다시 읽는다.

`GET /api/state` 추가:

```json
"background": {
  "supported": true,            // 작업 스케줄러를 쓸 수 있는 OS(Windows)인지. false면 화면은 안내만
  "enabled": false,             // 등록돼 있는지(실제 스케줄러 조회 결과)
  "interval_hours": 3,          // 1~24
  "command": "\"C:\…\lms-agent.exe\" sync --push",   // 등록되는 명령(보여 주기용)
  "last_run": "2026-11-12T21:00:00+09:00",            // 마지막 백그라운드 실행(없으면 null)
  "error": null
}
```

- `POST /api/background` 본문 `{"enabled": bool, "interval_hours"?: 1~24}` → 등록/해제 후 새 state. 실패 시 400 `{"error"}`.
- 데모 모드에서는 실제 등록하지 않는다(`supported: false`, 안내 문구만).

### 튜토리얼 완료 여부

`state.settings.tutorial_done`(기본 false). 화면은 처음 열 때 false면 튜토리얼을 띄우고, 끝내거나 건너뛰면 `POST /api/settings {"tutorial_done": true}`.
설정의 [튜토리얼 다시 보기]는 화면에서만 다시 띄운다(값은 바꾸지 않아도 됨). 서버에 저장하는 이유: 포트가 바뀌면 브라우저 localStorage가 달라져 튜토리얼이 다시 뜨기 때문.

**튜토리얼 판 (v0.6.3)**: `state.settings.tutorial_version`(정수, 기본 0 — 이 칸이 없던 옛 파일도 0). `POST /api/settings`는 0~1000 정수만 받고,
참·거짓·문자열·소수·범위 밖이면 400(그 요청의 다른 설정도 바꾸지 않음). 저장된 값이 망가져 있으면 0으로 보여 준다.
화면의 판은 `TOUR_VERSION`(지금 2). 핵심 투어는 대시보드 → 확인함 → 근거 → 일정에 넣기 → **에이전트에게 묻기**(20장, 에이전트 칸이 없는 서버면 뺌) 5단계이고,
끝내거나 건너뛰면 `tutorial_done: true`와 함께 `tutorial_version: TOUR_VERSION`을 저장한다.
이미 튜토리얼을 마친(또는 넘긴) 사람(`tutorial_done` 참, `tutorial_version` < 판)에게는 첫 동기화·소식 카드 뒤에 **'달라진 점'**을 한 번 제안한다
(에이전트에게 묻기 · 대시보드 시험 일정(있을 때) · 성적의 점수 → 평가 항목 연결(성적 칸이 있을 때) · 과목의 P/NP·제외(과목 종류가 있을 때)).
보거나 [다음에]·건너뛰기를 누르면 `tutorial_version`만 올린다(화면별 팁 설정은 그대로). 설정 → 튜토리얼의 [달라진 점 보기]로 다시 볼 수 있다.
판을 올릴 때: 늘어난 기능을 `newSteps()`에 넣고 `TOUR_VERSION`을 1 올린다.

## 9. 로그인·로그아웃 (Canvas 토큰)

앱은 학교 포털 아이디·비밀번호를 받지 않는다(포털 비밀번호 자동 입력은 이번에 만들지 않기로 함 — PROGRESS 참고).
로그인 = Canvas의 토큰 발급 화면을 새 탭으로 열어 주고, 붙여 넣은 토큰으로 실제 연결을 확인. 토큰·구글 토큰은 설정 파일에 Windows 암호화로 저장.

`GET /api/state` 추가:

```json
"account": {
  "logged_in": true,
  "user_name": "김현빈",                         // Canvas가 알려 준 이름(연결 확인 결과)
  "canvas_url": "https://mylms.korea.ac.kr",
  "token_page": "https://mylms.korea.ac.kr/profile/settings",   // 새 액세스 토큰 발급·삭제 화면(새 탭)
  "encrypted": true                              // 토큰을 Windows 암호화로 저장하는지
}
```

| 요청 | 동작 |
|---|---|
| `POST /api/account/login` | `{"canvas_url"?: str, "token": str}` → Canvas에 실제로 물어봐 확인, 저장, 첫 동기화 시작 → 새 state. 실패 400 `{"error"}` |
| `POST /api/account/logout` | `{"wipe"?: bool}` → 이 PC의 토큰·로그인 상태 삭제(wipe면 마감·결정 기록까지) → 새 state. Canvas 쪽 토큰은 사용자가 `token_page`에서 직접 삭제하도록 안내 |

데모 모드에서는 둘 다 400. `/api/setup`(3장)은 그대로 동작한다(같은 확인·저장).

동기화 도중 로그아웃: 로그인·로그아웃은 `app.session_gen`을 올린다. `core.sync(..., still_valid=)`는 저장 직전(잠금 안)에
확인해 그 사이 로그아웃됐으면 아무것도 저장하지 않고 `core.SyncCancelled`를 낸다(지운 기록이 되살아나지 않게).
백그라운드 동기화(`sync --background`)는 설정 파일의 토큰이 시작할 때와 같은지로 판단한다.

### 확인 알림 (벤치마크 추천, 선택)

`state.settings.review_reminder`(기본 false). 켜면 확인 대기 후보가 있는 날 **오늘 09:00 "[확인] 마감 후보 N개 — LMS Agent에서 확인"** 일정이 캘린더(.ics·구글)에 하나 들어간다(설명에 후보 목록). 다 처리하면 사라지고, 지난날 것은 다음 반영 때 지워진다. `POST /api/settings {"review_reminder": bool}`.

## 10. LearningX 게시판·첨부 자료·평가 비중 자동 찾기·AI CLI (v0.4)

실제 고려대 계정 확인 결과: 과제 안내·마감은 Canvas 글이 아니라 **LearningX 게시판 본문**에, 자세한 내용은 **첨부 PDF**에 있다.
비밀번호 없이 Canvas 토큰만으로 접근한다: `learningx.LearningX` — Canvas `sessionless_launch` → LTI 1.1 양식 제출 → `xn_api_token` 쿠키(메모리에만) → `/learningx/api/v1/learningx_board/...`.
첨부 `url`은 Canvas 파일 주소(`/files/<id>/download?verifier=…`)라 **인증 헤더 없이** 받는다(저장소로 리디렉션). 실패하면 Canvas `GET /api/v1/files/<canvas_file_id>`의 `url`.

### 수집 결과(cache)에 더해지는 것 — 리더(core.collect)가 채운다

```json
"boards": [
  {"course_id": "95079", "tool_id": 5, "board_id": 83605, "board_title": "과제",
   "posts": [{"id": 461903, "title": "Lab3 Assignment", "content": "<p>…(due : 23:59 on 11/18)</p>",
              "created_at": "…Z", "updated_at": "…Z", "post_url": "https://…" ,
              "attachments": [{"id": 10001, "filename": "Lab3.pdf", "filesize": 635152,
                               "canvas_file_id": 6180826, "url": "https://…/files/6180826/download?download_frd=1&verifier=…"}]}]}
]
```
- 글 목록 응답에 `content`가 들어 있어 바뀐 글만 본문 해시로 다시 읽는다. 첨부는 `attachment_count > 0`이고 새·바뀐 글만 상세를 받는다(지난 cache의 attachments 재사용).
- 마감 찾기 출처: `type: "board"`, `id: "<board_id>-<post_id>"`, 제목 `"<게시판>: <글 제목>"`. 화면의 source_type 라벨은 "게시판".

### 첨부 자료(materials) — B

- `store.data["downloads"]`: `{"<course_id>:<attachment id>": {"path", "filename", "size", "course_id", "board_title", "post_title", "post_key": "board:<board_id>-<post_id>", "downloaded_at", "pdf_ok": bool|null, "pdf_notes": [...]}}`
- `store.data["pdf_texts"]`: `{"<course_id>:<attachment id>": {"text": "…(최대 60,000자)", "course_id", "filename", "post_key", "posted_at"}}` → 마감 찾기 출처 `type: "pdf"`, `id: "<course_id>:<attachment id>"`, 제목 `"첨부 '<filename>'"`(라벨 "첨부 PDF").
- 설정: `settings.auto_download`(기본 true), `settings.download_dir`(기본 null = `~/Documents/LMS Agent`). 저장 경로: `<폴더>/<짧은 과목명>/<게시판>/<글 제목>/<파일명>`(안전한 이름). 새 첨부만(같은 id·크기면 건너뜀), 파일당 50MB 제한.
- `GET /api/state` → `"materials": {"folder": "C:\…\LMS Agent", "auto_download": true, "count": 12, "items": [{"key", "course_short", "board_title", "post_title", "filename", "size", "downloaded_at", "pdf_ok", "post_key"}], "errors": [...]}`
- `POST /api/materials/open {"key"?: str}` → 그 파일(또는 key 없으면 폴더)을 OS 기본 프로그램으로 연다(다운로드 폴더 안의 경로만). `POST /api/settings`에 `auto_download`, `download_dir`.

### 평가 비중 자동 찾기·AI CLI — C

- 강의계획서·게시판·공지·PDF 글에서 "중간고사 30%, 기말고사 40%, 과제 20%, 출석 10%" 같은 평가 비중을 **규칙으로 먼저** 찾고, 못 찾으면(또는 애매하면) 사용자가 켠 경우에만 **AI CLI**(예: `claude -p`)로 표를 뽑는다.
- 찾은 비중은 그 과목 성적 설정이 비어 있으면 **자동으로 들어간다**(`config.components`에 `"auto": true`, `source`에 근거 문장). 사용자가 고친 과목은 자동으로 덮어쓰지 않는다.
- 설정 `settings.ai_cli`(기본 false). 켜면 강의 글 일부(한 번에 최대 12,000자)가 그 AI 서비스로 전송된다(화면에서 분명히 안내).
- 규칙: `weights.find_weights(text, is_html=False) -> WeightsFound{components:[{name, weight, fixed}], total, sentence, confidence, notes} | None`.
  한국어·영어 목록, `출석(10)`, 여러 줄 목록, 표(HTML·전치·합계 행·PDF에서 칸마다 한 줄), 배점(합이 100이 아니면 %로 바꾸고 메모).
  무시: 출석 기준, "상위 30%", 학점 분포, 감점, 날짜, 점수 공지, 하위 비중. 합 100이면 high, 아니면 medium(≥50%)·low. 출석·참여는 `fixed`.
- 자동 넣기: `gradebook.auto_fill(store, sources, ai_enabled, runner=None, session=None) -> [기록 줄]`. 규칙 먼저, high 중 **가장 최근 글**
  (학기 중 "Updated Grading Policy" 공지가 강의계획서를 대신), 과제 하나의 채점 기준은 건너뜀. high가 없고 AI가 켜져 있으면 AI(`grading_weights`) —
  글마다 결과를 기억하고 실패는 12시간 뒤 다시. 이름이 같은 항목의 점수·평균·표준편차는 유지. `POST /api/grades/<id>`로 저장하면 `auto` 표시가 빠져 다시는 자동으로 덮지 않는다.
- `state.grades.courses[]` 추가: `auto`, `auto_source`, `auto_by: "rules"|"ai"`, `auto_changed: {at, source, added:[{name, weight}], removed:[…], changed:[{name, from, to}]} | null`.
  과목 설정 추가: `auto_previous`, `auto_updated_at`, `auto_confidence`, `auto_notes`, `auto_source_type`, `auto_posted_at`.
- 성적 파일(학번): `POST /api/grades/settings {"student_id": str|null}`(영문·숫자 6~12자, 이 PC의 state.json에만). `state.grades.student_id_set`, `student_id_masked`("2026****33"; 전체 학번은 응답에 없음).
  `scorefile.parse_score_text(text, student_id, label=None, filename=None)`, `is_score_file`, `label_from_filename`("Lab0_score-1.pdf" → "Lab0").
  `gradebook.candidates()`가 성적 PDF의 내 줄도 후보로 낸다(`source_type: "pdf"`, `source_title`=파일 이름, `key: "pdf:<pdf_texts 키>"`).
- AI CLI 하네스(`agent_cli`):
  - `detect()`(PATH의 `claude`, `LMS_AGENT_AI_CLI`로 전체 경로 지정), `command_name(path)`. 작업 목록 `TASKS`: `grading_weights`, `deadline_from_text`, `triage_review`
    — 각각 지시문(한·영), JSON 스키마, 입력 만들기(가리기 + 12,000자 예산), 결과 검증, 2회 시도, 90초 제한.
  - `run_task(name, inputs, runner=, command=, store=) -> AiResult{ok, data, error, attempts, duration_ms, input_chars, cli, model, notes}`(예외 없음).
  - `AiSession(store, runner)`: `settings.ai_cli`일 때만, 동기화당 5회·하루 30회. 동기화 한 번의 평가 비중·확인함 정리가 한 세션을 나눠 쓴다(`core._after_sync`).
  - 호출: `claude -p <고정 한 줄 지시> --output-format json --tools "" --no-session-persistence`(모르는 옵션이면 뒤 두 개 빼고 한 번 더).
    글·스키마·고침 요청은 모두 stdin(명령줄에 강의 글 없음), 셸 없음, 빈 임시 폴더, 창 없음, 시간 초과 시 `taskkill /T`. 검증 실패는 오류를 인용해 다시 묻는다.
  - 가리기: 이메일·전화·학번(가린 것 포함)·링크·토큰 같은 문자열·서명 이름·넘긴 이름 → `[이메일]` 같은 자리 표시. 점수·성적 파일은 보내지 않는다.
  - 기록 `store.data["ai_runs"]`(최근 50): 작업·성공·시도·시간·글자 수·cli·model·오류만(지시·글·답 없음).
  - 평가: `run_evals(runner=None, command=None) -> {cli, passed, total, cases}` = `python -m lms_agent ai-eval`(데모 문장만 실제 CLI로).
- `state.ai = {available, command, enabled, auto_triage, last_error, calls_today, limits:{per_sync, per_day, max_chars}, recent_runs(최근 5, 내용 없음), last_triage}`.
  `POST /api/ai/settings {"ai_cli"?, "ai_auto_triage"?}`. 화면에는 명령 이름만(경로 없음).
- 데모: `mock_ai.runner`(규칙으로 답하는 가짜 AI)를 쓴다 — 실제 AI 서비스로 글을 보내지 않고 하네스 전체(가리기·검증·반영·기록)를 지난다.

## 11. 성적 공개 반응 — "학기 마라톤을 함께 달리는 페이스메이커" (v0.4)

사용자 아이디어: 성적이 공개되면 게임처럼 반응한다. LMS Agent는 학기 시작부터 끝까지 함께 달리는 마라토너다.

- 감지(동기화 뒤): ① Canvas 과제가 새로 채점됨(`submission.score`가 새로 생기거나 바뀜) ② 공지·메시지·게시판·첨부에서 점수 문장이 새로 발견됨(`gradebook.candidates`) ③ (가능하면) 과목 `current_score` 변화. 같은 것은 한 번만(`store.data["celebrated"]`에 본 키 저장).
- 판정: 평균·표준편차가 있으면 `grades.top_percent` → 그 과목 분포(없으면 기본)로 `letter_for` → 구간 `band` = A(A+·A0) / B(B+·B0) / C(C+·C0) / D / F. 과목에 추정(`gradebook` estimate)이 있으면 그 학점을 쓴다. 근거가 없으면 `band: null`(점수만 축하/알림, 등급 단정 금지).
- 맥락 `exam`: 라벨·제목에 중간/midterm → "midterm", 기말/final → "final", 그 밖 → "assignment".
- 마라톤 진행: `km = 42.195 × week_now / weeks_total`(소수 1자리), 종강 주면 결승선.

`GET /api/state` → `"celebrations": {"pending": [{"id", "kind": "graded"|"score", "course_short", "title", "score", "max_score", "mean", "sd", "top", "letter", "band", "exam", "source_title", "at"}], "km": 27.6, "total_km": 42.195, "week_now": 11, "weeks_total": 16}`
`POST /api/celebrations/seen {"ids": [...]}` → 본 것으로 표시 → 새 state.

화면: 동기화 뒤 pending이 있으면 한 번에 하나씩 반응 카드(모달). A는 호들갑(축하·색종이), B는 "너무 고생했어요! 조금만 더 다듬으면 돼요"(중간고사면 남은 기말 이야기), C는 위로와 격려, null은 담백한 "점수가 나왔어요". 기말 버전은 "한 학기 완주" 톤. 움직임 줄이기 설정을 존중하고, 성적은 이 PC에서만 다룬다.

### 11-1. 중간중간 보상 (목표: 사용자가 계속 쓰고 싶게, 종강까지 함께 달리기)

성적 반응만으로는 보상이 드물다. 학기 동안 **실제 행동(제출·확인)에 맞춘 작은 보상**을 준다. 과하지 않게(같은 날 최대 2개, 한 번 받은 것은 다시 안 뜸), 근거가 있는 것만.

| 보상 `kind` | 조건(같은 학기 안에서 한 번) | 예시 문구 톤 |
|---|---|---|
| `start` | 첫 동기화 완료 | "출발! 16주 마라톤, 끝까지 같이 달려요" |
| `km` | km 이정표 도달: 5, 10, 21.1(하프), 30(마의 구간 — 보통 기말 준비기), 35, 42.195(결승) | 하프: "반환점 돌았어요" / 30km: "가장 힘든 구간이에요. 페이스 유지!" / 결승: "한 학기 완주!" |
| `week_clear` | 지난 주에 마감이 1개 이상 있었고 **전부 제출함**(Canvas 제출 확인 또는 사용자가 체크) | "11주차 완주 · 마감 3개 모두 제출" |
| `streak` | `week_clear`가 연속 3·5·8주 | "3주 연속 완주 🔥"(이모지는 화면 아이콘으로) |
| `inbox_zero` | 확인함이 3개 이상 쌓였다가 0개가 됨 | "확인함 비우기 완료"(마감 모르는 과제가 남아 있으면 그 수를 함께, "놓친 마감 없음"이라고 하지 않음) |
| `graded` / `score` | 11장 성적 반응 | (11장) |

- 상태: `celebrations.pending`에 같은 모양(`kind`, `id`, `title`, 필요한 숫자)으로 섞여 들어오고, `celebrations.medals`: 받은 보상 목록 `[{"id","kind","title","at","week"}]`, `celebrations.streak`: 현재 연속 완주 주 수.
- 기록은 `store.data["celebrated"]`(본 키)와 `store.data["medals"]`(받은 보상). 처음 실행 때 지난 주들의 `week_clear`·`streak`은 조용히 메달함에만 넣고 모달로 띄우지 않는다(스팸 방지).
- 화면: 대시보드에 마라톤 진행 막대(현재 km, 다음 이정표까지 남은 km)와 메달함(받은 것 + 다음 목표 1개), 새 보상은 모달 대신 가벼운 카드/토스트로(성적 반응만 모달).
- **앱을 안 열어도 (v0.6.4)**: 백그라운드 동기화(`lms-agent sync --background`, `settings.notify` 기본 참, Windows)는 신규 마감 알림을 띄우지 않은 실행에서
  `celebrations.pending` 중 아직 알리지 않은 **보상 하나**(`km`·`week_clear`·`streak`·`inbox_zero`; 성적 반응·출발 인사는 넣지 않음 — 잠금 화면)를
  Windows 알림으로 띄운다: 제목 "LMS Agent · {title}", 본문 한 줄 + "앱을 열면 메달을 걸어 드려요." 알린 id는 `state.reward_toasts`(최근 100개)에 남겨 다시 알리지 않는다.
  화면의 카드·메달함은 그대로라(본 것으로 바꾸지 않음) 앱을 열면 보상을 받는다. 알림은 한 번에 하나(`notify.reward_message`).

### 11-2. 구현 결과 (D, `celebrate.py`·`features/celebrate.py`·`features/mascot.py`)

- 감지는 ①·②만(③ `current_score`는 ①과 겹쳐 두 번 뜨므로 뺐다). 첫 계산 때 14일보다 오래된 점수와 지난 주·km·연속 보상은 조용히 메달함으로(`start`만 보여 줌).
  하루 새 보상 최대 2개, 같은 종류가 여럿 쌓이면 가장 최근 것만 보여 주고 나머지는 메달함. `inbox_zero`는 **주 1회**(학기 1회 아님 — 계속 받을 수 있게), 사용자나 AI가 실제로 결정했을 때만(로그아웃 지우기로는 안 뜸).
- `state.celebrations`: `news[]`, `pending[]`(성적 반응·보상), `medals[]`, `streak`, `streak_next`, `km`, `total_km`, `week_now`, `weeks_total`, `finished`, `after_finish`,
  `milestones[{km, label, reached}]`, `next{km, label, left, title}`, `waiting`. 성적 항목 추가 필드 `course_id`, `basis: "score"|"course"|null`, `source_type`, `url`. 보상 추가 필드 `week`, `km`, `label`, `finish`, `n`, `count`.
- **동기화 소식 카드**(사용자 요청 2026-11-13: "동기화했을 때 새 정보가 있으면 가운데 큰 화면으로, 유머러스하게"): `news[] = {id: "news:<kind>:<hash>", kind, count, mood, items[]}`,
  `kind` = `first`(첫 동기화 요약 한 장) → `grades`(+`grade_ids`, `snoozed`) → `deadlines`(+`review_ids`) → `changes`(items에 `old`, `new`, `conflict`) → `materials` → `weights`(+`changed`, items에 `diff{added, removed, changed}`, `source`, `by`) 순서.
  성적 카드는 점수를 "??점"으로 가리고 [앗 쫄려요… 나중에 볼래요] / [지금 확인하기]. 미루면 다음 실행 때 한 번 더, 두 번 미루면 큰 카드 없이 배지만. 동기화당 한 줄, 소식이 먼저·보상은 그 뒤.
  이 기능 전부터 쓰던 계정은 조용히 초기화(옛 소식이 한꺼번에 뜨지 않게). [가 보기]는 뒤 화면을 옮기고 다음 카드로 넘어간다.
- 요청: `POST /api/celebrations/seen {"ids"}`, `POST /api/celebrations/snooze {"ids"}` → 새 state. `GET /api/state`가 첫 계산(초기화 표시)과 `review_peak` 변화 때 잠금을 잡고 저장한다.
- `store.data` 칸: `celebrated`, `medals`, `celebrate_init`, `review_peak`, `news_seen{init, items, materials, weights}`, `news_snooze`, `mascot{choice}`.
- 캐릭터 `state.mascot = {pack: "ku"|"local"|null, name, moods{mood: url|null}, local, guessed, files, folder, choice, ku, ku_school, credit}`.
  `GET /mascot/ku/<mood>.gif`(패키지 안), `GET /mascot/local/<rel>`(내 PC 팩), `POST /api/mascot/map {mood, file}|{name}|{choice: "auto"|"original"}`, `POST /api/mascot/open-folder`.
  고려대 Canvas(그리고 고려대를 흉내 낸 데모)면 KU 팩이 기본, 아니면 오리지널 SVG 페이스메이커(버건디 머리띠·배번 42, 8가지 자세). 설정 → 캐릭터에서 오리지널로 바꿀 수 있고, KU 저작권 안내를 함께 보여 준다.

## 12. AI 에이전트가 미결정 사안을 직접 반영 (사용자 결정, v0.4)

사용자 결정(2026-11-13): AI CLI는 챗봇이 아니라 **대시보드의 미결정 사안을 직접 처리**한다. 원칙 "판단은 사용자가"는 **"AI가 처리할 수 있으나, 보이게·되돌릴 수 있게·검증된 것만"**으로 바뀐다.

- 대상: 확인함 후보(status review) — `approved`/`rejected`/제목·마감 수정, 마감 충돌(같은 과제의 여러 후보 중 하나 고르기), 평가 비중 채우기.
- 하네스(agent_cli, C): 작업 `triage_review` — 입력은 후보마다 {id, 과목, 제목, 마감, 근거 문장들(원문), notes, conflict}만(이메일·학번·이름은 가린다). 출력 `[{"id", "decision": "approved"|"rejected"|"keep", "due"?: ISO, "title"?: str, "reason": 한 줄, "evidence": 원문에서 그대로 인용}]`. 검증: id가 실제 후보, evidence가 그 후보 근거 원문에 글자 그대로 있음, due는 파싱되고 근거와 모순 없음(±1일 밖이면 거부), 충돌 그룹에서는 최대 하나만 approved. 통과한 것만 반영.
- 반영: `store.update_user(id, changes, by="ai", reason=...)` → item에 `decided_by: "ai"|"user"|null`, `ai_reason`. 사용자가 그 뒤 손대면 `decided_by: "user"`. 되돌리기 = `POST /api/item/<id> {"decision": "pending"}`(기존).
- 실행: `POST /api/ai/triage` → 새 state + `"triage": {at, ok, applied, skipped: [{id, why}], error, attempts}`(꺼져 있으면 400). 설정 `settings.ai_auto_triage`(기본 false)가 켜져 있으면 동기화 뒤 자동 실행(`core._after_sync`, report에 `ai_triage: {ok, applied, error}`). 둘 다 `settings.ai_cli`가 켜져 있을 때만. 결과는 `store.data["ai_triage"]`(= `state.ai.last_triage`).
- 저장 합치기: AI는 몇십 초 걸릴 수 있어 잠금 밖에서 돈다. 저장할 때 파일을 다시 읽어, 그 사이 다른 프로세스(앱 화면)에서 **사용자가 바꾼 칸은 건드리지 않는다**(사용자 쪽이 이김, report `ai_skipped_user_changed: n`). 대상 칸: `user`, `grades`, `weights_auto`, 그리고 `ai_runs`·`ai_triage`. 그 사이 로그아웃됐으면 아무것도 저장하지 않는다.
- 추가 검증: AI가 결정하기 직전에 그 항목을 다시 확인(그 사이 사용자가 결정했으면 건너뜀), 이유 120자 이하, 이미 승인된 같은 과제 후보도 셈.
- 화면(D): 확인함 위 [AI로 정리] 버튼(진행 표시), AI가 반영한 항목에 "AI" 칩 + 이유 + [되돌리기], 설정에 "동기화 뒤 AI가 자동 정리" 토글, AI 실행 기록.

## 13. 처음 쓰는 사용자 리뷰 반영 (2026-11-13, v0.4.1)

독립 리뷰(데모, 처음 쓰는 고려대 학생 관점)의 결함을 고친다. 서버(E-서버)와 화면(E-화면)이 나눠 맡고, 아래 모양을 함께 따른다.

### 13-1. 마감 충돌 = 하나만 고르는 그룹 (리뷰 B2·B3)
- 같은 과제의 찾은 후보(`origin: "found"`, 같은 `linked_to`, 없으면 같은 과목 + 정규화한 제목 — `agent_cli._group_key`와 같은 규칙)는 **그룹**이다. `store.view()` Item 추가 필드:
  - `group`: 그룹 키 문자열 | null(후보가 하나뿐이면 null)
  - `alternatives`: `[{"id", "due", "status", "decision", "posted_at", "source_type", "source_title", "newest": bool}]`(자기 제외, 최신 글 먼저)
  - `newest`: bool — 이 후보의 가장 최근 근거가 그룹에서 가장 최근인지
  - `replaced_by`: id | null — 그룹의 다른 후보가 승인돼 있으면 그 id
- 상태 `status: "replaced"`(새 값): 그룹의 다른 후보가 승인됐고 이 후보는 따로 승인되지 않음 → 캘린더·확인할 것·충돌 수에서 빠진다. 승인한 후보를 되돌리면 다시 `review`.
- `POST /api/item/<id> {"decision": "approved"}`로 그룹의 한 후보를 승인하면, 같은 그룹에서 이미 승인돼 있던 다른 후보는 `pending`으로 돌린다(한 과제 = 캘린더 일정 하나). 응답 state에 `"replaced": [id…]`.
- `conflict`(문자열)는 view에서 다시 계산: 제외(`rejected`)·대체(`replaced`)된 후보는 세지 않고, 남은 후보의 마감이 2개 이상 다를 때만. 승인된 후보가 있으면 null.

### 13-2. 성적 표시의 근거 (B5·B6)
- `state.grades.courses[]` 추가: `weight_total`(항목 비중 합), `weight_missing`(100 − 합, 0 이상). 남은 평가·필요 점수·공부 우선순위는 `weight_missing > 0`이면 "남은 평가 없음"이라고 하지 않는다(`needed`/`priorities` 항목에 `missing_weight: n`).
- 성적 반응 항목(`celebrations.pending[]`, kind graded/score) 추가: `letter_basis: "user" | "default" | null`(학점 비율을 사용자가 정했는지, 기본값인지), `max_assumed: bool`(만점이 안 적혀 100점으로 가정). 화면은 `letter_basis == "user"`일 때만 학점을 단정하고, `"default"`면 "기본 비율이라면 A+" 식으로, `max_assumed`면 백분위만.

### 13-3. AI 이유는 계산을 보여 주고 원문을 인용 (B7, F11)
- `ai_reason`의 인용은 **가리지 않은 원문 문장**(이 PC 안에서만 보임)으로 바꿔 저장한다. `[이메일]` 같은 자리 표시가 화면에 나오면 안 된다.
- 상대 날짜("다음 주 금요일", "이번 주", "next Friday") 근거로 승인할 때 이유에 계산을 남긴다: `‘다음 주 금요일’ → 공지일 11/12(목) 기준 11/20(금)`. 하네스는 상대 날짜 근거에 "분명히 적혀"·"명시" 같은 주장을 하면 그 결정을 거절한다.
- `POST /api/ai/triage` 결과·`state.ai.last_triage` 추가: `pending_left`(아직 확인 대기), `user_reverted`(사용자가 되돌려서 AI가 건드리지 않은 수).

### 13-4. 마라톤은 '합류한 뒤 달린 거리'로 보상 (D1·D4·D5·B9)
- `celebrations` 추가: `joined_at`, `joined_km`(처음 계산한 시점의 km). `milestones[].before_join: bool`. 합류 전에 지난 km 이정표·주 완주·연속은 **메달로 주지 않는다**(기존 계정의 합류 전 메달은 `medals`에서 빼고 `before_join`으로만 표시). 출발(`start`)은 메달이 아니라 합류 인사(`kind: "start"`, 화면에서 "새 메달"이라 부르지 않음).
- 성적 소식 카드(`news[] kind "grades"`) 추가: `fresh: bool` — 첫 실행이거나 점수가 3일보다 오래됐으면 false → 화면 제목 "앱에서 아직 안 본 성적 N개".
- 첫 실행 요약 카드(`kind "first"`): `count_deadlines`, `count_materials`, `items`는 겹치지 않게 나눈다(`{"label": "일정에 넣은 마감"}`, `{"label": "확인할 후보"}`, `{"label": "받은 자료"}` — 앞 둘의 합 = `count_deadlines`).
- `inbox_zero`: 마감 없는 과제(`no_deadline`)가 남아 있으면 보상 문구에 그 수를 넣고 "놓친 마감 없어요"라고 하지 않는다(`pending[]` 항목에 `no_deadline_left: n`).
- 보상 이름은 배너·메달·토스트에서 같은 말을 쓴다(`title`이 기준).

### 13-5. 캘린더 수 맞추기 (B8)
- `state.google.desired = {"deadlines": n, "summaries": m}` — 구글에 있어야 할 일정 수(마감 n + 주간 요약 m). 화면은 "마감 n개 + 주간 요약 m개"로 보여 주고 추가·수정·삭제·유지는 그 합과 맞는다.

### 13-6. 학기 근거 (리뷰 2장)
- 화면은 `state.semester.source`(`canvas`/`items`/`default`)와 `note`를 학기 머리에 보인다. `default`면 "(추정)"이라고 분명히 쓰고, 그 학기 날짜로 계산한 km·메달도 추정임을 밝힌다.

### 13-7. 구현 결과 (서버·화면, v0.4.1)
- 그룹 키 `store.group_key(v)`(= `agent_cli._group_key`): `"link:<linked_to>"` 또는 `"title:<과목>:<공백 없앤 소문자 제목>"`. **제목 묶음은 제목이 과제 하나를 가리킬 때만**(번호 붙은 과제·중간·기말, `extract.title_tokens`) — "마감"·"공지" 같은 일반 제목은 묶지 않는다(하나 승인하면 상관없는 마감이 숨지 않게). 그룹이 없는 찾은 후보는 `conflict: null`. 예전에 둘 다 승인된 저장 상태는 `decided_at`이 늦은 쪽이 이기고 나머지는 `replaced`.
- `Store.release_group(id)`: 승인할 때 같은 그룹의 다른 승인을 `pending`으로. `POST /api/item/<id>` 응답에 항상 `"replaced": [...]`.
- 성적: `courses[]`에 `missing_note`(예: "나머지 70%는 평가 항목이 없어 계산 못 해요")도. `max_assumed`일 때 `max_score`는 null 그대로(100으로 바꾸지 않음).
- AI 이유 모양: `"<이유> · ‘다음 주 금요일’ → 공지일 11/12(목) 기준 11/20(금) — 근거: \"<원문>\""`(`agent_cli.ai_reason_text`, `original_quote`, `date_basis`, `claims_explicit`). 상대 날짜 근거에 "분명·명시·적혀 있" 주장 → 한 번 고쳐 묻고, 그래도면 `skipped`.
- 마라톤: 합류 시각 = `celebrate_init`(없으면 지금), `joined_km`은 지금 학기 주차로 다시 계산. 옛 저장의 합류 전 메달은 읽을 때 뺀다(저장은 그대로). `milestones[]`에 `title`도. 성적 카드 `fresh`는 3일 이내이면서 합류 뒤 점수일 때만.
- 첫 요약 카드 행: "일정에 넣은 마감"(다가오는 scheduled), "확인할 후보"(review 전부), "받은 자료".
- Item에 `orig_due`, `orig_title`(직접 고쳤을 때 원래 값, 아니면 null).
- 화면: 겹치는 창은 한 번에 하나(소식 → 튜토리얼 제안 → 튜토리얼 → "이제 앱을 안 열어도 돼요" 카드), 화면별 팁은 튜토리얼을 거절했을 때만. 충돌 고르기 창(`#pick-dlg`), 대체된 후보는 승인한 줄 밑에 접힘. 날짜 축은 자기 날짜 칸 안(23:59 = 칸의 85%).

## 14. 점수 → 평가 항목 연결과 세부 점수(LAB 등) (사용자 요청 2026-11-13, v0.5)

사용자: "LAB0, LAB1…이 사실 Homework assignment인데 서비스가 알아서 반영하려면? 강의계획서와 다른 항목이 어디에 해당하는지 반영하는 기능 + 총 몇 개의 LAB인지 어떻게 알까."
원칙: 규칙 → (켜져 있으면) AI → 사용자 순. 자동 연결은 근거와 함께 보이고, 사용자가 바꾸면 그게 이긴다. 평가 비중(`auto_fill`)의 자동 표시는 연결·개수 때문에 사라지지 않는다.

### 14-1. 점수 후보의 키
`gradebook.candidates()`의 모든 후보에 안정적인 `key`: 성적 PDF는 `"pdf:<pdf_texts 키>"`(기존), 글 속 점수 문장은 `"txt:<source_type>:<sha1(course_id|source_title|label|score)[:12]>"`.

### 14-2. 어느 평가 항목인지(연결)
- `gradebook.link_candidates(store, course_id, components, cands) -> {key: {"component": 이름|None, "by": "rules"|"ai"|"user", "reason": str, "evidence": str|None}}`.
- 순서: 사용자(`store.data["grade_links"][cid][key] = {"component": 이름|null, "at"}`, null = "이 과목 성적에 넣지 않음") → AI 기억(`store.data["grade_links_ai"][cid][key]`) → 규칙.
- 규칙: 후보 라벨의 **계열**(lab·실습 / hw·homework·과제·assignment / report·레포트·보고서 / quiz·퀴즈 / midterm·중간 / final·기말 / project·프로젝트 / attendance·출석)을 항목 이름의 낱말과, 비중 근거 문장(`auto_source`)의 `(w/ …)` 별칭(예: Homework assignments ← "short reports", Midterm project ← "midterm report")에 맞춘다.
  lab 계열은 이름에 lab·실습이 있는 항목 → 없으면 homework·hw·과제·assignment 항목 → 없으면 report 별칭 항목. midterm·final처럼 후보가 둘 이상이면(예: Midterm project / Midterm quiz) 라벨·출처 제목의 다른 낱말(project·quiz·exam·시험·report)로 가르고, 그래도 둘 이상이거나 없으면 `component: None`(화면: "어느 항목인지 골라 주세요").
- 다른 과목·과목 모르는 후보는 연결하지 않는다.
- AI 작업 `map_score_item`(agent_cli, 동기화 뒤 자동 단계에서 규칙이 못 정한 것만, AI가 켜져 있을 때): 답의 항목 이름이 목록에 있어야 하고 근거 인용이 보낸 글에 그대로 있어야 한다. 결과는 `grade_links_ai`에 기억.

### 14-3. 세부 점수(members)
- 연결된 후보는 그 항목의 `members`가 된다(상태를 만들 때 계산, 설정에 저장하지 않음 — 비중이 공지로 바뀌어도 다시 붙는다):
  `courses[].config.components[i].members = [{"key", "label", "score", "max_score", "mean", "sd", "n", "rank", "source_type", "source_title", "posted_at", "url", "link_by", "link_reason"}]`(라벨 번호 순). 같은 라벨이 여러 번(다시 공지)이면 가장 최근 것.
- members가 있는 항목은 점수를 직접 넣지 않는다: `components[i].derived = {"score_pct", "mean_pct", "sd_pct", "received", "basis": "per_item"|"average"}`. 기본은 세부 점수마다 같은 비중.

### 14-4. 몇 개인지(count)
- `components[i].count = {"n", "low", "high", "basis": "stated"|"hint"|"observed"|"user", "evidence", "source_title", "by": "rules"|"ai"|"user"} | null`.
- 규칙 — 명시(`stated`): "Lab 0~6", "Lab0-Lab6", "총 7개(의) 실습/랩/과제", "7 labs", "7 lab assignments", "과제 7회" → n. 단서(`hint`): 같은 글에서 가장 큰 LabK 뒤의 "Next labs: A → B → C", "다음 실습: A, B, C" → n = K + 1 + 목록 수(예: Lab3.pdf "Next labs: P&R → STA → SRAM" → 7).
  관찰(`observed`): 과목 글 전체(점수 라벨, 찾은 마감 제목, 게시판 글 제목, 받은 자료 파일 이름)에서 본 번호 → `low` = 0번이 있으면 최대 번호 + 1, 아니면 최대 번호, `high` = null.
- AI 작업 `count_items`(규칙이 명시·단서를 못 찾고 AI가 켜져 있을 때): 그 계열 낱말이 있는 줄들을 보내 {count|null, evidence}를 받는다. 인용이 그대로 있어야 하고, count는 관찰 `low` 이상 30 이하.
- 사용자: `POST /api/grades/<cid>/count {"component": 이름, "n": 정수|null}` → `store.data["grade_counts"][cid][정규화한 이름] = {"n", "at"}`(null = 지우기).
- 우선순위: 사용자 > 명시 > 단서(규칙) > 단서(AI) > 관찰.

### 14-5. 추정에 넣기
- `gradebook.expand_components(components)` → `grades.estimate`에 넣을 목록. members 없는 항목은 그대로.
  members 있고 n을 아는(관찰만이 아닌) 항목: 받은 세부 점수마다 `{"name": "<항목> · <라벨>", "weight": W/n, …점수}`, 남은 것 하나 `{"name": "<항목> 남은 <n−r>개", "weight": W·(n−r)/n}`(점수 없음 → 남은 평가).
  n을 모르면(관찰만): 받은 것의 평균 비율로 W 전체를 하나로(점수·평균 = 평균 비율, 표준편차 = 비율 평균), 메모 "남은 개수를 몰라 받은 것 평균으로 어림".
- 필요 점수·우선순위는 남은 가상 항목을 남은 평가로 본다. `courses[].remaining = [{"component", "weight", "count_left", "range": [lo, hi] | null}]`. 추정 메모에 개수 근거를 넣는다.

### 14-6. 요청
- `POST /api/grades/<cid>/link {"key", "component": 이름|null}` → 사용자 연결, `{"key", "reset": true}` → 사용자 연결 지우기(규칙·AI로 돌아감). 응답은 새 state.
- `POST /api/grades/<cid>/count` (14-4). 연결·개수는 `save_course`가 아니므로 평가 비중의 `auto` 표시를 지우지 않는다.

### 14-7. 학번 안내
- `state.grades.score_files_waiting`: 학번이 없을 때 받아 둔 성적 PDF 수(`is_score_file`), 학번이 있으면 0. `state.grades.score_files_unmatched`: 학번은 있는데 내 줄을 못 찾은 성적 PDF 파일 이름 목록.
- 화면: 성적 화면 맨 위 안내("점수표 PDF 2개가 있어요. 학번을 넣으면 내 점수를 찾아 드려요" + 학번 칸), 설정에도 같은 학번 칸. 못 찾은 파일이 있으면 "학번을 확인해 주세요".

### 14-8. 실제 데이터 기준(ASIC, 95079)
학번을 넣으면 Lab0·Lab1 성적 PDF → "Homework assignments"(30%)의 members 2개, count n=7(단서: Lab3.pdf "Next labs: P&R (IC Compiler II) → STA (PrimeTime) → SRAM"), 남은 5개 = 30%×5/7 ≈ 21.4%. 학번이 없어도 count는 보인다(관찰 low 4 = LAB 0~3, 단서 7).

### 14-9. 평가 항목의 일정(시험 일정) (사용자 제보 2026-11-13: "ASIC 퀴즈 같은 시험 일정이 어디에 있는지 안 보인다")
- 찾은 마감·시험 항목(Item)도 14-2와 같은 규칙(→ AI → 사용자)으로 평가 항목에 연결한다. Item 추가 필드: `component`(이름|null), `component_by`("rules"|"ai"|"user").
  같은 계열 항목이 둘 이상이면(Midterm quiz / Final quiz) 근거 문장의 midterm·중간 / final·기말 낱말, 없으면 학기 앞쪽 절반(`semester.week_now` 기준 주차 ≤ 주 수/2)인지로 가른다. 실제 예: 공지 "short quiz session (+ explanation on midterm project) … on Oct. 22" → Midterm quiz.
- 사용자 연결: `POST /api/item/<id>/component {"component": 이름|null}`(같은 그룹 키 방식으로 `store.data["item_links"][item_id]`).
- `courses[].config.components[i].schedule = [{"item_id", "title", "due", "kind", "status", "past": bool}]`, `date_status: "announced" | "past" | "not_announced"`(연결된 항목이 없으면 not_announced).
- `state.exams = {"upcoming": [{"course_id", "course_short", "component", "weight", "title", "due", "item_id", "status"}], "not_announced": [{"course_id", "course_short", "component", "weight"}]}` — 시험 계열(quiz·exam·시험·고사·중간·기말 중 project/report가 아닌 것) 평가 항목만. 지난 시험은 upcoming에서 뺀다.
- 화면: 대시보드에 "시험 일정" 칸(다가오는 시험 + "날짜 미공지: ASIC Final quiz 5%"), 과목 화면에 과목별 "평가 지도"(항목 | 비중 | 일정 | 받은 점수 | 개수), 학기 화면의 시험 마름모는 그대로.

## 15. 과목 종류: 성적 있음 / P/NP / 제외 (사용자 요청 2026-11-13: "테니스는 패논패, 인권과 성평등은 실제로 듣는 수업이 아니라 제외 기능")
- `POST /api/course/<cid>/mode {"mode": "normal" | "pass_fail" | "hidden"}` → `store.data["course_prefs"][cid] = {"mode", "at"}`. 응답은 새 state.
- `state.courses[]` 추가: `mode`(기본 "normal"), `mode_suggest: {"mode", "reason", "evidence"} | null`.
- **제외(hidden)**: 항목은 계속 만들되(다시 보이면 바로 돌아오게) 대시보드·확인함·학기·캘린더(.ics·구글 — 다음 반영 때 그 과목 일정은 지워짐)·성적·우선순위·소식 카드·보상(주 완주 계산)·백그라운드 알림·AI 정리 대상에서 뺀다. 첨부 자동 다운로드도 새로 받지 않는다(받은 파일은 둠). 과목 화면 아래 "제외한 과목"에 [다시 보기].
- **P/NP(pass_fail)**: 마감·일정은 그대로. 성적: 학점·상위 % 추정을 하지 않고 "P/NP 과목 — 학점 추정 안 함"(점수는 보여 줌), 공부 우선순위에서 뺀다, 성적 반응은 등급 없이 담백하게(`band: null`).
- 제안(근거 있을 때만, 사용자가 [맞아요]로 정함 — 저절로 바꾸지 않음):
  - hidden: 과목 코드·이름에 학기 표시가 없고(다른 과목들이 공통으로 가진 "262R (서울-학부)" 같은 앞머리가 없음) `[학생]` 같은 대괄호 머리가 있거나 '교육'·'training'으로 끝나며, 공지·게시판·강의계획서가 하나도 없을 때. 실제 예: "[학생] [한국어] 2026 인권과 성평등 교육 - 03분반" → "정규 수업이 아닌 것 같아요(과목 코드에 학기 표시가 없고 공지·게시판이 없음)".
  - pass_fail: 강의계획서·공지·게시판·PDF에 "P/NP", "Pass/Fail", "Pass/Non-pass", "패스/논패스", "합격/불합격", "S/U"가 있을 때(근거 문장 인용). 실제 테니스는 그런 글이 없어 제안하지 않는다(사용자가 직접).

### 14-10. 구현 결과 (서버·화면, v0.5)
- 규칙은 `links.py`(계열·별칭·연결 `match`·시험 항목 `is_exam_component`·개수 `stated`/`hint`/`observed_low`), 과목 종류는 `course_modes.py`, 연결·세부 점수·개수·일정·시험은 `gradebook.py`(`link_candidates`, `members_of`, `derived_of`, `expand_components`, `remaining_of`, `link_items`, `exams_of`, `set_link`, `set_count`, `set_item_component`, `ai_links`). `section()`은 설정의 깊은 사본으로 계산(저장값을 바꾸지 않음).
- 단서 개수: 0번을 본 과목이면 n = K + 1 + 목록 수, 아니면 n = K + 목록 수(1번부터 세는 과목). 화살표는 `→`와 `->` 둘 다. "7 labs" 꼴은 복수형만(실제 강의계획서 시간표 "8 Report"·"10 Project1 Presentation"을 개수로 읽지 않게), 이름에 숫자가 있는 항목(Project1)은 세지 않음.
- `count`: `low`는 늘 관찰 최솟값, n을 알면 `high = n`, 관찰만이면 `high: null`. 추가 `seen`(본 번호), `family`. 관찰만인 `remaining`: `weight: null`, `count_left: null`, `range: [W·(low−r)/low, W]`.
- 펼친 가상 항목 이름 "<항목> · <라벨>", "<항목> 남은 N개" — `needed[]`에 `parent`. 비중 표시는 소수 한 자리(계산은 그대로).
- 추가 필드: `grades.courses[]`의 `links`, `mode`, `pass_fail`, `pf_note`("P/NP 과목 — 학점 추정 안 함"); `candidates[]`의 `component`, `link_by`, `link_reason`, `link_evidence`, `n`, `rank`; Item의 `component_reason`.
- 연결 규칙 추가: Item이 시험이면 시험 계열 항목, 과제면 그 밖을 먼저. 라벨에 midterm·final이 없으면 그 낱말이 없는 항목을 먼저("Report 2" → Homework assignments).
- `score_files_*`는 파일 이름에 score·점수·성적·grade가 있는 것만(내용만으로 고르면 실제 `Midterm_project.pdf`가 걸림).
- 과목 종류: 사용자가 한 번이라도 고르면(성적 있음 포함) 제안을 멈춘다. P/NP 제안은 같은 줄에 성적 낱말이 있을 때만(테스트벤치의 "PASS/FAIL" 제외). 제외 제안은 그 과목 자체의 학기 머리 유무로 판단.
- 제외: `Store.view(include_hidden=False)`가 기본으로 숨긴 과목을 뺀다 → 대시보드·확인함·학기·캘린더(.ics·구글)·알림·AI 정리가 함께 빠진다(저장된 항목·결정은 그대로). `POST /api/course/<cid>/mode`는 "changed"를 내보내 다음 구글 반영에서 그 과목 일정을 지운다. 학기 날짜 계산(중앙값)은 거르지 않는다.
- 자료: 공지 첨부 묶음의 화면 이름 `board_title` = "공지 첨부 파일", 디스크 폴더 이름은 `board_folder`("공지")로 그대로. `state.courses[].read_counts` — `board_posts`는 공지 첨부 묶음 제외, `attachments`는 포함.
- 데모: ASIC에 Updated Grading Policy·성적 PDF 2개(가짜 학번)·퀴즈 공지·Lab6.pdf "Next labs: …", Capstone에 P/NP 문장, 과목 105 "[학생] [한국어] 2026 연구실 안전 교육 - 01분반"(제외 제안), 데모만 가짜 학번 2026000002.
- 화면: 성적 표의 세부 점수 줄·연결 칩(`openChooser`, `#pick-dlg` 재사용, 되돌리기)·개수 줄(고치기)·남은 비중, 학번 안내 띠(`sidBanner`)와 설정 → 성적 칸, 대시보드 "시험 일정"(`examSection`), 과목 "평가 지도"(`gradeMapBlock`), 과목 종류 단추(`modeControl`)와 제안 칩, 제외한 과목 묶음, 과목별 읽은 글 수.

## 16. 학점 비율 자동 찾기 (사용자 제보 2026-11-13: "로공개 학점 비율도 강의자료에 나온 거로 기억")

원칙(사용자): **특정 과목·문장에 맞추지 않고, 다른 학생의 어떤 과목이 와도 처리되게** — 일반 구조(학점 낱말 묶음·범위·누적·이하/이내·점수 기준표)로 읽고, 변형 문장으로 시험한다. 못 읽는 꼴은 (켜져 있으면) AI → 사용자.

- 실제 예(로봇공학개론 Syllabus.pdf): "Grade (Guideline) / A+,A: 40~50% / B+,B: 40~60% / C+~F: 0~10%" — 학점 묶음(A+,A), 범위(40~50%), 꼬리 묶음(C+~F).
- `distribution.find_distribution(text, is_html=False) -> DistFound | None`:
  `{"kind": "relative" | "absolute", "dist": {9학점: %} | None, "ranges": {묶음: [lo, hi]}, "cutoffs": {학점: 점수} | None, "sentence", "confidence", "notes"}`.
  - relative: 묶음별 %(단일값·범위·이하/이내·최대·누적 "B 이상 70%"·"A+B ≤ 70%")를 9학점 비율로. 범위는 가운데 값을 쓰되 `ranges`에 그대로 남긴다. 묶음 안(A+/A0)은 고르게 나누고 메모. 합이 100이 아니면 메모(누적이면 차이로 계산).
  - absolute: "A: 90점 이상", "90 ≤ A+", "A0 85~89" 같은 점수 기준표 → `cutoffs`. 이때 순위가 아니라 예상 총점으로 학점을 정한다(`grades.letter_for_score`).
  - 무시: 평가 비중(중간 30%), 출석 기준("66% 이상 출석"), 통계 소수, 글 속 일반 퍼센트.
- 자동 넣기: `gradebook` — 사용자가 정한 비율(`config.dist`) > 자동 비율(`config.dist_auto = {dist|cutoffs, kind, ranges, source, source_type, posted_at, confidence, notes}`, 가장 최근 글이 이김) > 기본값. 사용자가 학점 비율을 저장하면 자동은 쓰지 않는다.
- 학점 표시 근거: `letter_basis = "user" | "syllabus" | "default" | null`(13-2 확장). "syllabus"면 화면이 학점을 보여 주되 근거 문장과 범위(예: "강의계획서 가이드라인 A 40~50%의 가운데로 계산")를 함께. `courses[].dist_source = {"by": "user"|"syllabus"|"default", "sentence", "source_title", "ranges"}`.
- AI 작업 `grade_distribution`(규칙이 못 읽고 AI가 켜져 있을 때): 결과는 합·범위 검사와 원문 인용 검사를 통과해야 한다.
- 실제 기준(로봇공학개론): kind relative, A(A+·A0) 45%(40~50), B(B+·B0) 50%(40~60), C+~F 5%(0~10), `letter_basis: "syllabus"`.

## 17. HTTPS 인증서 (베타테스터 제보 2026-11-13: "CERTIFICATE_VERIFY_FAILED … self-signed certificate in certificate chain")

원인: 백신의 'HTTPS 검사'(웹 보호)나 학교·회사·기숙사 네트워크 장비가 연결을 가로채 자기 루트 인증서로 다시 서명한다. 브라우저는 그 루트를 믿지만 파이썬은 못 볼 수 있다.
- `net.py`가 모든 외부 HTTPS(Canvas `canvas.py`, LearningX `learningx.py` 두 연결, 구글 `gcal.py`, 첨부 다운로드 `core._attachment_bytes`)의 공통 설정을 만든다: `ssl.create_default_context()`(Windows 인증서 저장소 포함) + macOS 키체인(`security find-certificate`로 내보내 더함) + 사용자가 넣은 루트 인증서(`<데이터 폴더>/certs/*.pem|.crt|.cer|.der`, 환경 변수 `LMS_AGENT_CA_FILE`; PEM·DER 둘 다).
- 인증서 검증을 끄는 선택지는 없다(끄면 토큰이 가로채일 수 있다).
- 검증 실패는 `net.cert_help(host, reason, service, port)`로 고칠 수 있는 말이 된다: 어느 서버인지, 인증서 발급자(`net.peer_issuer` — 검증 없이 TLS 인사만 하고 끊어 발급자 이름만 읽음, 아무 데이터도 보내지 않음), 원인(HTTPS 검사), 방법(① 그 프로그램의 HTTPS 검사 끄기 ② 루트 인증서를 certs 폴더에 넣고 다시 연결), 읽지 못한 인증서 파일 이름. 토큰·개인 정보는 넣지 않는다.
- `POST /api/account/login`은 연결 전에 `net.reset()` — certs 폴더에 넣고 다시 [연결]하면 바로 읽는다.
- **2차 제보(발급자 'GlobalSign … GCC R3 DV TLS CA 2020' — 학교 서버의 정상 인증서)**: 가로채기가 아니라 이 PC 운영체제 목록에 서버가 보낸 루트(GlobalSign Root R3)가 없던 것(Windows는 루트를 필요할 때 받아 오는데 크롬은 자기 목록을 써서 받지 않았을 수 있음). 그래서 앱에 공개 루트 목록 `src/lms_agent/data/cacert.pem`(Mozilla, certifi 2024.08.30, MPL-2.0 — `data/NOTICE.md`)을 넣고 운영체제 목록에 더해 믿는다(패키지 데이터·exe에 포함). 루트가 하나도 없을 때의 오류가 제보와 같은 'self-signed certificate in certificate chain'이고, 앱 목록만으로 고려대 LMS가 확인됨을 실제로 확인했다.
- 안내 문구는 발급자로 갈린다: `net.is_public_ca(issuer)`(GlobalSign·DigiCert·Sectigo·Let's Encrypt 등)면 "정상 공개 인증서인데 이 PC 목록에 루트가 없음 → Edge로 한 번 열기 / 업데이트 / certs 폴더", 아니면 "HTTPS를 검사하는 프로그램·장비 → 검사 끄기 / 루트를 certs 폴더에".
- 백그라운드 동기화 실행 스크립트(`<데이터 폴더>/background-sync.vbs`)는 켠 적이 있으면 앱을 켤 때마다 지금의 파이썬·exe 경로로 다시 쓴다(파이썬을 다시 설치하거나 exe로 바꿔도 멈추지 않게).
- 시험: `tests/test_net.py` — openssl로 만든 가짜 '검사 장비' 루트 CA와 그 CA가 서명한 127.0.0.1 서버로 실패(발급자 이름이 담긴 안내) → certs 폴더에 .cer을 넣으면 성공까지(openssl이 없으면 건너뜀).

## 18. 열마다 다른 평가인 점수표 (베타테스터 제보 2026-11-13: "성적 자동 입력이 안 된다")
- 'Quiz 1 (10) | Midterm (100)'처럼 열마다 다른 평가인 표는 열마다 결과 하나(`scorefile.parse_score_results`, 이름 = 열 이름과 번호, `ScoreFileResult.key`), 합계 열이 있거나 다른 열 만점의 합이 한 열의 만점인 표(ASIC Lab0)는 결과 하나(기존과 같음). 자세한 판단은 `docs/CORNER_CASES.md`.
- `gradebook.score_file_candidates`가 결과마다 후보 하나: key `pdf:<첨부 키>#<열>`(결과가 하나면 `pdf:<첨부 키>` 그대로 — 기존 연결 유지). 열 이름으로 평가 항목에 자동 연결되므로 파일 이름이 '성적.pdf'여도 들어간다.

## 19. 업데이트: 새 버전 확인과 한 번 누르기 업데이트 (사용자 요청 2026-11-13: "옛 버전을 쓰는 사람이 새 버전을 쉽게")

`updater.py`(확인·체크섬·백업·교체·다시 시작), `migrate.py`(state.json 맞추기), `features/update.py`(경로·상태 칸).
릴리스 쪽은 `.github/workflows/release.yml`이 태그마다 `SHA256SUMS.txt`(`sha256sum` 형식, 실행 파일 3개 + wheel)를 함께 올린다.

### 19-1. 확인
- 앱을 켤 때(`lms-agent serve`) 하루 한 번(실패하면 3시간 뒤 다시) + 설정의 [지금 확인]. 화면 서버는 한 시간마다 '하루가 지났는지'만 본다.
  데모·테스트의 `App`은 저절로 확인하지 않는다(`start_background_checks`는 cli만 부름). 첫 확인은 켜고 3초 뒤(v0.6.3 전에는 8초).
- **화면이 늦게 알지 않게 (v0.6.3)**: 저절로 확인하는 실행(`Updater.auto`, `start_background_checks`가 켬)에서 확인할 차례(`due()`)면 `update.pending: true`.
  화면은 이 값이 참인 동안 `GET /api/update`(업데이트 칸만)를 3초마다(최대 20번) 다시 읽어 띠를 고친다. 오래 열어 둔 화면은 30분마다,
  탭을 다시 볼 때(10분 넘었으면) 한 번 더 읽는다. v0.6.2까지는 켤 때 동기화가 확인보다 먼저 끝나면 띠가 다음 동작·다음 실행 때 떴다.
- 요청은 `GET https://api.github.com/repos/hbean00/LMS_agent/releases/latest` 하나. 헤더는 고정 `User-Agent: lms-agent-update-check`뿐(토큰·이름·학교·버전 없음).
  초안·미리 보기 릴리스는 GitHub이 빼고 준다. 버전은 `tag_name`(`vX.Y.Z`)을 숫자로 비교(`0.10.0 > 0.9.9`, `0.6.0-rc1 < 0.6.0`).
- 인터넷 없음·GitHub 제한(403 + `X-RateLimit-Remaining: 0`, 429)·서버 오류는 조용히 `error`에만 남기고 지난번 답(`latest`)은 그대로 둔다. 404 = 공개된 버전 없음(오류 아님).
- 설정 `settings.update_check`(기본 true): 끄면 저절로 묻지 않는다([지금 확인]은 눌렀을 때 한 번 묻는다).
- 캐시 `store.data["update"] = {"checked_at", "latest": "0.6.0", "tag": "v0.6.0", "notes_url", "notes"(본문 앞 1,500자), "published_at",
  "assets": [{"name", "url", "size"}], "error", "dismissed"?: "0.6.0", "applied"?: {"version", "from", "at", "how", "seen"?}}`.

### 19-2. `GET /api/state` 추가

```json
"update": {
  "current": "0.5.0",            // 지금 도는 버전
  "latest": "0.6.0",             // 확인한 최신(모르면 null)
  "available": true,             // latest > current
  "notes_url": "https://github.com/hbean00/LMS_agent/releases/tag/v0.6.0",   // [바뀐 점 보기]
  "notes": "- 앱 안 업데이트 …",  // 릴리스 본문 앞부분(없으면 null)
  "published_at": "2026-11-20T01:00:00Z",
  "how": "exe",                  // "exe" | "pip" | "git" | "unknown" (sys.frozen → exe, site-packages 안 → pip, src/ 위에 .git → git)
  "can_apply": true,             // [지금 업데이트]로 될 때(데모는 늘 false)
  "reason": null,                // 안 될 때 이유(예: "이번 릴리스에 확인값 파일(SHA256SUMS.txt)이 없어 …")
  "command": null,               // 직접 할 명령(pip·git·unknown). exe는 null → notes_url에서 받기
  "checked_at": "2026-11-20T09:00:00+09:00",
  "error": null,                 // 확인 실패, 또는 "지난번 업데이트가 적용되지 않았어요 …"
  "phase": "idle",               // "idle" | "checking" | "applying" | "restarting"
  "pending": false,              // 곧 저절로 확인한다(켠 직후 등, 19-1). 데모는 늘 false
  "result": null,                // 이 실행에서 적용한 결과 {"ok", "message", "restart"}(pip·git 성공이면 restart: true → [다시 시작])
  "dismissed": false,            // 이 버전 알림을 닫았는지(더 새 버전이 나오면 다시 false)
  "updated": null,               // 방금 바뀐 경우 {"from": "0.5.0", "to": "0.6.0", "at"} — 닫을 때까지
  "data_newer": null             // state.json을 더 새 버전이 저장했으면 그 버전(옛 exe로 돌아간 경우)
}
```
`settings.update_check`(기본 true)·`settings.auto_update`(기본 false)는 `state.settings`에 있고 `POST /api/settings`로 바꾼다.

### 19-3. 요청 (성공하면 새 state, 실패는 4xx `{"error"}`)

| 요청 | 동작 |
|---|---|
| `GET /api/update` | `{"update": …}`(19-2와 같은 칸만, 가볍게). 세션 값 필요 |
| `POST /api/update/check` | 지금 GitHub에 묻는다(몇 초). 결과는 `update.latest`·`error` |
| `POST /api/update/apply` | 백그라운드로 적용 시작 → `phase: "applying"`. 데모·최신·`can_apply: false`면 400, 이미 하는 중이면 409 |
| `POST /api/update/restart` | pip·git 설치를 마친 뒤(`result.restart`) 새 버전으로 다시 켜기 → `phase: "restarting"`. 그 밖에는 400 |
| `POST /api/update/dismiss` | 배너 닫기: 새 버전 알림이면 `dismissed = latest`, 방금 업데이트 알림이면 `applied.seen`, 실패 결과면 지움 |

적용(언제나 사용자가 누를 때만. 자동은 19-5):
1. **exe**: 이 OS의 파일(`lms-agent-windows.exe`·`lms-agent-macos`·`lms-agent-linux`)과 `SHA256SUMS.txt`를 받아 SHA-256이 맞을 때만
   실행 파일 옆에 `<이름>.new`로 저장(디스크에서 한 번 더 확인) → state·config 백업 → `applied` 기록 → `phase: "restarting"`(화면이 볼 틈 1.2초) →
   실행 중인 파일을 `<이름>.old`로 이름 바꾸고 `.new`를 그 자리로(실패하면 되돌림) → 포트를 비우고 같은 포트·`--no-browser`로 다시 켬
   (Windows는 새 콘솔 창, macOS·Linux는 `exec`로 같은 터미널). 같은 경로라 작업 스케줄러의 백그라운드 동기화(.vbs)도 그대로 새 버전을 쓴다.
   `.old`는 다음에 켤 때 지운다. Windows에서 파일이 잠겨 이름을 못 바꾸면 숨은 PowerShell이 이 프로세스가 끝난 뒤 바꾸고 다시 켠다.
   PyInstaller 환경 값(`_MEIPASS2`·`_PYI_*`)은 지우고 `PYINSTALLER_RESET_ENVIRONMENT=1`로 켠다.
2. **pip**: 릴리스의 wheel(`lms_agent-*-py3-none-any.whl`)을 같은 방식으로 확인 → 백업 → `python -m pip install --upgrade --no-deps <받은 파일>`
   (셸 없이, 5분 제한) → `result.restart: true` → 사용자가 [다시 시작]. wheel·체크섬이 없으면 명령만 보여 준다.
3. **git**: `git status --porcelain --untracked-files=no`가 비어 있을 때만 백업 → `git pull --ff-only`(비밀번호를 묻지 않게 `GIT_TERMINAL_PROMPT=0`) → [다시 시작].
   고친 파일이 있으면 아무것도 하지 않고 명령을 알려 준다.
4. **unknown**: 릴리스 주소와 `python -m pip install -U git+https://github.com/hbean00/LMS_agent@<tag>`만 보여 준다.

보안: 연결은 공통 HTTPS 설정(`net.py`, 17장 — 운영체제·사용자 루트 인증서, 실패하면 `net.cert_help` 안내)을 쓴다. 주소는 https만, 리다이렉트는 `github.com`·`api.github.com`·`objects.githubusercontent.com`·`release-assets.githubusercontent.com`으로만 따라간다.
체크섬이 맞지 않으면 아무것도 저장·실행·교체하지 않는다. 체크섬은 같은 릴리스에 있으므로 '받는 도중 바뀐 파일'은 막지만 '릴리스 자체가 바뀐 경우'는 못 막는다
(다음 단계 후보: 코드 서명 또는 앱에 넣은 ed25519 공개 키로 서명한 SHA256SUMS).

화면은 `phase`가 checking·applying이면 2초마다 state를 다시 묻고, restarting이 되거나(exe) 응답이 끊기면 `GET /`을 1초마다 불러
세션 값(`lms-agent-token`)이 바뀐 화면이 오면 새로 고친다(2분 넘으면 "직접 다시 켜 주세요").

### 19-4. 백업과 state.json 맞추기
- 업데이트 직전: `state.json`·`config.json`을 같은 폴더에 `state.before-<새 버전>.json`·`config.before-<새 버전>.json`으로 복사(권한 그대로, 덮어씀).
- `state.json`에 `app_version`(마지막으로 저장한 앱 버전)을 둔다. 0.6.0 전 파일에는 없다.
- 옛 버전이 저장한 파일을 처음 읽을 때 `state.before-<이 버전>.json`이 없으면 만든다(직접 pip·git으로 바꾼 경우도 백업이 남게).
- `migrate.MIGRATIONS`: `@migration("<버전>")` 단계 중 `app_version`보다 새 것만 차례로(여러 번 돌아도 같은 결과). 0.6.0 단계는 최상위 칸 모양이
  틀린 파일(예: `settings`가 목록)을 열 수 있게 고치고 틀린 값은 `_repaired`에 옮긴다.
- 더 새 버전이 저장한 파일은 고치지 않는다: 모르는 칸은 그대로 지닌 채 저장하고 `app_version`도 낮추지 않는다 → `update.data_newer`.

### 19-5. 켤 때 자동 업데이트(선택)
`settings.auto_update`(기본 false, exe·pip만, `update_check`가 켜져 있어야 함). 켜 두면 `lms-agent serve`가 화면을 열기 전에 확인(하루 한 번 규칙)하고
새 버전이 있으면 19-3과 같은 확인·백업을 거쳐 바꾼 뒤 원래 인자로 다시 켠다. 지난번에 같은 버전으로 바꿨는데 아직 옛 버전이 돌면(`applied.version == latest`)
되풀이하지 않고 `error`로 알린다. 터미널: `lms-agent update [--check] [-y]`.

## 20. 에이전트에게 묻기(채팅) — v0.6.1 (사용자 요청 2026-11-13)

사용자: "AI와 채팅을 할 수 있어서 직접적인 요청을 CLI를 통해 할 수 있도록 … 코너케이스를 모두 못 잡을 수 있는데 '성적이 안 들어오는 문제가 있습니다', '기말고사 날짜가 안 보이네요' 이런 것들을 요청해서 사후적으로 찾을 수 있게".
원칙: 규칙이 놓친 것을 **사용자가 말로 짚으면** 에이전트가 이 PC에 모아 둔 글(공지·게시판·메시지·강의계획서·첨부 PDF 글)에서 찾아 **근거와 함께 제안**한다. 하네스가 검증한 제안만 [반영] 단추가 붙고, 반영한 것은 `decided_by: "ai"`·이유·되돌리기(12장과 같음). AI가 꺼져 있어도 같은 창이 **규칙 검색**으로 동작한다.

### 20-1. 흐름과 요청
- `POST /api/agent/ask {"message": str(1~500자), "course_id"?: str}` → `{"job": id}` 바로 반환(AI는 수십 초 걸릴 수 있어 백그라운드). 진행은 `state.agent`.
- `state.agent = {"enabled": bool(AI 켜짐), "busy": bool, "threads": [{"id", "at", "message", "course_id", "status": "running"|"done"|"error", "answer", "proposals": [...], "searched": n, "error", "by": "ai"|"rules"}] (최근 20개), "calls_today", "limits"}`.
- `POST /api/agent/apply {"thread": id, "proposal": idx}` → 그 제안을 반영(다시 검증한 뒤) → 새 state. `POST /api/agent/undo {"thread", "proposal"}` → 되돌림. `POST /api/agent/clear` → 대화 기록 지우기.
- CLI: `lms-agent ask "기말고사 날짜가 안 보여요" [--course ID]` — 같은 작업을 터미널에서(제안 목록 출력, `--apply N`으로 반영).

### 20-2. 찾을 글 고르기(검색, AI 없이도)
- `agent.search(store, message, course_id=None, limit=…)`: 과목 이름(짧은 이름·별칭), 평가 낱말(기말·final·quiz·과제·LAB…), 날짜 낱말, 점수 낱말(점수·성적·score·평균)로 이 PC의 글(`core._sources` 전체 + `pdf_texts`)을 점수 매겨 가장 관련 있는 문단을 고른다. 결과 `[{"source_key", "type", "title", "posted_at", "course_id", "snippet", "dates": [...]}]`.
- AI가 꺼져 있으면 이 검색 결과를 그대로 답으로: "이런 글을 찾았어요" + 문단 + 날짜가 있으면 [마감 후보로 넣기](사용자가 날짜를 확인하고 넣는 직접 붙여넣기와 같은 길).

### 20-3. AI 작업 `assist` (agent_cli 하네스)
- 입력: 사용자 메시지(가리기), 과목 목록(짧은 이름·id), 지금 상태 요약(그 과목의 항목 제목·마감·상태, 평가 항목·비중, 점수 후보와 연결 상태, 학점 비율 출처), 20-2 검색 결과 문단(가리기, 12,000자 예산 안에서). 출력 스키마:
  `{"answer": 한국어 2~6문장, "proposals": [{"type", "course_id", "evidence": 보낸 글 그대로 인용, "source_key", "reason", …type별 칸}]}`
  type: `add_deadline`(title, due, kind 과제|시험, method?), `fix_due`(item_id, due), `link_score`(candidate key, component), `set_count`(component, n), `set_weights`(components[{name, weight}]), `set_dist`(groups 16장 모양), `note`(반영할 것 없이 설명만).
- 검증(통과한 것만 [반영] 가능, 아니면 `rejected_reason`과 함께 보여 주되 단추 없음): evidence가 보낸 글에 그대로 있음 · course_id가 실제 과목 · due가 파싱되고 evidence의 날짜(`extract.find_dates`, 게시일 기준)와 ±1일 · item_id·candidate key·component가 실제로 있음 · 비중 합 100±1 · 학점 비율은 `distribution.from_groups` 검사 · 같은 마감이 이미 있으면 `add_deadline` 거절("이미 있어요: …").
- 한도: 하루 30회(12장 `AiSession` 하루 한도를 같이 씀), 한 번에 2회 시도·90초. 기록은 `ai_runs`(내용 없음).

### 20-4. 반영
- `add_deadline` → 찾은 항목처럼 `origin: "found"` 새 항목(근거 = 그 글의 인용·출처·게시일, `found_by: "ai"`), 사용자가 [반영]을 눌렀으므로 `decision: "approved"`, `decided_by: "ai"`, `ai_reason` = 이유 + 근거. 다음 동기화가 다시 만들어도 사라지지 않게 `store.data["agent_items"]`에 남기고 `build_items`가 함께 합친다(같은 마감을 규칙이 찾으면 하나로).
- `fix_due` → `update_user(id, {"due"}, by="ai", reason)`. `link_score` → `grade_links_ai`(사용자 연결이 이김). `set_count` → `grade_counts_ai`. `set_weights`/`set_dist` → 자동 칸(`auto`/`dist_auto`)으로, 사용자가 정한 값은 덮지 않음.
- 되돌리기: 반영 전 값으로(항목은 지움, 연결·개수·비중은 이전 값).

### 20-5. 규칙이 놓친 사례 기록(규칙 개선용, 선택)
- AI 제안이 반영되면 `store.data["missed_cases"]`에 `{at, type, source_type, evidence(가린 문장), rule_version}`를 남긴다(이름·이메일·학번은 가림). 설정 → AI에 "놓친 사례 N개 · [파일로 저장]"(JSON) — 사용자가 원할 때만 개발자에게 보내 규칙을 일반화하는 데 쓴다. 자동으로 보내지 않는다.

### 20-6. 구현 결과 (서버·화면, v0.6.1)
- 파일: `agent.py`(검색·작업 연결·검증·반영/되돌리기·놓친 사례·대화 기록), `features/agent.py`(요청·state), 화면은 `index.html`의 사이드바 "에이전트에게 묻기" + 오른쪽 창.
- `POST /api/agent/ask` → `{"job", "agent"}`(400: 빈 글·500자 넘음·모르는/숨긴 과목, 409: 다른 질문이 도는 중). `GET /api/agent/missed-cases`(세션 값 필요, JSON 첨부).
- `state.agent` 추가: `available`(AI CLI 있음), `missed_cases`(개수), `limits = {per_day, per_ask, timeout, max_chars, max_message}`. 대화: `results`(검색 결과 — `dates: [{date, phrase, relative}]`, `score`, `url`), `finished_at`, `attempts`, `notes`. AI가 실패하거나 하루 한도면 `status: "done"`, `by: "rules"`, 이유는 `error`.
- 제안 공통 칸: `type, by, course_id, course_short, evidence(가리지 않은 원문), source_key, source_type, source_title, posted_at, url, reason, summary, rejected_reason, can_apply, applied, applied_at, undone_at, result({item_id} 또는 {}), notes, date_basis`. 종류별: add_deadline `title, kind, method, due` · fix_due `item_id, due, item_title` · link_score `key, component, score_label` · set_count `component, n` · set_weights `components` · set_dist `dist_kind, groups, cutoffs, dist`.
- Item 추가: `found_by: "ai"|"rules"|null`, `agent_ref: {thread, proposal} | null`(항목 카드의 [되돌리기]가 `/api/agent/undo`를 부름). 되돌리기는 반영 뒤 사용자가 그 항목을 바꿨으면 409(사용자 쪽이 이김 — 마감 추가·고치기 모두).
- 규칙 모드의 [마감 후보로 넣기]는 확인함의 확인 필요 후보(`found_by: "rules"`), AI 제안만 승인 상태로. 놓친 사례는 규칙 모드 반영도 기록(`by`).
- 제목은 근거 문장에서만 정한다(질문 낱말로 정하지 않음 — "기말고사 날짜가 안 보여요"에 LAB4 문장이면 'LAB 4').
- 검색 점수: 질문 주제(기말·퀴즈·LAB…)가 글의 제목이나 본문에 있으면 같은 무게, 주제가 있는 질문에서는 그 주제가 있거나 마감·시험 문장인 문단만 날짜 가산(강의 슬라이드 첫 장 날짜가 위로 오지 않게), 점수 질문은 제목의 점수 낱말도 가산. 실제 데이터: '로봇공학개론 기말고사 날짜' → '기말고사 공지', 'ASIC 퀴즈 일정' → 'Announcement about Midterm Week'.
- CLI: `lms-agent ask "<질문>" [--course ID] [--apply N]`(N은 출력된 1부터 번호). 데모: ASIC 공지 'Final week schedule'("The final will be held on … at 10:30") — 규칙은 맨 'final'을 시험으로 읽지 않아 날짜 미공지로 남고, 묻기로 찾는다.

## 21. 여러 에이전트 CLI 연결 — v0.6.2 (사용자 요청 2026-11-13: "다른 CLI들 연결하는 것들도 지원해봅시다 MunderDifflin 이런 거처럼 범용 에이전트들 연결")

참고: Munder Difflin은 Claude Code·OpenAI Codex·Gemini·OpenCode·Crush·GitHub Copilot CLI·로컬 모델 같은 코딩 에이전트 CLI를 그대로 띄워 묶는 다중 에이전트 하네스다. 우리 앱도 같은 생각으로 **하네스는 하나, 실행기(CLI)는 갈아 끼우기**로 만든다. 작업 정의·가리기·검증·한도·기록(10·12·20장)은 어느 CLI든 같다.

### 21-1. 실행기(adapter)
- `agent_cli.ADAPTERS`: `{id, name, exe_names[], args(prompt_mode), output: "claude-json"|"json-lines"|"text", no_tools_flags[], notes}`. 기본 목록:
  `claude`(Claude Code, `-p --output-format json --tools ""`, stdin) · `codex`(OpenAI Codex CLI, 읽기 전용 샌드박스·승인 끔으로 비대화 실행) · `gemini`(Gemini CLI, 비대화 모드) · `opencode` · `copilot`(GitHub Copilot CLI, 도구 허용 없이) · `ollama`(로컬 모델 — `ollama run <모델>`, stdin, 인터넷으로 글이 나가지 않음) · `custom`(사용자 지정).
  각 CLI의 정확한 비대화 옵션은 설치된 버전의 `--help`를 읽어 고른다(`probe`). 모르는 옵션이면 그 실행기는 "확인 필요"로 표시하고 쓰지 않는다 — 옵션을 짐작해 실행하지 않는다.
- 사용자 지정(`custom`): 실행 파일 경로 + 인자 목록(문자열 하나가 아니라 목록, 셸 없음) + 글을 stdin으로 보낼지 + 출력 형식. 글은 **항상 stdin**(명령줄에 강의 글을 넣지 않는다 — 프로세스 목록에 보이고 셸 해석 위험).
- 출력 읽기: claude-json(`result` 칸), json-lines(마지막 메시지의 글), text(전체) — 어느 쪽이든 그 글 안의 첫 JSON 객체를 꺼내 작업 스키마로 검증(지금과 같음).

### 21-2. 안전
- 에이전트 CLI는 원래 파일을 읽고 명령을 실행할 수 있다. 강의 글에 "이 명령을 실행해" 같은 글이 섞여 들어올 수 있으므로(프롬프트 주입): 도구·셸 실행을 끄는 옵션이 확인된 실행기만 "안전" 표시로 기본 선택 가능, 빈 임시 폴더를 작업 폴더로, 환경 변수에서 우리 토큰류(LMS_AGENT_*, GOOGLE 등)는 빼고 실행, 시간 제한·`taskkill /T`(지금과 같음). `custom`은 "도구를 끄는 옵션을 직접 넣어 주세요" 경고와 함께.
- 어느 실행기의 답이든 하네스 검증(원문 인용·날짜·합·존재)을 통과해야 반영된다.

### 21-3. 설정과 상태
- `settings.ai_provider`("auto" = 찾은 것 중 안전한 첫 번째, 기본) · `settings.ai_custom = {path, args[], stdin: true, output}` · `settings.ai_model`(ollama 등 모델 이름, 선택).
- `state.ai.providers = [{id, name, found: bool, path_hint(이름만, 경로 없음), safe: bool, status: "ready"|"needs_check"|"missing", version?}]`, `state.ai.provider`(지금 쓰는 id), `state.ai.command`(이름만).
- `POST /api/ai/provider {"provider", "custom"?, "model"?}`, `POST /api/ai/test {"provider"?}` → 데모 문장 평가(`run_evals`, 강의 글 아님)를 그 실행기로 돌려 `{passed, total, cases, duration_ms}` — 화면의 [연결 시험].
- CLI: `lms-agent ai-eval --provider <id>`, `lms-agent ai-providers`(찾은 실행기와 상태).

### 21-4. 구현 결과 (v0.6.2)
- `agent_cli.ADAPTERS`: claude(그대로 — `-p <고정 한 줄> --output-format json --tools "" --no-session-persistence`, 안전) · codex(`exec`, `--sandbox read-only`가 도움말에 있어야 안전 — 이 PC의 codex-cli 0.162.1에서 확인) · gemini(stdin, `--approval-mode default`, `-o json`이 있으면) · opencode(도구를 끄는 옵션이 없어 늘 '확인 필요') · copilot(`--prompt`·`--deny-tool shell/write`가 도움말에 있어야, `--allow-*`는 절대 넣지 않음) · ollama(`run <모델>`, 받아 둔 모델만, `-cloud` 모델·원격 OLLAMA_HOST는 '이 PC 밖') · custom(경로 + 인자 목록 + 출력 형식, 늘 '안전 아님', 자동 선택 안 함).
- 출력 형식 넷: claude-json · json-lines · text · json(한 객체 — gemini의 `response` 감싸기 포함).
- 도움말 읽기(probe): `<exe> --help`·`--version`, stdin 닫음, 10초, 경로·수정 시각·크기별 10분 기억. **사용자가 [다시 찾기]를 눌렀을 때만**(창을 열었다고 다른 프로그램을 실행하지 않는다 — 같은 이름의 다른 프로그램일 수 있음, 예: VS Code의 copilot 실행기). 확인 전 CLI는 '아직 확인 전'.
- 자식 프로세스 환경에서 `LMS_AGENT_*`, 우리 `.env`가 넣은 값, `GOOGLE*`(gemini 자기 자격 증명은 우리 .env에서 온 게 아니면 유지)를 뺀다. PATH·HOME 등과 각 CLI의 자기 키는 둔다.
- 선택: 자동 = claude → codex → gemini → opencode → copilot → ollama 중 '사용 가능·안전'인 첫 번째. 직접 고른 것이 못 쓰는 상태면 오류(다른 서비스로 몰래 바꾸지 않음). 동기화·자동 정리·묻기·`ask` 모두 같은 설정.
- 요청: `POST /api/ai/provider`(provider 생략 가능, custom 칸은 저장값에 합침, `custom: null` = 지우기, stdin 끄기·.cmd/.bat 인자의 위험 문자·못 쓰는 선택 → 400), `POST /api/ai/rescan`(도움말 다시 읽기), `POST /api/ai/test`(백그라운드, `state.ai.test = {status, provider, name, done, total, passed, cases[{label,…}], duration_ms, error}`, 한도·기록에 안 셈).
- `state.ai` 추가: `provider_name, selected, local, unavailable_reason, model, custom{set, name, args(홈은 ~), output, stdin}, auto_order, test, test_cases`. 실행기 칸 추가: `probed, auto, flags, missing, note, notes, site, output`, ollama `model/models`, custom `warning/args`.
- 아직: claude 말고는 실제 실행으로 끝까지 확인하지 않음(출력 모양은 공개 문서 기준 시험). 데모의 [연결 시험]은 가짜 AI가 정리·묻기만 흉내 내서 '8개 중 3개'로 나온다(화면에 그 이유를 적음). 한 번 부르기 90초 제한은 그대로(느린 로컬 모델은 걸릴 수 있음).

## 22. 에이전트 확장 — v0.7.0 (사용자 요청 2026-11-13: "지금은 놓친 것만 확인하는 느낌" → A~H 모두, "컴퓨터 켤 때 바로 실행 … 이 기능을 agent가 해줄 수 있게. 궁극적으로 사용자가 클릭해서 할 수 있는 기능을 agent가 모두 수행")

원칙(12·20장 그대로): 답·제안에는 근거(그대로 인용한 문장 + 원문 링크)를 붙인다. **숫자 계산은 앱이 하고 AI는 설명만**. 상태를 바꾸는 것은
제안 → 사용자가 [실행]/[반영] → 되돌리기. 밖으로 보내는 것은 없다(메일은 초안만). AI가 꺼져 있어도 규칙만으로 최소한의 답을 한다.
개인정보·동의가 걸린 것(로그아웃·데이터 지우기·학번·AI 켜기/실행기 바꾸기·구글 연결 해제)은 에이전트가 하지 않는다(사용자가 직접).

### 22-0. 파일 소유(서브에이전트 병렬 작업 — 남의 파일은 고치지 않는다)

| 담당 | 파일(만들거나 고침) | 테스트 |
|---|---|---|
| W 만약에 | `whatif.py` | `tests/test_whatif.py` |
| Q 채팅 | `agent.py`(묻기 갈래·A 답하기·다른 모듈 handle 연결), `agent_cli.py`(작업 `answer` 등록, assist에 action 추가), `mock_ai.py`, `features/agent.py` | `tests/test_agent_chat.py`, `tests/test_agent_qa.py` |
| M 메일·체크리스트 | `maildraft.py`, `checklist.py`, `features/checklist.py`, `mock_canvas.py`(과제 설명에 제출 요건 문장만 덧붙임 — 날짜 없는 문장, 다른 테스트 기대값을 바꾸지 않게) | `tests/test_maildraft.py`, `tests/test_checklist.py` |
| C 코치 | `coach.py`(C 브리핑·D 바뀐 마감·E 회고·종강 리포트), `features/coach.py`, `cli.py`(백그라운드 브리핑 알림 — `_toast_reward` 주변만), `notify.py`(문구 함수 추가만) | `tests/test_coach.py` |
| F 공부 일정 | `plan.py`, `features/plan.py`, `core.py`의 `calendar_payload` 주변만, `ics.py`·`gsync.py`(공부 블록을 일정으로) | `tests/test_plan.py` |
| S 자동 실행·액션 | `autostart.py`, `features/autostart.py`, `actions.py`(에이전트가 할 수 있는 클릭 목록·실행·되돌리기) | `tests/test_autostart.py`, `tests/test_actions.py` |
| U 화면 | `web/index.html`만 | (화면 확인은 리드) |
| 리드 | `docs/CONTRACTS.md`·`PROGRESS.md`·`README.md`·`features/__init__.py`·버전 | 전체 |

리드가 미리 만든 빈 모듈(함수 이름·모양 고정): `whatif.handle`, `maildraft.handle`, `plan.handle`, `actions.handle`(모두 해당 없으면 `None`),
`features/coach.py`·`features/plan.py`·`features/checklist.py`·`features/autostart.py`(FEATURES에 등록됨). 각 담당이 몸통을 채운다.

### 22-1. 묻기 갈래 (Q, `agent.answer`)

`answer()`가 메시지를 갈래로 나눠 처리하고 대화에 `kind`를 남긴다(옛 대화는 없으면 `"missing"`으로 본다). 순서대로 첫 번째로 `None`이 아닌 것:
1. `actions.handle(store, message, course_id)` — "자동 실행 켜줘", "동기화해줘", "주간 요약 꺼줘", "성적 화면 열어줘" 같은 **명령** → `kind "action"`
2. `maildraft.handle(store, message, course_id, names)` — "메일/이메일 … 써줘/초안/작성/문의" → `kind "mail"`
3. `plan.handle(store, message, course_id, now)` — "공부 계획/공부 일정/계획 세워/스케줄 짜" → `kind "plan"`
4. `whatif.handle(store, message, course_id, now)` — "몇 점 받아야/받으려면/만약/…이면/0점이면/P(패스) 기준" → `kind "whatif"`
5. 놓친 것(지금 20장 assist·규칙) — "안 보여/안 들어와/빠졌/없어요/놓친/못 찾/이상해" → `kind "missing"`
6. 그 밖의 질문 → `kind "question"`(22-2)

각 handle 결과 `{"answer": str, ...추가 칸}`을 대화에 그대로 합친다(`answer`·`by` 등 20장 칸은 유지). handle이 예외를 내면 다음 갈래로 넘어가고 `notes`에 남긴다.
AI가 켜져 있으면 assist(5)는 지금처럼 AI, question(6)은 22-2의 `answer` 작업. action·mail·plan·whatif는 규칙(+필요하면 각 모듈이 AI 선택 사용 — 숫자·날짜·주소는 지어내지 않음).
대화 칸 추가: `kind`, `citations`(question), `whatif`(whatif), `draft`(mail), `plan`(plan), 제안 `type "action"`(action, 22-9).

### 22-2. A. 과목 질문에 답하기 (Q)

- 검색은 지금 `_search`(공지·게시판·메시지·강의계획서·PDF 글). 범위·형식·규정 같은 질문 낱말에 맞게 점수 보강(예: 범위/scope/coverage/chapter, 형식/format/파일명, 감점/late/지각/규정/policy, 출석/attendance, 시험장/강의실/room).
- 대화 칸 `citations: [{"source_key", "course_id", "course_short", "type", "title", "posted_at", "url", "quote"}]` — `quote`는 그 글의 문장 그대로(500자 이내).
- 규칙(AI 꺼짐·실패): 가장 관련 높은 문단 1~3개를 인용해 "이 글들에 이렇게 적혀 있어요" 형태로 답하고 citations를 채운다. 못 찾으면 그렇다고 말한다(지어내지 않음).
- AI(`agent_cli` 작업 `answer`, mock_ai도 흉내): 입력은 assist와 같은 글 묶음. 출력 `{"answer": 2~6문장, "citations": [{"source_key", "quote"}]}`.
  검증: 각 quote가 보낸 글에 그대로 있어야 하고(없으면 다시 묻기, 마지막엔 그 인용을 뺌), 인용이 하나도 없으면 "글에서 찾지 못했어요" 답만 허용.
  답에 있는 날짜·숫자는 인용 안에 있어야 한다(없으면 다시 묻기). 제안(proposals)은 없음.

### 22-3. B. 성적 '만약에' (W, `whatif.py`)

- `handle(store, message, course_id=None, now=None) -> dict | None`. 성적 화면과 같은 계산 재료를 쓴다:
  `gradebook.section(store, courses, items, sem)`(features/grades가 쓰는 것과 같은 방식으로 만든 과목별 평가 항목·점수·평균·표준편차·학점 비율·절대평가 기준·P/NP)와
  `grades.estimate`/`estimate_absolute`/`needed_on`. **숫자는 이 계산 결과만**.
- 알아듣는 질문(예): "기말에서 몇 점 받아야 A?"(목표 학점 + 남은 항목 → needed), "A 받으려면?"(남은 항목이 여럿이면 항목별), "LAB 하나 0점이면?"/"중간 70점이면 학점은?"(가정 점수로 다시 추정 → scenario),
  "P/NP 기준 넘었어?"(P/NP 과목: 받은 점수·남은 비중만 설명 — 학점 추정 안 함). 과목을 못 정하면 성적이 있는 과목이 하나면 그것, 여럿이면 되묻기(answer로 과목 목록).
  "A"는 A0로 보고(A+이면 A+) 그렇게 했다고 basis에 적는다. 목표 학점이 없으면 지금 추정 학점과 다음 학점까지를 보여 준다.
- 결과 `{"answer": str, "whatif": {"course_id", "course_short", "kind": "needed"|"scenario"|"pass_fail"|"current"|"ask_course",
  "target": "A0"|null, "component": str|null, "lines": [str], "rows": [{"label": str, "value": str}], "basis": [str]}}`.
  `basis`: 어떤 값이 기본값/추정/사용자 입력인지(예: "학점 비율은 강의계획서에서 찾은 값", "평균·표준편차가 없어 기본값으로 어림"). 계산할 수 없으면 kind "current"·이유를 answer에.
- 이 PC 밖으로 성적을 보내지 않는다(AI 안 씀).

### 22-4. C·D·E. 코치 카드 (C, `coach.py`·`features/coach.py`)

`state.coach = {"briefing": 카드|null, "retro": 카드|null, "report": 카드|null, "changes": [바뀐 마감…]}` — 화면을 물을 때 저장된 것만으로 계산(네트워크 없음).
카드 공통: `{"id", "kind": "briefing"|"retro"|"report", "title", "lines": [{"text", "item_ids": [..], "tone": "deadline"|"exam"|"study"|"pace"|"warn"|"cheer"}], "created_for": "2026-11-09"}`.
- **briefing(C)**: 이번 주(월~일) 기준. 이번 주 마감(일정·확인 대기) 수와 가장 몰린 요일, 30일 안 시험 D-day, `gradebook.priorities` 상위 1~2(어느 과목부터),
  페이스(km·연속 완주·다음 메달까지). 문장마다 근거 항목 id. 이번 주 내내 보이고, 닫으면(`POST /api/coach/dismiss {"id"}` → `store.data["coach"]["dismissed"]`) 그 주에는 다시 안 보임.
- **retro(E 주간)**: 지난주 결과 — 마감 n개 중 m개 제출, 놓친 것 제목(최대 3), 연속 완주·다음 메달. 월·화요일에만(닫으면 끝).
- **report(E 종강)**: `week_now > weeks_total`이면 학기 리포트 — 마감 처리 수, 메달 수·이름, 최고 연속, 과목별 한 줄(성적 추정은 화면 안에서만). 닫을 때까지.
- **changes(D)**: 최근 14일 안에 마감이 바뀐 항목 `[{"item_id", "title", "course_short", "from", "to", "direction": "later"|"earlier", "source_title", "posted_at", "quote", "url"}]`.
  근거는 같은 과제의 다른 후보(충돌·대체됨)나 근거 사슬에서 찾은 더 새 글. 근거 문장이 있을 때만(지어내지 않음).
- 백그라운드(cli `_toast_reward` 순서: 신규 마감 알림 > **주간 브리핑(주 1회, 그 주 첫 백그라운드 동기화)** > 보상): "LMS Agent · 11주차 작전" / "마감 4개, 목요일이 고비 …". 알린 주는 `store.data["coach"]["toasted"]`. 성적 없음.

### 22-5. F. 공부 일정 (F, `plan.py`·`features/plan.py`)

- `make(store, course_id=None, item_id=None, days=None, minutes=60, hour=20, start=None, now=None) -> {"title", "course_id", "item_id", "blocks": [{"start", "end", "title", "course_id", "item_id"}], "notes": [str]}`.
  기본 대상: 그 과목(없으면 가장 가까운) 시험 항목. 오늘~시험 전날 사이 하루 한 블록, 마감이 2개 이상인 날·시험 당일은 건너뜀, 최대 14블록. 시각은 hour:00(KST), 길이 minutes.
- `handle(store, message, course_id=None, now=None)` — "기말 전 2주 공부 계획 세워줘" → `{"answer", "plan": make(...)}`(아직 저장 안 함, 미리 보기).
- 요청: `POST /api/plan/preview {course_id?, item_id?, days?, minutes?, hour?}` → `{"plan": {...}}`(저장 안 함),
  `POST /api/plan/apply {"title", "course_id"?, "item_id"?, "blocks": [...]}` → 검증(미래·최대 30블록·각 15~240분·겹침 없음) 후 `store.data["plans"]`에 `{id: "p-…", created_at, decided_by: "user"|"ai", ...}` 저장, "changed" → 새 state,
  `POST /api/plan/remove {"id"}` → 그 계획의 블록 전부 삭제 → 새 state. `state.plans = [{"id", "title", "course_id", "item_id", "blocks", "created_at"}]`.
- 내보내기: `core.calendar_payload`가 공부 블록을 함께 낸다(.ics·구글 모두, UID `study-<plan>-<n>@lms-agent`, 제목 앞 "[공부] ", 길이가 있는 일정, 알림 30분 전). 계획을 지우면 다음 반영에서 구글 일정도 지워진다.

### 22-6. G. 과제 체크리스트 (M, `checklist.py`·`features/checklist.py`)

- `extract(texts: list[{"text", "source_title", "url"}]) -> [{"text", "quote", "source_title", "url"}]`: 제출 요건 문장을 규칙으로 — 파일 형식(PDF·zip·docx·hwp), 파일명 규칙, 분량(쪽·페이지·words), 포함할 것(코드·스크린샷·보고서·사진), 제출처(LMS·이메일 주소·종이·우편함), 팀/개인, 지각 규정. 문장마다 그대로 인용.
- 대상 글: 그 항목의 Canvas 과제 설명 + 근거 사슬의 글(공지·메시지)·같은 과목 공지 중 제목이 맞는 것. 날짜는 체크리스트에 넣지 않음(마감은 따로 있음).
- `state.checklists = {item_id: {"items": [{"id", "text", "quote", "source_title", "url", "done", "by": "rules"|"user"}], "updated_at"}}` — 화면에 보이는 항목(review·scheduled)만, 요건이 하나라도 있을 때.
- 요청: `POST /api/checklist/<item_id> {"op": "toggle", "id"} | {"op": "add", "text"} | {"op": "remove", "id"} | {"op": "refresh"}` → 새 state. 사용자 체크·추가는 다시 동기화해도 남는다.

### 22-7. H. 문의 메일 초안 (M, `maildraft.py`)

- `draft(store, course_id, purpose, item_id=None, note=None, names=None) -> {"course_id", "purpose", "to": [{"email", "quote", "source_title"}], "subject", "body", "notes": [str]}`.
  purpose: `extension`(연장)·`absence`(결석·공결)·`grade`(성적 문의)·`submission`(제출 확인)·`question`(일반). 정중한 한국어 틀에 과목 이름·과제 제목·마감을 채운다.
  받는 사람: 그 과목 글에서 조교/TA/교수/professor/문의 근처의 이메일 주소만(인용과 함께), 항목의 `email_to`가 있으면 그것 우선. **없으면 비워 두고** notes에 "주소를 못 찾았어요". 지어내지 않음.
  이름·학번은 `[이름]`·`[학번]` 자리표시(설정에 사용자 이름이 있으면 이름만 채움). 보내지 않는다.
- `handle(store, message, course_id=None, names=None)` — "LAB4 연장 문의 메일 써줘" → 과목·항목·목적을 알아내 `{"answer", "draft"}`.
- 요청: `POST /api/mail/draft {"course_id", "purpose", "item_id"?, "note"?}` → `{"draft": {...}}`(저장 안 함). 화면: 제목·본문 고칠 수 있는 칸, [복사], [메일 앱으로 열기](`mailto:`).

### 22-8. 컴퓨터를 켜면 앱 열기 (S, `autostart.py`·`features/autostart.py`)

- `state.autostart = {"supported": bool, "enabled": bool, "open_browser": bool, "how": "windows-task"|"macos-launchagent"|"linux-autostart"|null, "command": str|null, "error": str|null}`.
- `POST /api/autostart {"enabled": bool, "open_browser"?: bool}` → 등록/해제 → 새 state. Windows: 작업 스케줄러 로그온 작업 "LMS Agent App"이 실행 스크립트(.vbs, 창 없이)로
  `lms-agent serve --port 8765`(open_browser false면 `--no-browser`)을 켠다 — 경로는 백그라운드 동기화처럼 지금의 파이썬·exe(켤 때마다 스크립트 갱신).
  macOS: `~/Library/LaunchAgents/kr.lmsagent.app.plist`(RunAtLoad). Linux: `~/.config/autostart/lms-agent.desktop`. 이미 같은 포트에 앱이 떠 있으면 새로 켜지 않고 브라우저만(open_browser일 때).
  데모는 등록하지 않음(supported false). 테스트는 가짜 runner/경로(실제 PC 설정을 건드리지 않음). 실패는 400 `{"error"}`.

### 22-9. 에이전트가 하는 클릭 (S `actions.py` + Q 연결 + U 단추)

- `actions.CATALOG`: 화면 단추가 부르는 API를 이름 붙여 묶은 목록. 항목 `{"id", "title"(한국어), "params"(간단한 모양), "client": bool, "route": ("POST", "/api/…") 또는 None, "undoable": bool}`.
  서버 액션은 **같은 API 경로를 앱 안에서 부른다**(api.router.match → handler(app, body) — 검증·저장이 화면과 똑같음). 되돌리기는 실행 전 값을 기억해 같은 경로로 되돌림(되돌릴 수 없으면 `undoable: false`).
  최소 목록: 설정 칸 켜기/끄기(include_canvas·weekly_summary·review_reminder·notify·update_check·auto_update), 백그라운드 동기화 켜기/끄기·간격, **컴퓨터를 켜면 앱 열기 켜기/끄기**,
  지금 동기화(전체 다시 확인 포함), 구글 캘린더 지금 반영(연결돼 있을 때), 과목 종류 바꾸기(성적 있음·P/NP·제외), 항목 승인·제외·확인 전으로, 새 버전 확인, 지금 업데이트,
  첨부 자동 다운로드, 캐릭터 바꾸기, 브리핑 닫기, 공부 계획 넣기·지우기(22-5), 체크리스트 체크(22-6).
  화면 액션(`client: true`, 화면이 직접 함): 화면 열기(대시보드·확인함·학기·과목·성적·자료·설정 칸·특정 과목/항목), 튜토리얼·달라진 점 보기, 캘린더 파일 받기.
  하지 않음: 로그아웃, 데이터 지우기, 학번, AI 켜기·실행기 바꾸기, 구글 연결 해제, 토큰.
- `actions.handle(store, message, course_id=None) -> {"answer", "proposals": [액션 제안]} | None` — 규칙으로 명령 알아듣기(켜/꺼/해줘/열어/바꿔/동기화/반영/업데이트 + 목록의 이름). 명령이 아니면 None.
  액션 제안: `{"type": "action", "action": id, "params": {...}, "title": "컴퓨터를 켜면 앱 열기 켜기", "reason": str, "client": bool, "undoable": bool}`.
  AI assist도 같은 목록(이름·params 모양)을 받아 type "action"을 낼 수 있다 — `actions.check(store, p) -> p(+rejected_reason)`이 id·params를 검사(모르는 것·금지 목록은 rejected_reason).
  `actions.catalog_text() -> str`: AI에게 보낼 목록 설명(이름·params·뜻, 2,000자 이내).
- 실행: 서버 액션은 `POST /api/agent/apply`(지금 경로)에서 type "action"이면 `actions.run(app, p) -> {"ok", "message", "undo": 값|None}` → 제안에 `applied`·`result`·`_undo` 저장,
  `POST /api/agent/undo` → `actions.undo(app, p)`. 화면 액션은 화면이 바로 수행(서버 기록 없음). 실행은 늘 사용자가 [실행]을 누를 때만.

### 22-10. 화면 (U, `web/index.html`)

- 채팅 서랍: kind별 — question(답 + 인용 목록: 인용문·글 제목·날짜·[원문]), whatif(줄·표 rows·basis 작은 글씨·[성적에서 보기]), mail(받는 사람·제목·본문 고칠 칸·[복사]·[메일 앱으로 열기]·notes),
  plan(블록 목록·[캘린더에 넣기]→/api/plan/apply·[다시 짜기]), action 제안([실행]·결과·[되돌리기]·client 액션은 화면이 바로). 예시 칩: '기말 범위 어디까지야?', '기말에서 몇 점 받아야 A?',
  'LAB4 연장 문의 메일 써줘', '기말 전 2주 공부 계획 세워줘', '컴퓨터 켤 때 자동 실행해줘', '기말고사 날짜가 안 보여요'.
- 대시보드: 브리핑 카드(위, 닫기), 회고 카드(월·화), 종강 리포트, "바뀐 마감" 묶음(D: from → to·근거 인용).
- 항목 카드(확인함·대시보드 날짜 목록·과목 상세): 체크리스트 칩 "체크리스트 2/5" → 펼치기(체크·추가·삭제·근거), [문의 메일] → 초안 창(/api/mail/draft).
- 설정: "컴퓨터를 켜면 앱 열기"(켜기·브라우저도 열기) 칸. 공부 계획 목록(지우기)은 학기 화면 또는 대시보드.
- 튜토리얼: `TOUR_VERSION` 3 — '달라진 점'에 "물어보면 답해요"(질문·만약에·메일·계획·명령 예시)와 브리핑 카드 단계를 더한다(보일 때만).

### 22-11. 구현 결과 (v0.7.0, 서브에이전트 7명 병렬 → 리드 통합)

- **갈래(Q)**: 모든 대화에 `kind`(답하는 중에는 `null`, 옛 대화·오래된 오류는 `"missing"`)와 `citations`(question 말고는 `[]`). handle이 예외를 내거나 dict가 아니면 다음 갈래로 넘어가고 `notes`에 갈래 이름·예외 종류만 남긴다(예외 내용은 남기지 않음).
  handle 결과는 대화의 뼈대 칸(`id, at, message, course_id, status, kind, finished_at, searched, results, attempts`)을 덮지 못한다.
  `agent.classify`: '안 보여요' 말이 없어도 **날짜 질문**("LAB4 마감 언제야?")은 missing(규칙이 놓친 마감을 [반영]할 수 있는 갈래), '안 보여요' + 주제만 있고 고칠 것이 없으면("제출 형식이 안 보여요") question.
- **질문(Q)**: `agent.TOPICS` 8개(scope·format·policy·attendance·room·materials·grading·contact) — 문단에 주제가 있으면 +4, 제목 +1.5. 규칙 인용은 평가 낱말(기말·LAB…)과 같은 번호(LAB4면 Lab6 글 제외)·주제가 문단이나 제목에 있어야 하고,
  과목을 말했으면 그 과목 글만, 최대 3개(가장 높은 점수의 절반 이상). AI 작업 `answer`: 인용 8자 이상·최대 4개, 잘못된 source_key는 인용이 실제로 있는 글로 고침, 답의 날짜는 인용의 월/일과 맞아야 하고
  다른 숫자는 인용·질문·과목 이름·인용한 글 제목에 있어야 함(마지막 시도까지 어기면 규칙 답). 점수 1.0을 넘는 검색 결과가 없으면 AI를 부르지 않는다(하루 한도 아낌). 인용은 원문으로 되돌려 보여 준다(가린 이메일 대신 실제 주소).
- **만약에(W)**: kind `current`는 '계산할 수 없음'(첫 줄 `계산할 수 없는 이유: …`)과 '되묻기'(`물어볼 것: …`)도 맡는다. rows 값은 모두 문자열(소수 한 자리). 남은 항목이 여럿이면 항목마다 따로 `needed_on`(성적 화면의 필요 점수와 같음).
  P/NP 과목은 '점수가 들어간 비중 X%: 비중대로 더하면 100점 중 Y점'(단순 합, 학점 추정 아님). 불평 말(안 들어가/반영이 안/누락/오류/버그 …)이 있으면 None → missing 갈래. 대문자 A~D만 학점으로 읽는다.
- **코치(C)**: 카드 id `briefing:<월요일>`·`retro:<월요일>`·`report:<마지막 주 일요일>`(=`created_for`). 칸 추가: briefing `week`, retro `week, total, done`, report `weeks_total`.
  changes 추가 칸: `source_type`, `basis`(`conflict`|`edited`|`canvas`|`canvas_post`), `from_item_id`, `status`(review|scheduled|replaced — 옛 날짜를 승인한 뒤 연장 공지가 오면 replaced로 보여 줌), `url`은 null일 수 있음. 최근 14일·최대 10개·최신 글 먼저.
  `store.data["coach"] = {"dismissed": [...], "toasted": [...]}`(각 100개). dismiss: 지금 보이지 않는 id는 400, 잠금 시간 초과는 409.
  백그라운드 브리핑 알림은 주 1회지만 **그 주에 앱·CLI 동기화가 있었으면 보내지 않는다**(카드를 이미 볼 수 있었음 — 알림은 앱을 안 여는 사람용). 알림 글은 마감·시험·응원 줄만(공부·페이스·성적 없음).
- **공부 일정(F)**: `apply`는 state와 함께 `plan_id`를 돌려준다(본문 `decided_by`·`reason` 받음, AI일 때만 reason 저장). 저장된 다른 계획과 겹쳐도 400(같은 계획 두 번 넣기 방지), 계획 최대 50개.
  plan에 `course_short, target{title,due,status,kind,url}, days, minutes, hour`. `state.plans[]`에 `course_short, decided_by, reason, hidden, past`, 블록마다 `uid, gcal_link`. 시험: kind "시험" 또는 시험류 평가 항목에 연결된 항목.
  내보내기: `core.study_events` → calendar_payload 둘째 목록에 `kind: "study"`(TRANSP 투명 — gcal과 같음, VALARM -PT30M, CATEGORIES:공부). 구글 `google.desired.study`(공부 블록이 있을 때만) — 13-5의 합 검사는 `deadlines + summaries + study`.
- **체크리스트·메일(M)**: 체크리스트 항목 `kind`(where·format·name·length·include·team·policy·user), 규칙 항목 id = `"r-" + sha1(kind|text)[:10]`(다시 동기화해도 체크 유지, 문구가 바뀌면 새 항목).
  `toggle`에 선택 `done`(되돌리기용), `refresh` = 숨긴 규칙 항목 되살리기, 사용자 항목 최대 30·200자. 404(모르는 항목·숨긴 과목·모르는 칸), 400(잘못된 op·id·글). "changed"는 내보내지 않음.
  메일 초안 `to[]`에 `role`(교수|조교|문의처|제출처)·`url`, `item_id`. 받는 사람 최대 3(항목 `email_to`는 그 주소를 적은 문장이 있을 때만 맨 앞, 조교 → 교수, 결석은 교수 먼저).
  역할 낱말이 근처에 없으면 주소 앞부분(`ta`·`prof`)으로 역할을 짐작한다. 과목을 못 정하면 `course_id: null`·`[과목명]` 자리표시 초안과 과목 목록. 400(목적·id·note 1000자 초과), 404(모르는 과목·항목).
- **자동 실행(S)**: `settings.autostart_open_browser`(기본 true). 실행 스크립트(Windows `app-start.vbs` — WinHttp, macOS·Linux `app-start.sh` — curl→wget→python)가 먼저 `GET http://127.0.0.1:8765/`에서 `lms-agent-token`을 찾고,
  있으면 브라우저만 열고 끝, 없으면 `serve --port 8765`. Windows 작업 "LMS Agent App"(로그온 10초 뒤). macOS는 plist만 쓰고 `launchctl load` 안 함(다음 로그인부터), 끌 때도 파일만 지움(돌던 앱을 죽이지 않게).
  **앱 끄기**(리드 추가): `POST /api/app/quit` → 응답 뒤 0.6초 있다가 서버를 멈추고 프로세스 종료(테스트는 `App(..., quit=가짜)`). 창 없이 켜진 앱을 끌 방법 — 설정 → 컴퓨터를 켜면 앱 열기의 [앱 끄기], 에이전트 "앱 꺼줘"(액션 `app_quit`).
- **액션(S)**: 목록 `setting, auto_download, background, autostart, sync, google_push, course_mode, item_decision, item_submitted, update_check, update_apply, mascot, materials_folder, app_quit, coach_dismiss,
  plan_apply, plan_remove, checklist_toggle`(서버) + `open_view, tutorial, download_calendar`(화면). 금지 id(logout·wipe_data·student_id·ai_enable·ai_provider·google_connect·google_disconnect·token)와
  금지 설정 키(ai_cli·ai_auto_triage·ai_provider/custom/model·student_id·token·canvas_url)는 거절하고 그 설정 칸 열기만 제안. 제목·client·undoable은 늘 목록에서(AI 입력을 믿지 않음), 모르는 params는 버림, '이미 그 상태'도 거절.
  run은 실패를 예외로 낸다(`ok: false`를 돌려주지 않음). undo는 `_undo`(없으면 `result.undo`)로, 실행 뒤 사용자가 값을 바꿨으면 409(사용자가 이김). 항목·과목 결정은 기존 경로를 지나므로 `decided_by: "user"`(사용자가 그 동작에 [실행]을 누름), plan_apply만 "ai".
  `/api/agent/apply`·`undo`는 action일 때 `actions.run/undo`를 **잠금 밖에서** 부른다(안쪽 경로가 app.lock·파일 잠금을 직접 잡음). 409: 이미 실행·실행 전·되돌릴 수 없음·다른 동작 실행 중, 400: 화면 액션·거절·다시 검사 실패.
- **화면(U)**: `#pane-autostart`(설정), `#mail-dlg`(항목의 [문의 메일]), 공부 계획 목록은 학기 화면 오른쪽, 코치 카드는 대시보드 맨 위·바뀐 마감은 옆 칸. [문의 메일]은 확인함 카드·과목 상세에서만(대시보드 날짜 목록은 체크리스트 칩만).
  튜토리얼 `TOUR_VERSION` 3: 판 2를 본 사람에게는 3에서 늘어난 단계(물어보면 답해요·브리핑)만.
- **함께 고친 것**: `extract.EMAIL_ADDR`를 ASCII로(주소 바로 뒤 한국어 조사 '로'가 주소에 붙던 문제 — 'x.test로'), `RULES_VERSION` 2026.11.13-5.
- **확인 안 된 것**: 실제 로그온 트리거·LaunchAgent·XDG 자동 시작(스크립트만 실행해 봄), 실제 클립보드 복사·메일 앱 열기, 캘린더 파일 받기 화면 액션, 실제 Windows 알림 표시.

## 23. 구글 캘린더의 다른 일정도 대시보드에 — v0.7.1 (사용자 요청 2026-11-13: "구글캘린더 연동해서 다른 일정도 대시보드에 보이게")

원칙: **선택(기본 꺼짐)**, 읽기만(다른 캘린더에 쓰거나 지우지 않음 — 쓰기는 지금처럼 앱이 만든 "LMS 마감" 캘린더만). 일정 내용은 이 PC(state.json)에만 두고
**AI에게 보내지 않으며**(에이전트 assist·answer의 상태 요약·검색 글에 넣지 않음), 알림(잠금 화면)에도 넣지 않는다. 앱의 "LMS 마감" 캘린더는 읽기 대상에서 뺀다(마감이 두 번 보이지 않게).

### 23-0. 파일 소유

| 담당 | 파일 | 테스트 |
|---|---|---|
| R 서버 | `gcal.py`(읽기 권한·목록·일정 읽기), `gsync.py`(GoogleLink에 읽기 상태·가져오기), `gevents.py`(새: 정리·저장·창), `features/google.py`(경로·상태·가져오는 때), `mock_google.py`(목록·일정 흉내), `plan.py`(공부 블록이 일정과 겹치지 않게), `coach.py`(브리핑에 일정 수), `actions.py`(액션 하나) | `tests/test_gevents.py`(새), 기존 `test_gcal.py`·`test_google_app.py`·`test_plan.py`·`test_coach.py`·`test_actions.py` 유지 |
| U 화면 | `web/index.html`만 | (리드가 확인) |
| 리드 | 이 문서·`PROGRESS.md`·`README.md`·버전 | 전체 |

### 23-1. 권한(R)

- 쓰기 권한은 그대로 `calendar.app.created`. 읽기용으로 `https://www.googleapis.com/auth/calendar.calendarlist.readonly`(캘린더 목록)와
  `https://www.googleapis.com/auth/calendar.events.readonly`(일정 읽기)를 **켤 때만 추가로**(증분 동의: `include_granted_scopes=true`, 요청 scope = 쓰기 + 읽기 둘).
- 동의 화면에서 사용자가 읽기 체크를 빼면(구글의 세부 권한) 쓰기 연결은 그대로 유지하고 읽기만 `granted: false` + 안내. 처음 연결(쓰기만)은 지금과 같다.
- 토큰의 `scope`로 허용 여부를 판단(`read_granted`). 연결 해제는 지금처럼 전부(읽어 둔 일정도 지움).

### 23-2. 상태와 요청

```json
"google": { …6장 그대로…,
  "read": {
    "enabled": false,            // settings.gcal_read (사용자가 켬)
    "granted": false,            // 토큰에 읽기 권한 둘 다 있음
    "calendars": [{"id": "primary", "name": "내 캘린더", "primary": true, "selected": true, "color": "#…", "access": "owner"}],
    "fetched_at": "2026-11-13T18:00:00+09:00", "count": 12, "error": null, "fetching": false
  }
}
"events": [   // 대시보드에 보일 다른 일정(오늘 0시 기준 −1일 ~ +21일), 시작 시각 순, 최대 300
  {"id": "<calendar>:<event id>", "calendar_id": "…", "calendar_name": "개인", "color": "#16a765", "title": "동아리 회의",
   "start": "2026-11-17T19:00:00+09:00", "end": "2026-11-17T21:00:00+09:00", "all_day": false,
   "location": "학생회관" | null, "url": "https://www.google.com/calendar/event?eid=…" | null, "busy": true}
]
```
- 종일 일정은 `all_day: true`, start/end는 그 날 0시(로컬)·다음 날 0시. 취소된 일정·"LMS 마감" 캘린더·`transparency: transparent`(한가함 표시)인 것도 보이되 `busy: false`. 반복 일정은 `singleEvents=true`로 펼침.
- 제목 없는 일정은 "(제목 없음)", 비공개로 공유된 일정(제목이 안 오는 것)은 "바쁨". 설명·참석자는 저장하지 않는다(필요 없음, 개인정보 최소).
- `POST /api/google/read {"enabled": bool}`: 끄면 즉시 저장된 일정 삭제·`events: []`. 켤 때 권한이 없으면 `{"need_consent": true, "url": 동의 화면}`(화면은 그 주소로 이동 — 돌아오면 `/?google=ok#settings`, 권한을 받으면 바로 가져옴),
  권한이 있으면 가져오기 시작 → 새 state.
- `POST /api/google/read/calendars {"selected": [id…]}`: 보일 캘린더(기본: 구글에서 '표시'로 둔 캘린더, "LMS 마감" 제외). 모르는 id는 400. → 다시 가져옴 → 새 state.
- `POST /api/google/read/refresh` → 지금 가져오기(백그라운드, `fetching: true`) → 새 state.
- 가져오는 때: 앱을 켤 때·동기화 뒤·15분마다(앱이 켜져 있을 때)·위 요청. 실패는 `read.error`(한국어)에만, 지난 일정은 그대로 둔다. 401/403 권한 오류면 `granted: false`로 바꾸고 다시 동의 안내.
- 데모: 가짜 구글이 목록(내 캘린더·동아리·공휴일)과 데모 날짜에 맞는 일정 몇 개를 준다. 데모에서 연결 → 다른 일정 켜기로 바로 볼 수 있게.

### 23-3. 다른 기능과 함께(R)

- **공부 일정(22-5)**: `plan.make`는 그 시각(hour~hour+minutes)에 busy 일정이 겹치는 날을 건너뛰고 notes에 "일정과 겹쳐 뺀 날: …". 종일 일정은 겹침으로 보지 않음(공휴일 등) — 단 종일 일정만 있는 날은 그대로.
- **주간 브리핑(22-4)**: 가장 몰린 요일을 고를 때 마감·시험과 함께 busy 일정 수도 센다("목요일이 고비 — 마감 2개·일정 3개"). 일정 제목은 카드에 넣지 않는다(수만).
- **액션(22-9)**: `gcal_read`({"enabled": bool}, 되돌리기 가능) — 권한이 없으면 실행 결과가 "설정 → 구글 캘린더에서 권한을 허락해 주세요"이고 화면이 그 칸을 연다(동의는 사용자가 직접). `gcal_refresh`(되돌리기 없음).

### 23-4. 화면(U)

- 설정 → 구글 캘린더: 연결돼 있으면 "다른 일정도 대시보드에 보기"(켜기 → 권한 동의로 이동할 수 있다는 안내), 캘린더 고르기(체크), 마지막으로 가져온 때·[다시 가져오기]·오류. 연결 전에는 회색 안내.
- 대시보드 날짜 축: 다른 일정을 마감과 구별되는 표시(색은 23-5 — 캘린더 색), 종일 일정은 그 날 칸 위쪽 얇은 띠. 마감 깃발과 겹치지 않게.
  날짜별 목록: 그 날 마감 아래에 "일정" 줄(시간 범위·제목·캘린더 이름, [구글에서 열기]). 마감 수·완주·보상 계산에는 넣지 않음.
- 학기·과목 화면에는 넣지 않는다(대시보드만). 일정이 없거나 꺼져 있으면 아무것도 그리지 않는다.

### 23-5. 색으로 구분 (사용자 추가 요청 2026-11-13: "별도의 색상으로 구분할 수 있게 등등")

- 23-4의 '회색만'을 바꾼다: 다른 일정은 **캘린더마다 다른 색**. 색은 구글 캘린더 목록의 `backgroundColor`(사용자가 구글에서 정한 색)를 그대로 쓰고,
  없거나 잘못된 값이면 앱의 고정 팔레트 6색(파랑 `#2a78d6`·주황 `#eb6834`·청록 `#1baf7a`·노랑 `#eda100`·보라 `#6250d6`·초록 `#008300` — 빨강은 버건디와 헷갈려 뺌.
  인접 색 색각 이상 ΔE·일반 시각 ΔE 검사 통과, 노랑·청록은 흰 바탕 대비가 낮아 이름표 필수)에서 캘린더 id의 고정 해시로 늘 같은 색을 고른다.
  서버가 캘린더(`read.calendars[].color`)와 일정(`events[].color`)에 `#rrggbb` 소문자로 넣는다(검증된 값만, 그 밖은 null → 화면이 팔레트).
- 마감은 지금처럼 **버건디**(앱의 유일한 강조색은 마감·지금·행동에만), 일정은 캘린더 색 — 모양도 다르게(마감 깃발·원, 일정은 얇은 막대·점).
  색만으로 구분하지 않는다: 날짜 축 위 범례(캘린더 이름 + 색 점), 목록 줄에 캘린더 이름, 마우스를 올리면 '시간 제목 · 캘린더'.
- 범례에서 캘린더를 누르면 대시보드에서 잠깐 숨기기/보이기(이 브라우저에만 기억, 서버 선택은 설정의 캘린더 고르기). 글자는 늘 먹색(색 위 글자 대비 문제 없게 — 색은 점·막대·띠에만).

### 23-6. 구현 결과 (v0.7.1, 서브에이전트 R 서버·U 화면 → 리드 통합)

- **보이는 것**: 취소된 일정과 앱의 "LMS 마감" 캘린더(저장된 id, 그리고 같은 이름·owner)는 **빼고**, '한가함'(transparent) 일정만 `busy: false`로 보인다(23-2 문장보다 이것이 기준).
  창은 [오늘 0시 − 1일, 오늘 0시 + 22일)과 겹치는 일정, 시작 시각 순, 최대 300(캘린더 최대 50, 제목 200자). 설명·참석자는 요청(`fields`)도 저장도 하지 않는다.
- **동의**: 처음 연결은 그대로(쓰기만, include_granted_scopes 없음). 켤 때 `authorization_url(read=True)` = 쓰기 + 읽기 둘 + `include_granted_scopes=true`. 구글이 갱신 토큰을 다시 주지 않으면 이전 것을 유지.
  `need_consent`는 설정을 켜지 않고, 읽기 동의를 받고 돌아왔을 때 켠다. 콜백: 받음 `/?google=ok&read=ok#settings`(바로 가져옴), 읽기 체크를 뺌 `/?google=ok&read=denied#settings`(쓰기 연결 유지, `read.error`에 안내),
  취소 `/?google=error&read=cancelled&msg=…#settings`(토큰 유지). `read_granted`는 토큰 scope 문자열로(읽기 둘, 또는 더 넓은 calendar 권한).
- **저장**: `settings.gcal_read`(이 경로로만 — /api/settings에는 없음), `store.data["gcal_events"] = {fetched_at, events, error, calendars(google_selected 포함), selected(null = 구글에서 '표시'인 것)}`, 읽기 403이면 `cfg.google.read_denied`(다음 동의 성공·연결 해제 때 지움).
  끄면 일정·캘린더 목록 삭제(선택은 남김), 연결 해제는 `gcal_events`와 설정까지 모두 삭제. 켜져 있고 연결됐는데 권한이 없으면 `read.error` = 권한 안내(꺼져 있어도 거절 안내는 보임).
- **오류**: 401 → 토큰을 버리고(반영과 같음) 일정 삭제, 403(한도 아님) → `granted: false`·안내(일정 유지), 5xx·네트워크 → `read.error`만(일정 유지), 한 캘린더만 404/권한 403이면 그 캘린더만 건너뛰고 error에 적음. 반영(push)이 401이어도 일정 삭제.
- **가져오는 때**: `GoogleLink.fetch_soon`(한 번에 하나, 도는 중에 요청이 오면 끝난 뒤 한 번 더) — 앱 시작(켜짐·권한·연결), 동기화 뒤(**Canvas 캐시가 바뀌었을 때만**, 120초 안에 가져왔으면 건너뜀),
  15분마다(켜졌을 때 데몬 타이머 하나, `App(..., gcal_read_every=초)` 기본 900·0이면 없음, `app.gcal_read_stop`으로 멈춤), 켜기·캘린더 고르기·다시 가져오기·동의 뒤. 상태 읽기는 네트워크를 쓰지 않는다.
- **요청 코드**: `/api/google/read` 잘못된 값 400, 켤 때 연결 안 됨·클라이언트 없음 400. `/read/calendars`·`/read/refresh`는 꺼짐·연결 안 됨·권한 없음 400, 목록 50 초과·모르는 id 400(빈 목록 허용, 뺀 캘린더 일정은 바로 지움). 잠금 시간 초과 409.
- **함께**: 공부 일정은 busy·종일 아닌 일정과 겹치는 날을 건너뛰고 "일정과 겹쳐 뺀 날: …(구글 캘린더의 다른 일정)"(날짜만), +21일 너머는 "…21일 뒤까지만 확인했어요".
  브리핑은 마감·시험이 있는 날에만 끝나지 않은 busy 일정 수를 함께 센다("화요일이 고비예요 — 마감 1개·일정 3개", 제목 없음). 백그라운드 알림(toast_card)에는 일정을 넣지 않는다.
  액션 `gcal_read`(되돌리기 가능, 그 뒤 사용자가 바꿨으면 409)·`gcal_refresh`. 권한이 없으면 400 "다른 일정을 읽을 권한이 아직 없어요…"(거짓 성공 없음, 동의를 대신 열지 않음) → 화면이 설정 → 구글 캘린더 칸을 연다.
  에이전트·AI CLI·알림 코드는 `gcal_events`를 읽지 않는다(소스 검사 테스트).
- **화면(U)**: 대시보드 날짜 축 위에 일정 띠(종일은 얇은 띠·여러 날 이어짐, 시간 일정은 6px 막대·점, 겹치면 줄을 나누고 넘치면 "+n", 한가함은 속이 빈 표시, 누르면 목록의 그 줄로),
  범례 "다른 일정"(캘린더별 색 점·이름·개수, 누르면 숨기기 — localStorage `lms-agent.evhide`), 날짜별 목록 "날짜별 마감·다른 일정"(마감 아래 '다른 일정' 줄, [구글에서 열기]), 옆 칸 '캘린더'에 가져온 수.
  흰 바탕 대비가 2 미만인 연한 구글 색(파스텔)은 점·막대에 얇은 먹색 테두리. '일정 N'(승인한 마감)과 헷갈리지 않게 늘 "다른 일정"이라 부른다. 설정: 켜기·[권한 허락하기]·캘린더 고르기(색 점)·마지막으로 가져온 때·[다시 가져오기].
- **데모**: 가짜 구글이 목록(페이지 나눔)·일정(singleEvents 펼침·timeMin/Max·startTime 순)을 주고, 읽기 권한 없이는 403 insufficientPermissions. 캘린더: 내 캘린더(분홍 `#f691b2`)·동아리(초록)·대한민국의 휴일(노랑)·가족(색 없음 → 팔레트)·학과 행사(기본 숨김), 기본 12개.
- **함께 고친 것**: 소식 카드 목록이 좁은 창(약 860px)에서 왼쪽 칸이 0이 되어 한 글자씩 줄바꿈되던 문제(왼쪽 최소 45%, 480px 아래는 한 줄로).
- **확인 안 된 것**: 실제 구글 계정(특히 '바쁨만 보기'로 공유된 캘린더에서 events.list가 무엇을 주는지 — 403이면 그 캘린더만 건너뜀), 매주 말고 다른 반복 규칙(가짜 구글은 매주 COUNT만 펼침).

## 24. 이어지는 대화·일반 대화·과제 설명의 첨부 — v0.7.2 (사용자 제보 2026-11-14)

사용자: "에이전트와 지속적인 대화 불가. 대화할 때마다 새로운 창이 뜨게 됨", "project 2 공지 사항은 잘 읽는 거 확인했는데 거기에 첨부되어 있는 첨부파일은 못 보는 문제"(Canvas 과제 Project2 설명의 `project2-1.pdf`),
"에이전트가 학업적인 얘기만 하고 다른 질문에 대한 얘기를 일절 받아주질 않음".

### 24-0. 파일 소유

| 담당 | 파일 | 테스트 |
|---|---|---|
| A 첨부 | `core.py`(`_announcement_files` → 공지·과제 설명·강의계획서의 파일 링크), `materials.py`(필요하면), `checklist.py`(그 과제에 붙은 PDF를 요건 글로), `mock_canvas.py`(데모 과제 설명에 파일 링크 하나) | `tests/test_linked_files.py`(새), 기존 유지 |
| C 대화 서버 | `agent.py`, `agent_cli.py`, `mock_ai.py`, `features/agent.py` | `tests/test_agent_chat.py`·`tests/test_agent_qa.py` 유지, `tests/test_agent_convo.py`(새) |
| U 화면 | `web/index.html`만 | (리드) |
| 리드 | 문서·버전 | 전체 |

### 24-1. 이어지는 대화 (C·U)

- 대화(conversation) = 묻고 답한 차례(turn)들의 묶음. 지금의 '대화 칸(thread)' 하나가 차례 하나다(반영·되돌리기는 지금처럼 차례마다 — `thread` id·제안 번호).
- `POST /api/agent/ask {"message", "course_id"?, "conversation"?: id}` → `{"job", "conversation", "agent"}`. `conversation`이 없거나 모르는 id면 새 대화(`c-<hex>`)를 만든다. 화면은 지금 대화 id를 들고 다니다가 [새 대화]에서 비운다.
  차례 칸 추가: `conversation`(옛 차례는 없음 → 화면은 각각을 따로 된 대화로 본다). 남기는 차례 수 `MAX_THREADS` 20 → 80.
- `state.agent.conversations = [{"id", "title"(첫 질문 40자), "started_at", "last_at", "turns": n}]`(최근 먼저, 최대 20). `POST /api/agent/clear`는 지금처럼 전부 지우고,
  `POST /api/agent/clear {"conversation": id}`는 그 대화만.
- **앞 차례를 이어받기**: 같은 대화의 최근 차례(최대 6)를 `history`로 넘긴다 — `[{"message", "answer", "kind", "course_id"}]`.
  - 과목: 이번 메시지에 과목이 없고 `course_id`도 없으면 바로 앞 차례의 과목(있으면)을 쓴다("ASIC 기말 범위는?" → "그럼 중간은?").
  - 생략된 질문: 짧고(25자 이하) '그럼/그러면/그건/거기/그 과목/또/그리고/중간은/기말은…'처럼 앞 차례에 기대는 말이면 앞 메시지의 주제 낱말(범위·형식·날짜·점수 등)을 더해 갈래를 정하고 검색한다(만약에·메일·계획도 앞 과목·항목을 이어받음).
  - AI 작업(assist·answer·chat)의 입력에 '이전 대화 / previous turns'(가림 처리, 최대 1,500자)를 더한다. 이전 답의 숫자·날짜는 근거가 아니다(인용 검증은 지금처럼 찾은 글에서만).

### 24-2. 일반 대화 (C)

- 갈래 7번째 `kind "chat"`: 앱 명령·메일·계획·만약에·놓친 것이 아니고, question 검색에 관련 글이 없거나(인용 0) 처음부터 학업과 무관한 말(인사·잡담·고민·일반 지식·날씨·추천…)이면 chat.
  학업 질문인데 글을 못 찾은 경우는 지금처럼 question('찾지 못했어요')이 먼저 — 다만 AI가 켜져 있으면 그 답 끝에 일반 지식으로 덧붙일 수 있다(chat 작업, "강의 글에는 없어서 일반적인 설명이에요"라고 밝힘).
- AI가 켜져 있으면 AI 작업 `chat`: 한국어, 페이스메이커 말투(같이 달리는 친구, 짧고 따뜻하게), 이전 대화 이어받기, 이 앱이 할 수 있는 일은 안내해도 되지만 **했다고 하지 않음**(실행은 [실행] 단추로만),
  과목 사실(마감·점수·범위)은 지어내지 않음(모르면 그 과목 글을 묻게), 제안(proposals) 없음, 인용 없음. 입력: 메시지·이전 대화·짧은 상태 요약(이번 주 마감 수·km·다음 메달 — 성적 숫자·개인정보 없음, 가림).
  답 2~8문장, 1,200자 이내. 위험한 요청(자해·불법 등)은 정중히 거절하고 도움 받을 곳 안내. AI 실행 한도·기록은 지금 규칙 그대로.
- AI가 꺼져 있으면 규칙으로: 인사("안녕"·"hi") → 인사 + 오늘 할 일 한 줄(이번 주 마감·km), 감사 → 짧은 답, 응원·힘듦("힘들어"·"지쳤어") → 페이스메이커 응원 + 다음 이정표, 오늘 날짜·요일·시각 → 지금 시각,
  그 밖 → "학업 밖 이야기는 설정 → AI에서 AI 도움을 켜면 같이 나눌 수 있어요" + 할 수 있는 것 예시 2개. `by: "rules"`.
- 대화 칸: `kind "chat"`, `answer`, `by`, `notes`. 화면은 말풍선 하나(인용·단추 없음).

### 24-3. 과제 설명·강의계획서에 링크된 파일 (A)

- 지금은 Canvas **공지** 본문에 링크된 파일(`/courses/<c>/files/<id>`, `/files/<id>`, `/api/v1/files/<id>`)만 첨부로 모았다. 같은 방법으로 **과제 설명**(`assignments[].description`)과 **강의계획서**(`courses[].syllabus_body`)의 링크도 모은다.
  가상 게시판(source "canvas"): 과목마다 `board_id` "announcements"(공지) · "assignments"(과제) · "syllabus"(강의계획서). 과제 게시판의 글 = 과제 하나(`id` = 과제 id, 제목 = 과제 이름, `post_url` = 과제 html_url, 시각 = 과제 created_at/updated_at).
  새 파일만 파일 API로 이름·크기를 묻고, 지난번에 안 것은 다시 쓴다(지금처럼). 한 파일이 여러 글에 링크돼 있어도 내려받기는 한 번.
- 내려받기·글자 뽑기는 지금 materials 그대로(첨부 자동 받기 설정을 따름) → `pdf_texts` → `pdf_source`(마감 찾기)·에이전트 검색·체크리스트에 쓰인다. 제외(hidden)한 과목은 받지 않는다(지금 규칙).
- PDF 글자가 어느 과제에 붙은 것인지 알 수 있게 `pdf_texts[key]`에 `post`(가상 게시판 글 id)·`board_id`를 남기고, 체크리스트(22-6)는 그 과제(항목의 Canvas 과제·연결된 과제)에 붙은 PDF 글자도 요건 글로 읽는다.
- 데모: 과제 하나(예: 캡스톤 Final Report 또는 반도체공학 과제 2)의 설명에 파일 링크 하나(제출 요건·날짜 없는 문장이 든 작은 PDF)를 넣어 데모에서 보이게. 다른 테스트 기대값이 바뀌면 이유를 적고 맞춘다.

### 24-4. 자료 화면: 과목별 접기·분류별로 나눠 보기 (사용자 추가 요청 2026-11-14)

사용자: "과목별로 자료가 방대하여 다른 과목을 보기 위해 많은 스크롤을 내려야 한다 → 불릿화를 해서 열고 닫을 수 있는 페이지로, 목록별로 분류하여 과제·공지사항 나눠서 볼 수 있게.
완료 기준: 과목별로 분류해서 다운받은 자료를 볼 수 있도록."

- 서버(리드, `features/materials.py`): `state.materials.items[].category` = `assignment`(과제)·`announcement`(공지)·`lecture`(강의자료)·`syllabus`(강의계획서)·`board`(기타 게시판),
  `state.materials.categories = [{"id", "label"}]`(이 순서). 가상 게시판은 post_key(`board:announcements-`·`board:assignments-`·`board:syllabus-`)로, 그 밖은 게시판 이름 낱말로
  (과제·숙제·레포트·보고서·실습·프로젝트·assignment·homework·hw·lab·project·report / 공지·알림·notice·announce / 강의자료·수업자료·자료실·강의노트·강의록·교안·lecture·slide·material·note / 강의계획서·syllabus) — 특정 과목 이름에 기대지 않음.
- 화면(U): 자료 화면을 과목별 접기 묶음으로(과목 머리: 이름·파일 수·최근 받은 때, 누르면 펼치기/접기, [모두 펼치기]·[모두 접기]), 과목 안은 분류별 소제목(과제·공지·강의자료·강의계획서·기타 게시판 — 파일이 있는 분류만, 개수)
  과 그 아래 글(게시판 글 제목) 묶음 → 파일 줄(이름·크기·날짜·[열기]). 분류 칩으로 전체 과목을 한 분류만 보기(예: 과제만). 처음에는 가장 최근에 받은 과목 하나만 펼침, 펼침 상태는 이 브라우저에 기억.
  제외한 과목은 맨 아래 접힌 '제외한 과목' 묶음. 검색 칸(파일·글 제목)이 있으면 맞는 과목을 저절로 펼친다.

### 24-5. 구현 결과 (v0.7.2, 서브에이전트 A 첨부·C 대화 서버·U 화면 → 리드 통합)

- **대화(C)**: `POST /api/agent/ask`의 `conversation`이 없거나 모르는 값·문자열이 아니면 `c-<10자리 16진>`을 만든다. `POST /api/agent/clear`: 본문 없음/`{"conversation": null}` = 전부, `{"conversation": id}` = 그 대화만(모르는 id 404, 빈 값 400).
  차례 칸 추가: `conversation`, `carried`(`{"from", "course_id", "course_short", "message"}` — 과목만 이어받았으면 message null), `course_context`, chat만 `intent`(crisis·cheer·time·greeting·thanks·help·general·other),
  AI 덧붙임이 있는 question만 `general: {"answer", "by": "ai"}`(answer 끝에도 빈 줄 뒤로 들어 있음). 옛 차례(conversation 없음)는 그 차례 id(`t-…`)가 대화 id(`legacy: true`), 그 id로 이어 물을 수 있다.
  `state.agent.conversations[]`에 `legacy`, `running`도 있다. `limits`에 `history_turns` 6·`max_threads` 80·`max_conversations` 20, 묻기 한 번의 AI 호출은 답하기·덧붙임을 합쳐 2번(하루 30번은 그대로).
- **이어받기(C)**: 같은 대화의 끝난 차례 최근 6개(오류·답하는 중 제외). 앞 차례 = chat이 아닌 가장 최근 차례(잡담은 아무것도 넘기지 않음). 이어받은 과목에 관련 글이 없으면 전체 과목에서 다시 찾고 이어받기를 버린다.
  메일·계획·만약에는 늘 이어받은 과목을, 명령은 과목을 직접 말하거나 따라 묻는 말("그 과목 P/NP로 바꿔줘")일 때만. 따라 묻는 말 = 25자 이하이면서 '그럼·그러면·그건·그거·거기·그 과목·또·그리고·다른 건·나머지·and/what about…'로 시작하거나,
  두 낱말 이하로 '은?·는?·도?·건?'으로 끝남 → 앞 메시지의 주제·질문 낱말을 더한 뜻으로 읽는다("그럼 중간은?" → "중간은? 범위 어디까지야?", 학점 낱말은 바꿔 끼움). '또 다른 건?'은 앞 차례에서 인용한 문장을 뺀다.
  AI 입력에는 가린 '이전 대화'(1,500자, 최근 것 먼저)와 이어 읽은 뜻을 한 줄 더한다. 앞 답은 근거가 아니다(근거·인용 검증은 지금처럼 찾은 글에서만).
- **일반 대화(C)**: 갈래 순서 actions → mail → plan → whatif → chat_intent → missing/question → chat(남은 것). 바로 chat: 위기(가장 먼저), 응원(질문이 아닐 때), 시각·인사·감사·도움말·일반(학업 낱말이 없을 때).
  학업 질문인데 인용 0이면 question 그대로이고 AI가 켜져 있으면 "강의 글에는 없어서 일반적인 설명이에요 — …" 덧붙임. AI 작업 `chat` 검증: 1,200자·8문장·링크 넘으면 다시 묻고 마지막엔 줄임,
  앱 동작을 했다고 하거나 메시지·상태에 없는 날짜·학점·'N점'을 말하면 규칙 답으로(지어내지 않게). 위기 메시지의 AI 답에 도움 연락처(109·1577-0199·학교 상담센터·112/119)가 없으면 하네스가 붙인다.
  규칙 답: 인사(이번 주 마감·가장 가까운 마감·페이스), 감사, 응원(페이스·다음 이정표·가장 가까운 마감), 시각(날짜·요일·시각·주차), 도움말(할 수 있는 일 6가지와 예시), 위기(돌봄 말 + 연락처, 페이스 없음),
  그 밖(강의 글에서 못 찾았다고 + "학업 밖 이야기는 설정 → AI에서 AI 도움을 켜면…" + 예시 둘).
- **첨부(A)**: `core._linked_files`(옛 `_announcement_files`) + 공개 `file_links(html, host)`. 알아보는 링크: `/courses/<c>/files/<id>`, `/files/<id>`, `/api/v1/(courses/<c>/)files/<id>`, `/groups|users/<n>/files/<id>`, `/courses/<c>/files?preview=<id>`(절대·상대·프로토콜 상대,
  `/download`·`/preview`·`?wrap=1`·`?verifier=`), 주소 경로 맨 앞에서만. Canvas가 아닌 호스트의 절대 주소는 Canvas 모양(`/courses/<n>/files/<id>`·`/api/v1/…`)일 때만. 한 글 안에서 같은 id는 한 번.
  가상 게시판(과목마다 공지 → 과제 → 강의계획서 순, source "canvas"): 과제 글 = 과제 하나(`post_key` `board:assignments-<과제 id>`, 폴더 `<과목>/과제/<과제 이름>/`), 강의계획서 글 id = 과목 id(`post_url` `…/assignments/syllabus`).
  `downloads`·`pdf_texts`에 `board_id`·`post`·`canvas_file_id`(옛 기록은 post_key에서 읽음), PDF 출처에 `board_id`·`post`·`attached_to: [{board_id, post, title, url}]`(그 파일이 붙거나 링크된 모든 글).
  같은 과목·같은 Canvas 파일 id는 한 번만 받는다(이미 받은 것 우선, 아니면 LearningX → 공지 → 과제 → 강의계획서 순). 체크리스트는 그 과제의 설명 다음에 `attached_to`가 그 과제인 PDF를 읽는다(출처 "첨부 '<파일>'", 주소 = 과제 페이지).
  기존 고침: LearningX를 못 읽어 지난 게시판을 쓸 때 가상 게시판까지 두 번 들어가던 문제. RULES_VERSION은 그대로(다음 보통 동기화에서 받음).
- **화면(U)**: 채팅 서랍 = 지금 대화의 말풍선(내 말 오른쪽·답 왼쪽, 답하는 중 표시, 새 답이 오면 그 차례 위로), 위쪽 대화 막대(제목·[이전 대화 n ▾]·[신규 대화]), 대화 목록은 서랍 안(이 대화 지우기·모든 대화 지우기, 확인 한 번),
  Esc는 확인 → 목록 → 서랍 순으로 닫는다. 서랍을 열면 닫힌 동안 답이 온 대화 → 이 페이지에서 고른 대화 → 가장 최근 대화. 보내는 단추는 '보내기'. 빈 대화에만 예시 칩 7개('요즘 너무 지쳐요' 포함).
  이어받은 과목은 내 말 아래 "앞 대화의 과목 ‘X’에서 찾았어요"(리드). 자료 화면 상태는 localStorage `lms-agent.materials`(펼친 과목·고른 분류·제외 묶음), 실행 파일은 [열기] 대신 [폴더 열기].
- **확인 안 된 것**: AI를 켠 채팅(가짜 AI로만), 실제 화면 읽기 프로그램, 실제 계정의 과제 첨부 받기(다음 동기화에서 — 데이터 구조는 실제 데이터에서 확인: Project1·Project2·Report에 링크 2개씩).

## 25. 엑셀(xlsx·xls)·CSV 점수표도 성적 후보로 — v0.7.3 (베타테스터 제보 2026-11-14)

제보: "'디지털통신' 과목은 엑셀로 파일 첨부하였는데 성적 반영이 안 된 것 같다(CLI를 연결 안 해서인지 모르겠다)".
원인(리드 확인): 첨부 내려받기(materials.fetch)는 **PDF에서만** 글자를 뽑고(`pdf_texts`), 성적 후보(`gradebook.score_file_candidates`)는 그 글자만 읽는다.
엑셀 첨부는 파일로 저장만 되고 내용을 읽지 않았다(`grades.xlsx_stats`는 있었지만 아무도 부르지 않음). AI(CLI) 연결과는 관계없다.

### 25-0. 파일 소유

| 담당 | 파일 | 테스트 |
|---|---|---|
| X 읽기 | `sheets.py`(리드가 만든 빈 모듈 — 이름·모양 고정) | `tests/test_sheets.py`(새) |
| Y 연결 | `materials.py`(표 파일 글자·표 뽑기, 받아 둔 파일 다시 읽기), `scorefile.py`(표 → 점수 결과), `gradebook.py`(후보·대기 수), `mock_canvas.py`(데모 엑셀 점수표 하나), `grades.py`(xlsx_stats는 sheets를 쓰게 — 기존 테스트 유지) | `tests/test_sheet_scores.py`(새), 기존 유지 |
| 리드 | 문서·버전·화면 글(필요하면) | 전체 |

### 25-1. 표 파일 읽기 (X, `sheets.py`)

- `read(data: bytes, filename: str = "") -> list[{"name": str, "rows": list[dict[int, str | float]]}]` — 시트마다 행 목록(행 = 열 번호(0부터) → 값, 빈 칸은 없음). 
  - **xlsx·xlsm**(zip, `xl/workbook.xml`): **모든 시트**(숨긴 시트는 빼고 notes), 공유 문자열·inline 문자열·숫자·수식의 저장된 값·불리언 무시·오류 무시, 셀 주소(r)로 열 위치, 날짜 서식 숫자는 숫자 그대로.
  - **xls**(옛 엑셀, OLE2 복합 문서 `D0 CF 11 E0`, BIFF8 'Workbook' 스트림): BOUNDSHEET·SST(+CONTINUE)·LABELSST·LABEL·NUMBER·RK·MULRK·FORMULA(숫자 결과)·BOOLERR 무시, 시트마다.
    BIFF5 이하·암호 걸린 파일은 ValueError("옛 형식(…)이라 읽지 못했어요 — 엑셀에서 .xlsx로 저장해 다시 올려 달라고 …").
  - **csv·tsv**: UTF-8(BOM)·CP949(EUC-KR) 자동, 구분자 쉼표·탭·세미콜론 추측, 숫자처럼 보이는 칸은 그대로 문자열(숫자 판단은 쓰는 쪽).
  - 형식은 내용(매직 바이트)으로 먼저, 다음 파일 이름 확장자로. 한도: 시트 20·시트당 행 5,000·열 200(넘으면 자르고 첫 시트 결과에 상관없이 읽은 만큼).
  - 못 읽으면 `ValueError`(한국어, 파일 내용·주소 없음). 매크로·외부 연결은 실행하지 않는다(값만 읽음).
- `kind_of(data: bytes, filename: str) -> "xlsx" | "xls" | "csv" | None` — 표 파일인지.
- `to_text(sheets, limit=60000) -> str` — 검색·마감 찾기·체크리스트용 글: 시트마다 `[시트 이름]` 줄 다음 행마다 칸을 탭으로 이은 줄(숫자는 정수면 정수로).

### 25-2. 받기·연결 (Y)

- `materials.fetch`: 표 파일(`sheets.kind_of`)이면 `sheets.read` → `texts[key] = {"text": to_text, "sheets": 읽은 표(한도 안), "kind": "sheet", "sheet_kind": "xlsx"|"xls"|"csv", …(PDF 기록과 같은 칸)}`,
  다운로드 기록에 `sheet_ok`·`sheet_notes`(못 읽은 이유). PDF가 아니어도 `pdf_texts`에 함께 둔다(이름은 옛것이지만 '첨부에서 뽑은 글'의 저장소).
- **받아 둔 파일 다시 읽기**: 동기화 때 `downloads` 중 표 파일인데 `pdf_texts`에 표 기록이 없거나 `sheet_version`이 낮은 것을 디스크에서 다시 읽는다(한 번에 20개, 다운로드 폴더 안·크기 한도 안만).
  v0.7.2 이전에 받아 둔 '디지털통신' 엑셀도 다음 동기화에서 읽힌다.
- `scorefile.parse_sheet_results(sheets, student_id, filename) -> list[ScoreFileResult]`: 시트 하나 = 표 하나(PDF의 표 여럿처럼, 시트가 여럿이면 notes). 머리글 행(글자 칸 2개 이상, 위의 제목 줄 건너뜀),
  학번 열(정확히 같은 값 — 숫자 학번 `2023123456.0`도 — 또는 보이는 자리가 맞는 가린 학번이 한 행뿐), 숫자 열마다 내 값·평균·표본표준편차·인원·등수·최고·최저·만점(머리글 '(10)'·'/10'·'만점/배점' 행),
  학번·이름·번호·학과·등수·백분율 열과 요약 행(평균·표준편차·합계·최고·최저…)은 통계에서 뺌, 결시·빈 칸은 뺌(notes). 그 뒤는 PDF와 **같은 규칙**(`_table_kind` → 열마다 다른 평가면 `_per_column`, 세부면 합계 열 대표)
  — 가능하면 `_parse_table`의 뒷부분을 함수로 나눠 함께 쓴다(같은 결과를 두 번 구현하지 않게).
- `gradebook.score_file_candidates`·`score_files`: 표 기록(`sheets`)은 `parse_sheet_results`로, 후보 key는 지금처럼 `pdf:<첨부 키>(#<평가>)`, `source_title` = 파일 이름, `source_type` "pdf" 그대로(화면 호환) + `file_kind: "sheet"`.
  점수표로 볼지: 파일 이름(score·점수·성적·grade…) 또는 표 모양(학번 같은 칸 + 숫자 칸인 행 5개 이상).
- 데모: 한 과목 공지에 엑셀 점수표(`…성적.xlsx`, 데모 학번 줄 포함, 가짜 학번만) 하나 → 데모 성적 화면에 후보가 보이게. 다른 테스트 기대값이 바뀌면 이유를 적고 맞춘다.
- 개인정보: 표 전체는 이 PC의 state.json에만(PDF 글과 같음), 화면·API·AI에는 내 값과 반 집계만(다른 학생 행·학번은 결과에 넣지 않음 — 지금 규칙).

### 25-3. 구현 결과 (v0.7.3, 서브에이전트 X 읽기·Y 연결 → 리드 통합)

- **읽기(X, `sheets.py`)**: `kind_of`·`read`·`read_with_notes(data, filename) -> (sheets, notes)`(notes는 개수만 — 시트 이름·내용 없음)·`to_text`·`SheetError(ValueError)`·`VERSION = 1`.
  한도 시트 20·행 5,000·열 200(넘어 잘린 시트에 `"truncated": True`), 받은 파일 100MB, xlsx 압축 풀기 50MB(zip 머리글로 먼저 검사, DOCTYPE·ENTITY 거절), xls 스트림 50MB.
  보이는 워크시트만(숨김·차트·매크로·빈 시트 뺌 — 숨긴 시트에 점수가 있어도 읽지 않음). 숫자 학번은 `2023123456.0`, 날짜는 일련번호 숫자. 행 키는 정수지만 state.json을 거치면 글자 → 읽는 쪽이 `int(k)`.
  xls: 자체 OLE2 읽기(512·4096 섹터, DIFAT, 미니 스트림) + BIFF8(BOUNDSHEET·SST+CONTINUE·LABELSST·LABEL·RSTRING·NUMBER·RK·MULRK·FORMULA 숫자/글자). CSV: UTF-8(BOM)·UTF-16(BOM)·CP949, 구분자 쉼표·탭·세미콜론.
  못 읽는 것(한국어 이유): .xlsb, 엑셀 95 이하, 암호 걸린 파일, .xls 이름의 HTML·XML 표(`E_HTML`) — 모두 "엑셀에서 .xlsx로 저장해 다시 올려 달라고…". 글자 표인 .xls는 CSV로 읽음. 실제 엑셀이 저장한 파일로 확인.
- **연결(Y)**: `pdf_texts`의 표 기록 = `text`(to_text, 60,000자)·`sheets`(칸 키는 글자, 파일당 20,000칸·칸당 200자)·`kind: "sheet"`·`sheet_kind`·`sheet_version`(1)·`sheet_reader`(sheets.VERSION) + PDF 기록과 같은 칸.
  표 파일 다운로드 기록에 `sheet_ok`·`sheet_notes`·`sheet_kind`·`sheet_version`·`sheet_reader`·`sheet_read_at`. 모든 점수 파일 후보에 `file_kind`("pdf"|"sheet"), key·source_type은 그대로.
  다시 읽기는 `materials.plan()`이 만든다(`"reread": True` 일 — 실제 동기화 경로 `core._download_new`도 탄다): 표 확장자이면서 `sheet_version`/`sheet_reader`가 없거나 낮은 것, 한 번에 20개(최신 글 먼저),
  다운로드 폴더 안(심볼릭 링크 풀어서)·크기 한도 안만, 이름만 표인 파일은 '못 읽음'으로 남겨 매번 다시 시도하지 않음. 동기화 보고의 `downloaded`는 다시 읽은 것을 빼고 센다(리드).
  `scorefile.parse_sheet_results(sheets, sid, filename, label=None)`: 시트마다 표(한 시트에 머리글이 다른 표가 위아래로 있으면 나눔), 첫 학번 행 바로 위의 글자 칸 2개 이상 행 = 머리글(그 위 제목 줄은 표 이름),
  학번(정확히 — 대소문자 무시·float 학번 — 또는 보이는 자리가 맞는 가린 학번 한 행), 빼는 열(학번·이름·번호·학과·학년·등수·백분율·비고·날짜·전화, 학번 왼쪽의 1..n, 값이 모두 10000 이상, 대부분 글자),
  요약 행·만점/배점 행·빈 칸·결시는 통계에서 뺌, 만점은 머리글 '(10)'·'[10]'·'/10'·'10점'·'(max 20)'·'(20점 만점)' 또는 만점/배점 행·제목 '만점 50점'. 그 뒤는 PDF와 같은 규칙(`_col_stats`·`_table_result`·`_split_results`로 나눠 함께 씀 — PDF 결과는 2,970줄 비교로 바뀌지 않음을 확인).
  점수표로 보는 것: 학번 열 + 점수 열이 있고, 파일 이름에 점수 낱말이 있거나 학번+숫자 행 5개 이상이면서 어떤 열에 서로 다른 값 3개 이상(시간표·명단·O/X 출석표는 후보도 '학번 확인' 안내도 없음).
  AI 보호: `gradebook._ai_target`·`_dist_target`은 반 전체 점수표(글 모양으로 판단)를 고르지 않는다(AI에는 내 값·반 집계만).
- **화면(리드)**: 자료 목록의 표 파일에 '표 내용 읽음' / '내용을 못 읽은 표'(이유 = sheet_notes 첫 줄) 꼬리표. 데모: 디지털신호처리 공지 'Quiz 2 결과 안내'(15일 전)의 `Quiz2_성적.xlsx` → 데모 학번으로 'Quiz 2' 9/10·7명 중 2등 후보.
- **확인 안 된 것**: 베타테스터의 실제 '디지털통신' 파일(형식·모양을 모름 — 위 규칙으로 못 읽으면 자료 화면 꼬리표에 이유가 보인다), 두 줄로 합쳐진 머리글(합치지 않음), 표 안의 날짜로 마감 찾기(일련번호라 안 됨).
