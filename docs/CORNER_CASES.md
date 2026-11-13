# 글 이해 규칙의 일반화 — 실제 코너 케이스 8가족

실제 고려대 Canvas 데이터에서 만난 코너 케이스를 "그 문장만" 고치지 않고, **같은 모양의 다른 글**(다른 학교·다른 과목·영어/한국어)이
들어와도 맞게 처리하도록 규칙을 구조(모양) 기준으로 바꾼 기록이다. 가족마다: 실제 사례 → 일반 구조 → 처리하는 규칙·함수 → 테스트 → 남은 틈.

공통 원칙
- 낱말 목록은 **닫힌 부류**(달·요일 이름, 호칭, 시험/과제/범위 같은 종류 낱말)에만 쓰고, 판단은 모양(뒤가 소문자인가, 마지막 낱말이
  무엇인가, 다른 과목들과 코드 모양이 같은가)으로 한다. 실제 글의 문자열을 그대로 상수로 넣지 않는다.
- 애매하면 **만들지 않는다**("가짜를 보여 주지 않는다"). 만들 때는 근거·추정을 notes에 남기고 확신도를 낮춘다.
- 마감 규칙을 바꿨으므로 `extract.RULES_VERSION = "2026.11.13-4"` — 다음 동기화에서 모든 글을 다시 읽는다.
  실제 데이터(사본)로 다시 만들었을 때 항목·항목 id·성적 연결·제안이 모두 그대로였다(사용자 결정이 끊기지 않음).

---

## 가족 1. 마침표가 문장 끝이 아닌 경우

- **실제 사례**: ASIC 공지 "… quiz session … in the regular class hour on **Oct. 22**, 2026 ." — `Oct.`에서 문장이 끊겨 날짜가 사라졌다.
- **일반 구조**: 마침표 뒤에서 문장을 끊을지는 줄임말 목록이 아니라 **앞·뒤 조각의 모양**으로 정해진다.
  `Mon./Tues./Sept./No./approx./p.m./U.S./Prof./Dr./e.g./i.e./etc./vs./Fig./Eq./Ch./Sec.`, `22 Oct. 2026`, `Thu., Oct. 22`,
  `10. 22.(목)`, `2026. 10. 22.`
- **규칙** — `extract.split_sentences` → `_is_end(left, right)`:
  1. `! ? 。` 뒤는 끊는다.
  2. 뒤가 **소문자**로 이어지면 같은 문장(`U.S. students`, `5 p.m. on Friday`, `etc. and`).
  3. 뒤가 **숫자·'(숫자'** 이면 앞 조각이 줄임말 꼴(달·요일 이름의 앞부분, 4글자 이하 영문, `approx`·`fig` 같은 절대 문장 끝이 아닌 말)일 때 같은 문장(`Oct. 22`, `No. 3`, `Eq. (2.1)`).
  4. 앞 조각 **안에 마침표가 또 있거나 한 글자**면 줄임말·이니셜(`e.g.`, `U.S.`, `J. Smith`). 단 `a.m./p.m.`은 뒤가 날짜 낱말이 아니면 문장 끝(`5 p.m. Late …` / `5 P.M. Friday`).
  5. 호칭(`Prof. Dr. Mr.`), 절대 끝이 아닌 줄임말(`vs. cf. approx.`), 달/요일 줄임말 + 날짜 낱말(`Mon. Nov. 16`)은 잇는다.
  6. **숫자로 끝난 조각**(`Nov. 20.`, `2026. 11. 20.`)은 예전처럼 잇되, **양쪽에 각자 날짜가 있고** 뒤가 대문자 영어 낱말이면 끊는다
     (`Lab 3 is due Nov. 20. Lab 4 is due Nov. 27.` → 두 마감 / `Lab 3: Nov. 20. Submit via email.` → 한 문장이라 제출 낱말을 잃지 않음).
  - 앞에 쓴 영어 요일은 날짜에 포함해 확인한다(`RX_EN_MD`의 `wdb`): `Thu., Oct. 22`, `Monday, Dec. 15` — 요일이 틀리면 "요일 불일치" 노트,
    요일을 따로 '이번 주 목요일'로 세지 않는다.
