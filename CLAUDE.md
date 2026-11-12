# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

LMS Agent: Canvas LMS에서 마감이 안 보이는 과제(이메일·종이 제출)의 마감을 공지·메시지·강의계획서에서 찾아, 근거와 함께 승인받고 .ics로 내보내는 배포용 앱. 사용자는 한국어로 대화한다.

## Commands

```bash
python -m pytest                                   # 전체 테스트 (표준 라이브러리 외 의존성은 pytest뿐)
python -m pytest tests/test_extract.py -k midnight # 하나만
PYTHONPATH=src python -m lms_agent demo            # 가짜 Canvas로 화면 열기 (설치 없이)
pip install -e . && lms-agent demo                 # 설치 후
python scripts/build_exe.py                        # PyInstaller로 dist/lms-agent.exe
```

## Architecture

- 흐름: `canvas.Canvas`(REST, Bearer 토큰) → `core.collect`(원본을 state.json의 cache에 저장) → `core.build_items`(글마다 `extract.find_deadlines` → 같은 과목·시각끼리 묶기 → Canvas 과제와 연결/중복 → 같은 과제의 다른 마감은 `conflict`) → `store.Store.view()`(사용자 결정을 덮어 status 계산) → `ics.build_ics`.
- 원본 cache에서 언제든 `core.rebuild`로 다시 만든다. 사용자 결정(`user`)은 item id로 붙으므로 **item id는 재동기화해도 같아야 한다**(found는 과목·원래 마감·제목의 해시).
- 모듈 사이 데이터 모양(Canvas 필드, 데모 시나리오, 화면 API·Item)은 `docs/CONTRACTS.md`가 기준이다. 바꾸면 그 문서와 `src/lms_agent/web/index.html`을 같이 고친다.
- `mock_canvas.py`는 데모 모드와 `tests/test_e2e.py`가 함께 쓴다. 시나리오를 바꾸면 e2e 기대값도 바뀐다.
- 진행 상황과 확인 안 된 것: `docs/PROGRESS.md`.

## Rules

- 런타임 의존성 추가 금지(누구나 설치 없이 돌아가야 한다). Python 3.9+ 문법.
- 마감 규칙을 고칠 때는 `tests/test_extract.py`에 실패 문장을 먼저 추가한다.
- 토큰은 로그·오류 메시지·응답에 절대 넣지 않는다.
