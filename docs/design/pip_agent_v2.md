# Pip Agent v2 설계

> 원칙: 모델이 해석하고 판단, 코드는 검증과 안전. 읽기는 자유, 쓰기는 근거
> 강도에 따라 바로/카드/되묻기. 카드는 사람만 답함. 실행 루프 없음.
>
> 제약: 증상별 규칙 추가 금지. 새 케이스는 원칙·구조로만 해결한다.
> 세션 시작 시 이 파일을 먼저 읽고, 이 기준으로 작업한다.

## 0. 서버 버전

- 시작 로그 1줄: `pip-server commit=<12자리> tools=<N> ph=<8자리>`
  (`uvicorn --reload` 재시작 때마다 출력).
- `GET /health`에 `commit` 포함(없으면 신설).
- 확인법: 증상과 코드가 안 맞으면 `/health`의 commit과
  `git rev-parse --short HEAD`를 비교. `calls=`에 `get_well`/`where_am_i`가
  나오면 구버전. 단 `server/data/record.json`은 gitignore 런타임 상태라
  코드 버전과 별개로 스테일할 수 있음(삭제 후 재시작하면 재시드).

## 1. 원문 그대로

- Spark 입력에 norm을 쓰지 않음. 모델은 transcript 원문을 받는다.
- 정규화는 `match_grammar` 내부 판단에만 쓰고, 완전히 확실할 때만 바꾼다.
- grounding evidence의 utterance도 원문. 더듬음("two two", "D two two")은
  WellRef·양 파서가 허용한다.
- 원칙: 시간·양 단위가 뒤따르는 숫자는 WellRef 후보에서 제외한다
  ("a 5 minute"의 5는 양, 웰이 아님). 웰은 행 문자와 결합될 때만 웰이다.

## 2. WellRef 하나

- 형식: `"C5"` | `"C2–C8"`(입력 허용: `C2-C8`, `C2 to C8`, `C2 through C8`) |
  `"C2, C5, C7"`.
- 단일 파서 `parse_wellref(s) -> (wells | None, error)`. 모든 웰 인자
  (`wells`, `well`, `near`, `target`, `original`)가 같은 타입·파서·설명을 쓴다.
- `find_free_wells`의 near가 범위면 범위 끝 다음 칸부터 탐색한다
  (같은 행 끝 열+1, 행 끝이면 기존 인접행 규칙).
- 검증 실패(None 포함 모든 입력)는 무효 웰 경로로 가고, 크래시하지 않는다.
  "note C nine nine test"의 C99는 범위 밖이라 무효다.

## 3. 입력 길 하나 + 카드 먼저

- `POST /input {text?, card_id?, answer?, session, screen, ...}` 단일 입구.
  `/voice`는 ASR 후 같은 내부 함수로 합류한다.
  (`/orchestrate`·`/confirm` 처리 방식은 미결정 D2.)
- 대기 카드가 있으면 모델·grammar 전에 `match_card_answer` 판별기를 돌린다:
  yes / no / cancel / yes to all. 판별되면 모델 호출 없이 `confirm` 경로로
  처리하고, `asked` 로그에 답을 남긴다.
- 카드 키 `(action, subject)`로 1장만 유지한다. 같은 키가 다시 생기면 기존
  카드를 지우고 새로 만든다(쌓지 않음). op별 subject: exclude→well,
  redo→wells, mark_done(catch)→wells, tip→step, dup_timer→(minutes, wells),
  next_step→step, cancel/extend→label.
- 처리 결과는 기존 `_commit`의 snapshot SSE로 즉시 반영된다.
  (전제: 앱이 `snapshot.cards`를 렌더한다.)

## 4. 읽기 자유

- `read_record(kind: wells|notes|timers|steps|history, filter, limit)` 유지.
  2라운드에서 모델이 스스로 답하고, 목록이 길면 화면에 띄우고 말로 요약한다.
- 짧은 질문은 grammar에서 코드가 바로 답한다(Spark 호출 0):
  where am I, what's next, what step, how many left.

## 5. 근거 강도

- `grounded(value, evidence) -> (ok, source)`.
  `evidence` = 이번 발화 + 최근 대화 요약 + 계획 + 이번 턴 읽기.
- 3단계: utterance·read → 바로 적용 / conversation·plan → 확인 카드
  (근거 1줄 표시, "Two minutes on D five, like before?") / none →
  쓰지 말고 되묻기. 근거 없는 값을 사람 승인으로 통과시키는 구조는 금지.
- 되묻기는 슬롯 없이 요약에 `"asked: minutes for D5"` (유지).
- asked 대상과 다른 웰에 쓰려고 하면 카드로 확인한다.
- 전제: v2의 대화 기억은 요약 모드다. `recent_turns`는 체인 플래그와
  무관하게 항상 포함한다. 체인 코드 자체의 삭제는 별도 결정(D1 보류).

## 6. 메모 출처