- **테스트**(`tests/test_extract.py`): `test_abbreviated_month_does_not_split_sentence`(실제), `test_other_month_abbreviations_keep_date`,
  `test_split_keeps_eg_and_ie`, `test_split_sentences_general`, `test_abbreviations_and_date_formats_keep_the_date`,
  `test_two_deadlines_in_two_english_sentences_on_one_line`, `test_weekday_written_before_english_date_is_checked`
- **남은 틈**: 영어 줄임말만 다룬다. `in the U.S. Next week …`처럼 줄임말이 진짜 문장 끝이면 두 문장이 이어진다(날짜가 둘이면 늦은 쪽).
  `;`은 문장 경계가 아니다. 숫자로 끝난 문장은 양쪽에 날짜가 있을 때만 끊는다.

## 가족 2. 학기 시점 낱말 + 종류 낱말 — 마지막 낱말이 종류를 정한다

- **실제 사례**: ASIC 게시판 "**Midterm Project** (due : 23:59 on 11/5)"가 이름 "Midterm", 종류 시험으로 잡혔다.
- **일반 구조**: 붙어 있는 낱말 덩어리에서 앞 낱말은 꾸밈말, **마지막 낱말(머리)이 종류**다.
  `Final Report`·`Midterm Presentation`·`기말 프로젝트 발표`·`중간 보고서 제출`(과제), `Final Exam Review session`(복습 — 일정 아님),
  `중간고사 대체 과제`(과제), `Quiz 3`·`Pop quiz`(시험), `중간고사 성적 발표`(알림).
- **규칙** — `extract.phrases()` / `_kind_of()` / `title_hint()` / `same_task()`:
  - 낱말 종류(`_LEX`): `exam`(시험·고사·퀴즈·exam·quiz), `task`(과제·보고서·프로젝트·실습·lab·report·paper…), `event`(발표·presentation·demo —
    학기 시점·과제 낱말 **뒤**일 때만 과제, `성적 발표`는 알림), `meta`(범위·성적·결과·복습·해설·주간·review·grades·solutions·week·slides…),
    `thru`(공지·안내·일정·시간·장소·session·notice — 종류를 바꾸지 않음), `term`(중간·기말·midterm·final — 뒤 낱말이 정함),
    `weak`(`test` — 학기 시점·번호가 붙을 때만 시험).
  - 덩어리 잇기(`_links`): 사이가 공백뿐이거나, 공백으로 둘러싸인 **모르는 낱말 하나**(`대체`, `take-home`; 조사로 끝나거나 and/the 같은 말은 제외).
  - 종류 = 투명한 말을 뺀 **마지막 낱말**의 종류. `Midterm` 혼자는 시험, `final`·`finals`·`중간` 혼자는 모름(만들지 않음).
  - 이름 = 첫 학기 시점/종류 낱말부터 머리까지, 번호가 붙은 낱말이 있으면 거기서 끝(`LAB4 보고서` → `LAB 4`).
  - `same_task`: 같은 번호의 같은 종류, 또는 **같은 학기 시점 + 같은 머리 종류**(`Midterm`↔`중간고사`, `Final Report`↔`기말 보고서`).
    `Midterm Project`와 `Midterm Exam`은 이제 다르다(전에는 같은 과제로 묶여 가짜 '마감 충돌'이 날 수 있었다).
  - 휴강 말(`NEGATIVE`)에 `no class`, `class will not be held`, `수업이 없` 추가.
  - 발표 일정(`event`)으로 만든 항목에는 "발표하는 날로 봄(제출 마감이 따로 있을 수 있음)" 노트.
- **테스트**: `test_midterm_project_is_a_task_not_an_exam`(실제), `test_real_learningx_assignment_posts`(실제), `test_term_word_plus_kind_word`,
  `test_exam_word_as_modifier_is_not_an_exam`, `test_exam_after_scope_phrase_still_found`, `test_same_task_term_needs_same_head`, `test_same_task`
