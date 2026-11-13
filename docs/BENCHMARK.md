# 벤치마크: 첫 실행 안내·재방문·확인함 UX

조사일 2026-11-12. 링크는 직접 열어 확인한 출처다. "확인 안 됨"은 1차 출처에서 직접 확인하지 못했다는 뜻이다. 퍼센트 수치는 판매사가 자기 고객 데이터로 낸 값이다.

## 1. 제품 투어·코치마크

- **단순한 앱에서는 투어 효과가 없다.** 70명 실험에서 과제 성공률은 투어를 본 쪽 91%, 건너뛴 쪽 94%로 차이가 없었다. 체감 난이도는 투어를 본 쪽이 더 높았다. [NN/g](https://www.nngroup.com/articles/mobile-tutorials/)
- **맥락 도움말이 투어보다 낫다.** 필요할 때 보여 주고, 직접 해 보게 하고, 지나간 안내도 다시 찾을 수 있어야 한다. [NN/g 2023](https://www.nngroup.com/articles/onboarding-tutorials/)
- **코치마크는 한 번에 하나씩, 그 위치에서 보여 준다.** 글은 아주 짧게 쓴다. 설명은 약 20초면 잊힌다. [NN/g](https://www.nngroup.com/articles/mobile-instructional-overlay/)
- **Apple HIG**는 온보딩을 빠르고 즐겁고 선택 가능하게 만들라고 한다. [HIG](https://developer.apple.com/design/human-interface-guidelines/onboarding)
  - 직접 해 보며 배우게 하고, 팁은 해당 영역 옆에 둔다.
  - 건너뛰면 다시 띄우지 않고, 도움말이나 설정에서 다시 볼 수 있게 한다.
  - 권한은 그 기능을 처음 쓸 때 요청한다.
- **Material(M1) 기능 발견 안내**: 세션당 최대 1개만 보여 주고, 앱을 열자마자 띄우지 않는다. [M1](https://m1.material.io/growth-communications/feature-discovery.html)
- **완주율(판매사 데이터)**: 3단계 72%, 4단계 74%, 7단계 이상 16%다. 사용자가 눌러서 시작하면 67%, 시간이 지나 자동으로 뜨면 31%다. [Chameleon 2025](https://www.chameleon.io/blog/mastering-product-tours)
  - 진행 표시는 완주율을 12% 올렸다. 상대값인지 절대값인지는 밝히지 않았다. [Chameleon](https://www.chameleon.io/blog/product-tour-benchmarks-highlights)
- **Elastic EUI**: 투어를 자동으로 띄우기보다 원하는지 먼저 묻는다. 뻔한 내용은 투어의 가치를 떨어뜨린다. [EUI](https://eui.elastic.co/docs/components/display/tour/)
- **라이브러리 기본값**
  - [Driver.js](https://driverjs.com/docs/configuration)(MIT): 어두운 막 투명도 0.7, 강조 여백 10px, Esc로 닫기, 화살표로 이동. 다른 화면 대상에는 `waitForElement`, 직접 해 보는 단계에는 `advanceOn`을 쓴다.
  - [Shepherd](https://docs.shepherdjs.dev/api/step/interfaces/stepoptions)는 `showOn`, `scrollTo` 옵션이 있고 '동작 줄이기' 설정을 따른다.
  - Shepherd와 [Intro.js](https://github.com/usablica/intro.js)는 AGPL 라이선스다.
- **접근성**
  - 말풍선이 열리면 포커스를 안으로 옮기고, Tab은 안에서만 돈다. Esc로 닫으면 포커스를 연 요소로 돌려준다. `role="dialog"`를 붙인다. [APG](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/)
  - 포커스된 요소를 가리지 않는다. [WCAG 2.4.11](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html)
  - 애니메이션은 끌 수 있어야 한다. [WCAG 2.3.3](https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions.html), [MDN](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion)
- **빈 상태 화면**은 바로 시작할 길을 준다. 데모 데이터로 둘러보게 하는 사례도 있다. [NN/g](https://www.nngroup.com/articles/empty-state-interface-design/)

## 2. 비슷한 제품의 재방문 장치

- **Todoist**: 매일 아침 요약 메일을 보낸다. 모바일에는 '아침 개요'와 '저녁 리뷰' 알림이 있다. [도움말](https://www.todoist.com/help/articles/manage-your-notifications-in-todoist-QxQGXkMu)
- **Google Calendar**: 캘린더별·일정별로 알림을 정할 수 있다. [도움말](https://support.google.com/calendar/answer/37242?hl=en) 매일 오전 5시 일정 메일은 확인 안 됨(대학 안내에만 있음).
- **Notion Calendar**: 시작 안내의 2단계가 알림 켜기이고, 계정 연결은 그다음이다. [안내](https://www.notion.com/help/guides/getting-started-with-notion-calendar)
- **TickTick**: 정한 시각에 일일 알림을 보내고, 홈 화면 위젯이 있다. [Features](https://ticktick.com/features)
- **MyStudyLife**: 마감 위젯과 리마인더가 있다. 학기 날짜를 받아 설정하는 안내와 단계별 튜토리얼도 있다. [App Store](https://apps.apple.com/us/app/mystudylife-study-planner/id910639339)
- **Canvas**: 알림 빈도를 즉시·일일·주간·끄기 중에서 고른다. [Wharton](https://support.wharton.upenn.edu/help/managing-notifications-in-canvas-student) 캘린더 구독 반영이 12~24시간 늦다는 말은 확인 안 됨.
- **피하는 것**: 급하지 않은 일을 급한 것처럼 알리지 않는다. 알림 설정은 앱 안에 둔다. [HIG](https://developer.apple.com/design/human-interface-guidelines/managing-notifications)
- **공통점**: 사용자를 다시 데려오는 것은 앱 화면이 아니라 앱 바깥 채널이다.

## 3. 확인함·반응형

- **Linear**: 키 하나로 결정한다. 1은 수락, 2는 거절, H는 미루기다. [Triage](https://linear.app/docs/triage)
- **Gmail**: j/k로 이동하고 z로 되돌린다. [단축키](https://support.google.com/mail/answer/6594?hl=en) 보내기 취소 시간은 5~30초다. [Undo](https://support.google.com/mail/answer/2819488?hl=en)
- **확인 대화상자**는 되돌릴 수 없는 일에만 쓰고, 나머지는 되돌리기로 처리한다. [Nielsen](https://www.nngroup.com/articles/confirmation-dialog/)
- **일괄 처리 바**는 항목을 하나라도 고르면 나타나고 취소 버튼이 있다. [Carbon](https://carbondesignsystem.com/components/data-table/usage/)
- **화면 폭**: 840px부터 두 칸을 나란히 두는 구간이다. [Android](https://developer.android.com/develop/ui/compose/layouts/adaptive/use-window-size-classes) 폭 320px에서도 가로 스크롤이 없어야 한다. [WCAG 1.4.10](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html)

## 우리 앱에 적용할 것

확인함에는 이미 10초 되돌리기와, 처리한 뒤 다음 카드로 가는 포커스가 있다.

### (a) 튜토리얼 스크립트

- 첫 동기화가 끝나면 "둘러볼까요?"라고 묻는다.
- 1~4번은 핵심 투어로 이어서 보여 주고, "n/4"로 진행을 표시한다.
- 5~8번은 그 화면에 처음 들어갔을 때 하나씩만 띄운다.
- 설정의 '안내 다시 보기'에서 8단계 전체를 다시 볼 수 있다.
- Canvas에 연결하지 않았으면 데모 데이터로 진행한다.

| # | 화면 | 대상 요소 | 제목 | 본문 | 방식 |
|---|---|---|---|---|---|
| 1 | 대시보드 | `.hero` | 이번 주 마감이 한 줄에 | 앞으로 7일의 마감이 날짜 축에 찍히고, 색으로 상태를 나눕니다. | 보기 |
| 2 | 공통 | `#nav a[data-view=inbox]` | 숨은 마감은 확인함에 | 공지·메시지에서 찾은 마감 후보가 여기 모이니 눌러 보세요. | 직접 해 보기 |
| 3 | 확인함 | 첫 `.rcard .chain` | 근거부터 보세요 | 어느 글의 어떤 문장에서 찾았는지 보여 주고, 날짜는 바로 고칠 수 있습니다. | 보기 |
| 4 | 확인함 | 첫 `[data-decision=approved]` | 맞으면 일정에 넣기 | 누르면 'LMS 마감' 캘린더에 들어가고 10초 안에 되돌릴 수 있습니다. | 데모는 직접 해 보기, 실제는 보기 |
| 5 | 학기 | `.chart-wrap` | 학기를 주차로 | 막대를 누르거나 ←/→로 주차를 옮겨 보세요. | 직접 해 보기(선택) |
| 6 | 과목 | `#courses-body .tbl` | 과목별 다음 마감 | 과목을 고르면 다음 마감과 지난 과제를 한 번에 봅니다. | 보기 |
| 7 | 성적 | 첫 `.g-sec` | 내 위치는 어림값 | 평균과 표준편차를 넣으면 반에서 대략 몇 %쯤인지 추정합니다. | 보기 |
| 8 | 설정 | `#pane-google` | 한 번 연결하면 알아서 | 연결하면 앱을 열지 않아도 승인한 마감이 휴대폰 캘린더에 들어갑니다. | 보기 |

- v0.6.3: 핵심 투어 끝에 '에이전트에게 묻기'(5단계)를 붙이고, 이미 마친 사람에게는 '달라진 점'을 한 번 제안한다(계약 8장 튜토리얼 판).
- 2→3번: 화면이 바뀐 뒤 대상이 나타날 때까지 기다린다.
- 폭 1024px 미만에서는 메뉴 글자가 숨으므로, 2번 본문에 "받은편지함 아이콘"이라고 적는다.
- 마감 후보가 하나도 없으면 3·4번 대신 `#manual-form`(직접 붙여넣기)을 강조한다.
- 라이브러리를 들이지 않고 직접 만든다(런타임 의존성 금지 규칙).

### (b) 재방문 기능 (가치/노력 순)

1. **백그라운드 동기화** (가치 최상, 노력 중): Windows에 로그인하면 창 없이 시작해서 바뀐 것을 구글 캘린더에 반영한다. 화면에는 '마지막 확인 n분 전'을 보여 준다.
2. **일정 알림 기본값** (가치 상, 노력 하): `reminders.overrides`로 하루 전과 3시간 전에 팝업 알림을 넣는다. 일정 하나에 최대 5개, 최대 4주 전까지 넣을 수 있다. [API](https://developers.google.com/workspace/calendar/api/v3/reference/events)
3. **"[확인] 마감 후보 N개" 종일 일정** (가치 상, 노력 하): 앱으로 돌아오게 하는 신호다. 끌 수 있게 한다.
4. **주간 요약을 기본으로 켜기** (가치 중, 노력 하)
5. **'지난 확인 이후 바뀐 것' 배너** (가치 중, 노력 하)
6. **화면별 맥락 팁** (가치 중, 노력 하): (a)의 5~8번이다.
7. **Windows 알림 창** (가치 중, 노력 상)

메일 요약과 연속 기록 같은 게임 요소는 하지 않는다. Canvas 앱과 같은 마감을 두 번 알리지 않도록 'Canvas 과제도 넣기' 토글을 유지한다.

### (c) 브라우저 UX 체크리스트

1. '나중에'를 고르고 새로고침해도 투어가 다시 뜨지 않는다.
2. 말풍선에 `role="dialog"`와 진행 표시 "n/4"가 있다.
3. Tab이 말풍선 안에서만 돈다.
4. Esc로 닫으면 포커스가 연 요소로 돌아온다. ←/→로 단계를 옮기되, 5번에서는 화살표가 차트를 움직인다.
5. 다른 화면에 있는 대상도 화면을 바꾼 뒤 강조된다.
6. `prefers-reduced-motion`을 켜면 강조 상자가 움직이지 않는다.
7. 폭 800px에서 말풍선이 잘리지 않고 가로 스크롤이 없다.
8. 키보드만으로 카드를 승인하고 되돌릴 수 있다.
9. 데모에서는 실제 구글에 쓰지 않는다.
10. 백그라운드 동기화 뒤 앱을 열면 마지막 확인 시각과 새 후보 수가 보이고, 그 수가 `#nav-count`와 같다.