- verbatim 조각 맞추기를 삭제한다.
- `note = {text(모델 정리), raw(원문 전체, 불변), wells, time, route}`.
  기존 메모는 로드 시 `raw = text`로 backfill한다.
- text에 raw에 없는 새 사실(숫자·웰)이 들어 있으면 거절하고 raw로 저장한다.
  `edit_note`는 이전 값을 이력에 남기고, 새 text 검사 실패 시 기존 text 유지.
- `read_record` 메모 출력은 text 중심. 웰 카드에 text + "원문 보기",
  Pip이 읽을 때는 text.
- Hand off CSV: `well,status,detail,raw` + 메모 행 `{well},memo,"{text}","{raw}"`.
  (sim 비교는 done 목록 기반이라 영향 없음.)

## 7. 쓰기 화면 반응은 코드가

- 쓰기 적용 시 서버가 ui 액션을 자동 발행한다: 대상 웰 `flash`,
  메모면 웰 카드 `peek` 3초. 대상 도구:
  mark_done, add_note, edit_note, exclude, redo, set_timer.
- 앱: flash는 기존 show 하이라이트 재사용, peek 시트는 신규.
  기존 memoFlash 배너(2초) 처리 방식은 미결정 D3.
- 모델은 이 반응을 위해 `open`을 부르지 않는다(item 8과 세트).

## 8. 도구 설명 = 계약

- 18개 도구 설명을 전부 `(언제 쓰나 / 언제 쓰지 않나 / 예시 1개)` 형식으로 통일.
- open/navigate: 보기 요청(open, show, let me see, go to)일 때만 쓴다.
  쓰기 대신·쓰기 확인용으로 쓰지 않는다.
- add_note: 기록할 내용이 있을 때 쓴다. 내용이 없으면
  ("I want to make notes on C8") `ask`로 무엇을 적을지 묻는다.
- SYSTEM 1줄: "After a write, the screen updates itself;
  never call open to confirm."

## 9. 말투

- `shared/pip_voice.json`. 상황 키별 2–4개 변형, 자리표시자만 사실을 담당:
  `{wells}` 화면용, `{wells_spoken}` 말하기용("C two to C eight"),
  `{minutes}`, `{group}`, `{step}`, `{reason}`, `{target}`, `{label}`,
  `{count}`, `{on_done}`. 무효 웰 표시에는 `{target}`을 쓴다.
- 선택 규칙: 직전과 다른 문장(세션에 마지막 변형 기록).
  화면 표시는 `{wells}`, TTS는 `{wells_spoken}` 치환.
- 실패는 정직하게 + 다음 제안을 붙인다.
- 2라운드 답 SYSTEM: "You're Pip, a friendly lab buddy at the bench.
  Short, warm, casual (Got it / Nice / Heads up / Hmm).
  Never claim anything the record doesn't show."
- 키별 문구 (서버 조립용, 앱 표시·TTS 공용):

```json
{
  "mark_done": [
    "Nice, {wells_spoken} are in.",
    "Got it — {wells_spoken} done.",
    "{wells_spoken} logged. Nice work."
  ],
  "timer_start": [
    "{minutes} minutes on {wells_spoken}. I'll ping you.",
    "Timer's on: {minutes} minutes for {wells_spoken}.",
    "Got it — {minutes} minutes on {wells_spoken}."
  ],
  "timer_cancel": [
    "Okay, {wells_spoken} timer's off.",
    "Cancelled the {wells_spoken} timer.",
    "Done — {wells_spoken} timer off."
  ],
  "timer_done": [
    "Heads up! {minutes} minutes are up on {wells_spoken}.",
    "Ding — {wells_spoken} hit {minutes} minutes.",
    "{wells_spoken} timer done. {minutes} minutes."
  ],
  "note_saved": [
    "Noted on {wells_spoken}.",
    "Got it — noted on {wells_spoken}.",
    "Written down for {wells_spoken}."
  ],
  "ask_minutes": [
    "Sure — how long?",
    "How many minutes?",
    "For how long?"
  ],
  "confirm_exclude": [
    "Drop {wells_spoken}?",
    "Exclude {wells_spoken}?",
    "Take {wells_spoken} out?"
  ],
  "catch_off_plan": [
    "Wait — {wells_spoken} isn't in this step. Still log it?",
    "Hmm, {wells_spoken} isn't planned here. Log it anyway?"
  ],
  "not_found_timer": [
    "Hmm, I don't see a {label} timer. Want me to start one?",
    "No {label} timer running. Start one?"
  ],
  "invalid_well": [
    "{target} isn't on the plate. Which well did you mean?",
    "Hmm, {target} isn't a valid well. Which one?"
  ],
  "cant_do_physical": [
    "I can't hand things over, but I can log it or set a timer.",
    "Hands are yours — I can log it or set a timer though."
  ],
  "didnt_catch": [
    "Sorry, say that again?",
    "Didn't catch that — once more?"
  ],
  "step_move": [
    "Ready for step {step}?",
    "Move to step {step}?"
  ],
  "confirm_reuse": [
    "{minutes} minutes on {wells_spoken}, like before?",
    "Same as before — {minutes} minutes on {wells_spoken}?"
  ]
}
```