- **남은 틈**: 종류 낱말 사전은 한·영 닫힌 목록 — 모르는 머리 낱말(`Final critique`, `기말 작품`)은 `final` 혼자로 보아 만들지 않는다.
  한 문장에 시험 덩어리와 다른 덩어리의 날짜가 섞이면("the midterm exam will cover … the review session on Nov 3") 여전히 시험으로 본다.
  여러 날의 발표("Dec 8 and Dec 10")는 마지막 날 하나만. 한국어 `발표` 혼자는 무시한다.

## 가족 3. 종류는 제목에만, 본문은 칸(날짜·시간·장소)

- **실제 사례**: 로봇공학개론 공지 제목 "**기말고사 공지**", 본문 "날짜: 12월 15일 / 시간: 10:30am - 11:30am / 장소: 강의실 …".
- **일반 구조**: 제목이 무엇인지 말하고 본문은 이름표(칸) 줄. `일시: 12/15(월) 10:30~11:30`, `Date: Dec 15 / Time: 10:30-11:30`,
  `시험 일시 : 2026년 12월 15일(월) 오전 10시 30분`, `When: Monday, Dec. 15, 10:30 AM`, HTML 표(`<td>날짜</td><td>12/15</td>`), `날짜 | 12월 15일`.
  그리고 제목이 일정이 아닌 경우: `기말고사 범위 안내`, `중간고사 성적 공지`, `Final Exam Review`.
- **규칙** — `extract.find_deadlines(..., title=)`:
  - 제목의 종류 = 제목에 `_kind_of` (덩어리 머리 기준) — 머리가 범위·성적·복습이면 종류 없음, `_RESULT_TITLE`(성적·결과·score…)도 없음.
  - 빌리는 줄: 날짜 칸 줄(`_FIELD_DATE`) 또는 날짜가 하나뿐인 글, **그리고** 그 줄에 휴강 말도, 자기 종류·범위·성적 덩어리도 없을 때
    (`Lab 5 보고서는 12월 1일…`, `12월 10일 복습 세션…`은 빌리지 않음).
  - 칸 구분: `:`·`|` 또는 **표 칸처럼 공백 뒤에 바로 날짜·시각**(`날짜 12/15` — HTML 표가 글로 바뀐 모양). 칸 이름 뒤의 숫자 날짜는
    마감 낱말이 없어도 날짜로 본다(`_NUM_OK_BEFORE`에 일시·날짜·date·when).
  - 시간: 같은 줄 → 뒤 두 줄 → **앞 두 줄**의 `시간:` 칸(`시간: 오후 2시부터 / 날짜: 11월 25일`).
- **테스트**: `test_title_gives_kind_to_field_style_body`(실제), `test_title_without_kind_does_not_make_a_deadline`,
  `test_title_kind_needs_field_line_or_single_date`, `test_result_titles_do_not_give_a_kind`,
  `test_title_kind_with_field_style_body_variants`, `test_title_kind_does_not_create_bogus_exams`
- **남은 틈**: 과제 제목(`Lab 5 Report`)은 제출 낱말이 제목에 있을 때만 종류를 빌려준다(실습일·제출일 구분 불가). 표에서 이름 칸과 날짜 칸이
  다른 줄로 갈라진 경우(rowspan)는 못 잇는다. 시간 칸은 앞뒤 두 줄까지만. 제목에 시험 덩어리와 범위 덩어리가 함께 있으면
  (`기말고사 일정 및 범위 안내`) 시험으로 빌려준다.

## 가족 4. 점·빗금 숫자가 날짜가 아닌 경우

- **실제 사례**: 데모 성적 메시지 "표준편차 **12.1**"이 12월 1일 시험이 됐다.
- **일반 구조**: `a.b`/`a/b` 앞에 그 숫자를 **부르는 이름**이 있으면 날짜가 아니다 — 통계(`평균 11.5점`, `avg 9.25`, `median: 8.5`),
  번호(`Fig. 3.4`, `Eq. (2.1)`, `Section 3.2`, `1.2절`, `v1.2`, `Problem 2.3`), 점수·학점(`9.5/10`, `GPA 3.8/4.5`, `score: 9/10`), 시각(`at 11.30`).
