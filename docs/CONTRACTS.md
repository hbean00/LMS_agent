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

- 위 1번의 엔드포인트를 같은 모양으로 흉내 낸다. 토큰이 `demo-token`이 아니면 401.
- 과제 목록은 페이지 크기 2로 잘라 `Link: <...&page=2>; rel="next"`를 준다(페이지 처리 검증용).
- 서버 객체에 받은 요청 경로 목록(`server.requests: list[str]`)을 남긴다(테스트에서 auto_mark_as_read=false 확인용).

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