(`confirm_reuse`는 item 5의 대화·계획 근거 카드용으로 추가.)

## 10. 데이터

- `samples/plan_demo.json` 한글 → 영어 (완료됨, 구현 시 확인만).
- `server/data/` 스테일 시 삭제 후 재시작.

## 11. 테스트

- `server/tests/`: 음성 "yes" 카드 처리 시 모델 호출 0 / 같은 카드 1장 /
  메모 text+raw / "start a 25 minute timer" 25분 / "a 5 minute timer for C2" /
  "note C nine nine test" 무효 / WellRef 세 형식 / redo backup 5회 /
  "oh no cancel C five I need D five"(직전 2분) → 카드 → D5 2분 /
  "i wanna make some notes on C eight..." → add_note /
  말투 연속 2번 다른 문장.
- 쓰기 vs 보기 헷갈리는 10문장 정확도 → `eval/results/`.
- 스모크 + 지연: 질문 ≤4초, 명령 ≤3초.

## 관찰 매핑 (iPad 로그 → 설계 항목)

1. 500 crash (`invalid_message([None])`, "spare cells" 턴) →
   item 2 단일 파서. None 포함 모든 입력이 무효 경로로 가고 크래시 불가.
2. "yep" 반복에 exclude 재생성, 카드 영원히 미해결 →
   item 3 카드 우선 판별(모델 호출 0).
3. "three" → C5 타이머(C8이어야) →
   item 5 asked-대상 불일치 시 카드.
4. "D two two" norm 잔재 + R2 read drop →
   item 1+2 (확실할 때만 정규화, WellRef 더듬음 허용).
5. `chain=16` 증가, `cached≈241` → item 5 전제(요약 기억). D1 참조.

## 결정됨

- D1(보류): v2 경로는 요약 기억만 사용. 체인 코드 삭제는 추후 결정.
- D2: `/orchestrate`·`/confirm`은 내부에서 `/input`으로 넘기는 별칭으로 유지.
  앱이 `/input`으로 다 옮긴 뒤 다음 단계에서 삭제.
- D3: peek이 memoFlash 배너를 대체한다.

## Amendments (iPad 실측 라운드, 2026-10-27)

- A. 보고만 기록: 쓰기 계약에 "보고된 사실만 기록" 원칙. mark_done은
  done/finished/added/I put/washed 보고에만; wash/add/do/start the next
  같은 벤치 지시에는 기록 없이 타이머·안내·ask. exclude/record_redo/
  next_step 동일 원칙. add_note/edit_note/save_tip은 이미 보고형이라 유지.
  eval/instruction_report.py 10문장으로 검증.
- B. 카드 대상: 단순 yes/no/cancel은 Pip이 가장 최근에 말로 물어본 카드
  (세션 last_asked_cards, 미해결만)에 답. 최근 질문이 불분명(2장 이상)이면
  해결 없이 "Which one — A, or B?"로 되묻기. 앱 체크리스트는 최신 위 +
  첫 행 강조. "yes to all"은 그대로.
- C. 대화형 답: 도구 없는 모델 텍스트는 정상 대화 결과로 그대로 말함.
  didnt_catch는 받아쓰기 empty·응답 잘림·모델 완전 무응답에만.
- D. asked 수명 1턴: evidence_summary·recent_turns에 직전 턴 asked만 노출.
  답이 왔든 무시됐든 다음 턴 뒤 만료(제거).
- E. 잘림 재시도: status incomplete면 이유 로그 + 동일 설정 1회 재시도,
  그래도 잘리면 didnt_catch.
- F. R2 inline 재구성: 요약 모드 2라운드는 [user 질문, R1 function_call
  에코, function_call_output, "Continue with the looked-up data."].
  "Looked up:" 텍스트 전달은 바인딩 실패(침묵·팬텀 redo·재시도 중얼거림),
  질문 단독 반복은 새 턴으로 읽혀 재조회. 3안 실측 후 (b) 채택, V10 잠금.
- G. 정직한 텍스트: 도구 없이 기록·행동을 주장하는 모델 텍스트(noted/done/
  excluded/started/…/logged/moved, 원본 honest_reply 부활)는 pip_voice
  didnt_act("I didn't log anything — want me to?")로 대체 + asked 타겟 설정.
  R2 읽기 답은 읽기 도구가 실행됐으므로 원본 시맨틱대로 게이트 제외.
- H. 실행됐는데 말 없음: _executed_reply가 실행 결과로 발화(UI→opened,
  읽기→found+데이터 렌더, 빈 읽기→found_empty). 검증 탈락은 couldn't로.
  didnt_catch는 받아쓰기 empty·응답 잘림에만.