- **규칙** — `extract.find_dates`의 `RX_MD_NUM` 처리:
  - 요일이 붙지 않은 숫자 날짜는 (원래대로) 마감 말이 앞뒤에 있어야 하고,
  - 바로 앞이 통계·학점 낱말(`_STAT_BEFORE`, 점·빗금 모두)이면 아님,
  - 점 숫자 앞이 번호를 붙여 부르는 낱말(`_REF_BEFORE`: fig/eq/table/section/ch/v/version/problem/exercise/at/그림/식/표/절/장/문제…)이면 아님,
  - 과제·실습 낱말 + 점 숫자(`Lab 2.1`)는 바로 뒤에 `까지/마감/by/until/due`가 없을 때만 번호(`과제 11.20까지`는 날짜),
  - 날짜 **바로 뒤**의 점 숫자와 그에 `~`로 이어진 점 숫자는 시각(`12/15 10.30~11.30`) — `find_time`도 그 자리(`RX_T_DOT_LEAD`)와
    `10.30 am`(`RX_T_DOT_AP`), `at 10.30`(`RX_T_DOT_AT`)을 시각으로 읽는다.
  - 단위가 붙은 수(`12.5%`, `3.5점`)는 원래부터 마감 말이 없어 날짜가 되지 않는다. `9.5/10`·`3.8/4.5`는 빗금 뒤 숫자 검사로 걸러진다.
- **`11.30`은 시각인가?** — `am/pm`이 붙거나 `at`·`@` 뒤이거나 날짜 바로 뒤일 때만 시각. 그 밖의 `11.30`은 한국식 날짜 후보(마감 말이 있어야 날짜).
- **테스트**: `test_decimal_statistics_are_not_dates`(실제), `test_dotted_dates_still_parse`, `test_numbers_that_are_not_dates`,
  `test_identifiers_do_not_steal_the_date`, `test_dotted_time_range_after_a_date_is_time_not_dates`, `test_quiz_score_fraction_is_not_a_deadline`
- **남은 틈**: `퀴즈 11.19`처럼 과제·시험 낱말 바로 뒤의 점 날짜는 마감 말·요일이 없으면 버린다. `Lab 2.1 by Friday`는 여전히 2/1로 읽는다.
  쪽수 범위(`pp. 12-15`)는 원래 날짜 꼴이 아니라 해당 없음. 유럽식 `11.30`(시각)은 at·am/pm이 없으면 모른다.

## 가족 5. 점수표 PDF

- **실제 사례**: ASIC `Lab0_score-1.pdf` — 여러 낱말 머리글 "Required Items (10)", "필수 요소(10)", 소수 점수 22.5, 학번 `2026KU0004`,
  끝의 공식 "평균 (Average)" 줄.
- **일반 구조**: 조교마다 다른 표 모양 — 만점 표시(`/10`, `[10]`, `(10점)`, `(max 10)`, `(10 pts)`, `(만점 10)`, `/ 10`, 머리글 아래 `만점 20 30 50` 행),
  열 순서·학번 위치·이름 칸(한·영, 띄어 쓴 이름), 합계 이름(`합계/총점/Total/Sum`), 등수 칸, 백분율 칸, 한 PDF의 표 여러 개, 페이지마다 되풀이되는 머리글.
- **규칙** — `scorefile`:
  - `_norm_header`: 만점 표시를 한 모양으로(`(max 20)`·`(20 pts)`·`(만점 20)` → `(20)`, `/ 20` → `/20`), `_MAX_TOKEN`·`_MAX_INLINE`에 `[20]`.
  - `_MAX_ROW`(`만점/배점/full marks/possible/out of`) 행은 머리글도 학생도 아니고 열마다 만점이 된다. `Max`만 있는 행은 반 최고점일 수 있어 쓰지 않는다.
  - 학번 앞의 작은 정수는 순번, 글자 칸(이름)은 값이 아님(원래 규칙) — 학번 위치·이름 칸·열 순서가 달라도 같다.
  - `_RANK_HEAD`(등수·순위·rank) 칸은 열에서 뺀다(클수록 나쁨, 대표 열로 고르지 않음). 백분율 값(`90%`)이 있으면 `_PCT_HEAD` 이름(백분율·percent)도 뺀다.
  - 공식 평균을 열에 맞출 때 빠진 칸을 건너뛴 위치(`col_at`)로 맞춘다(전에는 빈 칸·등수 칸이 있으면 한 칸씩 밀렸다).
  - `_split_tables`: 행 묶음 사이의 머리 줄에 **다른 평가 이름**(`Lab1 Scores`, `_TABLE_LABEL`)이 있거나 **다른 열 머리글**이 나오면 새 표.
    같은 머리글 되풀이·`Page 1 of 2`는 같은 표. `parse_score_text`는 label/파일 이름과 맞는 표(없으면 첫 표)만 읽고
    "PDF에 점수표가 N개 — 'Lab0' 표만 읽음(다른 표: Lab1)" 노트.
  - 머리글의 번호: 만점 표시 바로 앞의 작은 정수는 이름의 일부(`_numbered_name`: `Quiz 1 (10)`, `Lab 2 [20]`, `HW 3 /10`; 붙여 쓴
    `과제 2(10점)`·`퀴즈1(10)`은 원래 됨). 머리글의 `|` 칸 구분선은 지운다(`순번 | 학번 | Quiz 1 (10) | Midterm (100)`).
  - **한 평가의 세부인가, 서로 다른 평가인가**(`_table_kind`, 베타 사용자 제보 "성적 자동 입력이 안 된다" — 표 하나에 Quiz 1과 Midterm):
    합계 열(`_TOTAL_STRICT`: total/sum/합계/총점…, 'final'·'score'는 평가 이름일 수 있어 뺌)이 있거나, 다른 열 만점의 합이 한 열의 만점이고
    그 열 이름에 다른 평가 낱말이 없으면(`Grade (50)` = 10 + 25 + 15, `HW (20)` = HW 1 + HW 2) → **세부(parts)**: 결과 하나, 대표 = 합계 열.
    아니고 모든 열 이름에 평가 낱말(`links.families`: quiz·퀴즈·midterm·중간·final·기말·hw·과제·lab·실습·project·report·exam·시험…)이 있으면
    → **따로(separate)**: 열마다 결과(label = column = 열 이름, 번호 포함), 내 칸이 빈 평가는 빼고 노트. 애매하면 세부(예전 동작).
  - **`parse_score_results(text, student_id, label=None, filename=None) -> list[ScoreFileResult]`**: 평가마다 결과 하나씩(표마다 ×
    세부/따로). 결과가 둘 이상이면 `ScoreFileResult.key`(새 필드, 기본 None)가 결과마다 다르다(표 이름 또는 열 이름; 여러 표의 따로 열은
    '표 이름 열 이름') — 성적 후보 key `pdf:<파일 key>#<key>`용. 결과가 하나면 key None(기존 후보 key 그대로).
    `parse_score_text`는 예전처럼 결과 하나(따로 표에서도 마지막 열이 대표, 다른 열은 notes).
- **테스트**(`tests/test_scorefile.py`): `test_multi_word_column_names_keep_their_max`·`test_decimal_scores_letter_ids_and_official_average`(실제),
  `test_max_marker_styles`, `test_max_row_under_header_is_not_a_student`, `test_id_not_first_name_column_and_total_not_last`,
  `test_english_name_columns_with_spaces`, `test_total_column_names`, `test_rank_column_is_not_a_score`,
  `test_rank_column_without_total_still_picks_score`, `test_percentage_column_is_dropped_with_its_name`,
  `test_two_tables_in_one_pdf_are_not_mixed`, `test_repeated_page_header_is_the_same_table`,
  `test_separate_assessments_one_result_per_column`, `test_three_separate_assessments_and_korean_headers`,
  `test_small_number_before_max_marker_stays_in_the_name`, `test_total_column_means_parts_of_one_assessment`,
  `test_blank_and_absent_cells_in_separate_columns`, `test_asic_parts_table_still_one_result` (학번은 모두 가짜)
- **남은 틈**: `gradebook.py`(이 작업 범위 밖)는 아직 `parse_score_text`만 불러 PDF 하나에 점수 후보 하나 — 표가 여럿이거나 열마다 다른 평가인
  PDF는 나머지가 후보로 나오지 않는다(섞이지는 않음). gradebook이 `parse_score_results`로 결과마다 후보(key `pdf:<파일 key>#<key>`)를
  만들면 된다. 평가 낱말이 없는 따로 열(`출석부 점수 | 발표 점수` 같은 모양)은 세부로 본다. 학년·분반처럼 숫자 값을 가진 신원 칸은
  머리글에서만 빠져 열 이름이 밀릴 수 있다(그때는 '열 1, 열 2'). 붙임표·공백이 든 학번(`2026-1234`)은 학번으로 보지 않는다.

## 가족 6. 남은 실습 개수 단서

- **실제 사례**: ASIC `Lab3.pdf` "Next labs: P&R (IC Compiler II) → STA (PrimeTime) → SRAM" → 0~6, 7개.
- **일반 구조**: 지금 번호 K 뒤에 **남은 것**을 말하는 글 — 목록(`Upcoming labs: …`, `다음 실습: A, B, C`), 번호 목록(`Upcoming labs: Lab 4, Lab 5`),
  남은 수(`Remaining labs: 4`, `3 more homework assignments`, `남은 과제: 2개`, `실습은 2번 더 남았습니다`), 범위(`Lab 4–6`, `Labs 0 through 6`),
  총수(`총 8회의 실습`), 강의 일정 표(`Week 12 | Lab 5`). 그리고 세면 안 되는 목록.
- **규칙** — `links`:
  - `hint`: 영어는 **복수**(`Next labs:`)만 목록을 연다 — 단수 `Next lab:`은 다음 실습 하나의 안내. 항목이 이름이 아니면(`bring your laptop`,
    `노트북 지참하세요` — `_NOT_TITLE`) 세지 않는다. 화살표 없는 한국어 목록은 두 항목 이상. 항목에 번호가 있으면 그 최댓값으로.
    `Remaining labs: 4` 같은 수만 있는 목록, `_remaining_patterns`(N more / N … left / 남은 … N개 / … N번 더 남)는 K (+1) + N.
    `the next 3 labs`처럼 마지막이라는 말이 없는 수는 세지 않는다.
  - `numbered`: 범위를 펼친다(`Lab 4–6` → 4,5,6, `Lab4-Lab6`) — 붙임표로 이은 이웃 번호(`실습 2-3`)는 하위 번호일 수 있어 펼치지 않는다.
  - `stated`: 개수 뒤에 `this week/per week/이번 주/매주`가 오면 학기 전체가 아님(`_PERIOD_AFTER`). 마지막으로 `_schedule` —
    주차 표시가 있는 줄 3개 이상에서 번호가 0 또는 1부터 **빈틈없이** 이어지고 주차가 늘어나면 그 개수.
- **테스트**(`tests/test_links.py`): `test_hint_next_labs_and_observed`(실제), `test_stated_counts`, `test_hint_variants`,
  `test_hint_does_not_count_unrelated_lists`, `test_stated_count_variants`, `test_numbered_expands_ranges`
- **남은 틈**: 목록이 '남은 전부'라고 보는 가정은 그대로(실제 사례의 규칙). 한국어 한 항목 목록(`다음 실습: STA`)은 세지 않는다.
  일정 표는 번호가 빈 표(1, 4)나 3줄 미만이면 세지 않는다. `Lab (3)`처럼 괄호 속 번호는 읽지 않는다.

## 가족 7. 평가 비중 줄

- **실제 사례**: ASIC 공지 "Homework assignments: 30% (w/ short reports)" 목록, 로봇공학개론 강의계획서 "Assessment: Final exam 50% / Report 20% / Project1 15% / Project2 15%".
- **일반 구조**: 표(HTML·`|`·이름 줄 + 숫자 줄), 점수 배분(`과제 30점`), 괄호 숫자(`중간고사 (30)`), 여러 줄에 흩어진 목록, %와 점이 섞인 목록,
  조건 괄호(`출석 10% (결석 3회 이상 F)`), 항목 번호(`Exam 1 (20%)`), 개수(`Projects (2): 40%`), 공유 비중(`the midterm and the final each 30%`).
- **규칙** — `weights`(이미 구조 기반이라 대부분 그대로 통과했다. 실패한 세 모양만 고침):
  - `_tokens`: 탭·`|`·두 칸 공백이 없으면 쉼표·세미콜론으로 칸을 나눈다(`Midterm, Final, Homework` / `30%, 30%, 25%`).
  - `_PAIR`: 단위 없는 수 **뒤에 단위 붙은 수가 오면** 앞 수는 번호·개수(`Exam 1 (20%)` → 'Exam 1' 20%). 병기 괄호에 숫자 허용(`Projects (2): 40%`).
  - `_expand_each`: `A and B each N%`·`A와 B는 각각 N%` — `each/각각` 앞 마디가 평가 항목 이름 둘 이상으로만 이루어질 때 항목마다 N%.
- **테스트**(`tests/test_weights.py`): `test_updated_grading_policy_announcement`(실제), `test_weight_variants`, `test_weight_variants_not_weights`,
  `test_item_numbers_counts_and_each` (+ 기존 `test_finds_full_block_high`, `test_not_weights`)
- **남은 틈**: 분수 비중(`1/3`), 범위 비중(`5–10%`), 세부가 앞에 오는 `Homework (5 × 6%) 30%`는 못 읽는다. 강의 자료 속 %(`30% of instructions…`)는
  목록·평가 낱말이 없어 버려진다(실제 ASIC 강의 PDF로 확인).

## 가족 8. '정규 수업이 아닌 과목' 제안

- **실제 사례**: "[학생] [한국어] 2026 인권과 성평등 교육 - 03분반" vs 정규 "262R (서울-학부)로봇공학개론(GENERAL ROBOTICS)-00분반".
- **일반 구조**: 학교마다 과목 코드 모양이 다르다(`262R (서울-학부)`, `CS 101`, `EE401`, 코드가 course_code에만, 코드가 아예 없음). 교육 과목은
  대괄호 대상 머리(`[학생]`, `[All Students]`), 이름 끝의 교육 낱말(교육·연수·오리엔테이션·training·orientation·compliance·module), 글 없음.
- **규칙** — `course_modes._hidden_suggest`(이제 `core._TERM_PREFIX`를 쓰지 않는다):
  - 공지·게시판·강의계획서가 하나라도 있으면 제안하지 않는다(원래대로).
  - `_CODE_HEAD`로 이름(앞 대괄호 머리를 뗀 뒤)·course_code의 학사 코드 머리를 찾고 `_shape`로 모양만 남긴다(`262R (서울-학부)` → `9A ()`, `CS 101` → `A 9`).
  - **내 다른 과목들이 함께 쓰는 모양**(`_dominant_shape`: 2개 이상, 다른 과목의 절반 이상)이 있으면: 이 과목에 그 모양이 없고 +
    (대괄호 머리 **또는** 교육 낱말로 끝남)일 때 제안.
  - 그런 모양이 없으면(코드가 없는 학교, 과목이 하나): 학사 코드 머리가 없고 + 대괄호 머리 **와** 교육 낱말이 둘 다 있을 때만.
  - 교육 낱말은 이름 **끝**에서만(분반·연도·학기·영문 괄호를 뗀 뒤) — `교육학개론`·`안전공학`은 정규 과목.
- **테스트**(`tests/test_course_modes.py`): `test_hidden_suggestion_real_name`(실제), `test_hidden_suggestion_general`,
  `test_hidden_suggestion_needs_no_posts_even_for_outlier`, `test_demo_courses_modes_suggestions_and_read_counts`
  - 바뀐 기대값 하나: 과목 하나뿐인 `"Lab safety training"`은 더 이상 제안하지 않는다(비교할 코드 모양이 없고 대괄호 머리가 없음 —
    "코드가 어디에도 없으면 제안하지 않는다"). `"[Students] Lab safety training"`은 제안한다.
- **남은 틈**: 교육 과목도 정규 과목과 같은 코드 모양을 쓰는 학교에서는 제안하지 않는다(신호가 없으니 맞는 동작). 교육 낱말 목록은 한·영뿐.
  제안 이유 문구(`HIDDEN_REASON`)는 화면 호환을 위해 그대로라 어떤 신호였는지는 말하지 않는다.
