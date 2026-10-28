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

## Protocol import plan (2026-10-27)

## Goal

Import a PDF/txt bench protocol into a reviewable run plan: Spark extracts
groups/steps/times into our plan schema, code validates and grounds every
value, the user reviews (hand + voice edits), and Start run archives the old
run and begins the new one.

## Success Criteria

- Demo PDF imports to groups {ctrl/control, drug_A, drug_B}, 5 steps, n=5,
  100uL + 30min grounded, wells auto-laid on rows B/C/D cols 2-8.
- Invented numbers become needs_review (never silent); stated wells absent
  from the PDF fall back to auto layout with a flag.
- Blocking errors (overlap, bad wells, unknown group, n > wells) prevent
  Start; needs_review warns but allows it.
- Voice "change drug A to row E" moves the draft group; manual table edits
  re-validate; old run is archived under server/data/runs/.

## Approach

One-shot extraction call (no tools, no chain): PDF rides inline as
`input_file` + base64 `file_data`, output constrained by `text.format`
json_schema loaded from shared/schemas/plan.schema.json. Server validates
with hand-rolled checks (no new dep except pypdf for evidence text),
auto-lays missing wells, and holds the result in record["draft"] so it flows
to the app through the existing snapshot/SSE path. Review UI mirrors the
robot sheet pattern. Runs archive to server/data/runs/ (gitignored).

## Key Decisions

1. Inline PDF (input_file + file_data), not Files API. One-shot import needs
   no reuse, and inline leaves no server-side copy (data-safety story).
   Server cap 10MB (docs allow 50MB inline). Docs: file-handling page.
2. text.format {type json_schema, name, schema, strict:true} on the Responses
   endpoint (never response_format there). Schema kept strict-clean: root
   object, additionalProperties:false everywhere, required==all props,
   nullable via type arrays, no pattern/format/oneOf/$schema key, groups
   as an array (strict forbids free-key maps; found via live 400). WellRef
   strings validated by parse_wellref in code. Docs: structured-output page
   + responses schemas reference.
3. Extraction is Spark's documented strength (headline structured-output use
   case; PDF arrives as text + page images; decoding constrained so output
   conforms): effort minimal, max 2048 tokens, separate prompt_cache_key
   (pip-bench-protocol), store:false, no tools.
4. Evidence text: pypdf in-memory extraction (import-only dep); txt uploads
   use raw text. Numbers ground via grounded(n, build_evidence(utterance=
   pdf_text)) plus unit-word presence (uL/min words). Group/reagent names via
   content-word presence reusing _SPAN_STOPWORDS (grounded-text cannot do
   presence; documented deviation, grounded itself untouched). Scanned PDFs
   yield no text, so everything flags needs_review (safe direction).
5. Stated wells must appear in the PDF text (find_well_mentions subset);
   otherwise the group falls back to auto layout + flag. This is the teeth
   behind "never invent positions".
6. Draft lives in record["draft"] = {plan, review[], errors[], meta{source,
   extracted_at, evidence_chars, layout{group: auto|stated}}}. Stateless
   recompute on every edit (no accept-state; Start never blocks on review).
7. Draft edits: edit_draft(path, value) agent tool (static in TOOLS, gated
   by draft_open in state + code rejection when no draft is open); PATCH
   /protocols/draft {path, value} for hand edits; same validator both
   channels (value parsing differs: JSON types vs tool strings). Values:
   digits for n/minutes, WellRef or 'row X' for wells. Malformed edits get
   honest couldn't; rule violations apply + flag errors (red, block Start).
   Replies use the draft_edited voice key. No draft grammar.
8. Start: errors block (200 + list), review warns (count in reply). Archive
   full record to server/data/runs/run-<UTC>.json, reset with draft plan at
   step_index 0, pop draft, snapshot push. current_step always 1.
9. Full-mode gate: env PROTOCOL_FULL_MODE (default true); false gives 403
   with no Meta call. PDF bytes never touch disk. Demo PDF only for now;
   Private mode (on-device extraction) later.
10. plan_times additionally honors step.minutes (additive; names still
    parsed first). Step wells derive from the group union when step.group is
    set; explicit step.wells only for group-less steps and must pass the
    PDF-presence rule.

## Steps

1. Schema + demo PDF. shared/schemas/plan.schema.json (groups{n,wells} /
    steps{name,group,wells,tip,minutes}); samples/make_protocol_demo.py
    (stdlib hand-rolled 1-page Methods PDF, no plate positions, 100uL +
    30min + 5 replicates) + committed samples/protocol_demo.pdf.
2. server/protocol.py. extract (Meta call), pypdf evidence, mini-validator
    driven by the schema file (type/required/properties/items/enum subset),
    grounded + name/unit checks, auto-layout (rows B.., cols from 2, n+2
    wells, last 2 into plan.backups), draft validate/edit/start/archive.
    pypdf to requirements.txt.
3. Endpoints + gate + tool. POST /protocols/import (multipart, mirrors
    /voice), PATCH /protocols/draft, POST /protocols/start, DELETE
    /protocols/draft; PROTOCOL_FULL_MODE gate; edit_draft tool (contract +
    _apply_call branch + draft block in model_state) and draft_edited voice
    key. Tools 18 -> 19.
4. App. screens.json += protocol_review; toolbar Import -> system document
    picker (PDF/txt) -> PipClient.importProtocol; review sheet (table,
    yellow review rows with reasons, red errors, editable fields, Start run
    disabled on errors, Discard); RecordSnapshot.draft optional;
    currentScreen += draftState (robotState mirror).
5. Tests + live eval. server/tests/test_protocol.py (mocked: strict-subset
    checker on the schema file, validator, layout incl. overflow/>7 groups/
    default-n grounding, invented-number review, PDF-absent wells auto,
    PATCH + edit_draft re-validation, invalid edit blocks Start, gate 403,
    start archive/reset on tmp store); eval/import_check.py live (demo PDF
    asserts groups/steps/minutes/layout, fuzzy step names, plus 3 free
    voice edits on fresh imports incl. an overlap case) appending
    eval/results/import.md.

## Validation Plan

- `.venv/bin/python -m unittest server.tests.test_protocol` (mocked suite).
- `.venv/bin/python eval/import_check.py` (live Spark extraction proof).
- Full suites + sim-SDK typecheck (known @Observable macro caveat).
- Manual iPad: pick PDF -> review flags -> hand edit -> "change drug A to
  row E" -> Start -> bench runs new plan; runs/ holds the old record.
- Assumption checks at implementation: no "ctrl" hardcode outside plan_demo
  (grep); edit_draft gated by draft_open + code rejection verified (P6a).

## Risks

- Group-name variance (control vs ctrl): normalize case/space/underscore
  only; live test accepts either; verify no hardcode.
- pypdf text quality on real PDFs degrades to more review flags (safe).
- Registry grows (protocol_review): SCREEN_IDS enum changes, prompt cache
  cold for one turn; tools stay 18.
- Second import overwrites the open draft (single-bench assumption).
- Writes during review still target the live record (non-goal; Start
  archives whatever the record holds).

## Open Questions

None.

## Sources

- https://dev.meta.ai/docs/file-handling
- https://dev.meta.ai/docs/structured-output
- https://dev.meta.ai/docs/protocols/responses
- https://dev.meta.ai/docs/api-reference/responses/schemas
- https://pypi.org/project/pypdf/

## Protocol format v2 plan (2026-10-28)

## Goal

Replace the fixed groups+steps protocol shape with a generic structure that
covers any bench protocol (volumes, temps, wash, centrifuge, reads): v2
schema (liquids/conditions/steps/layout), quote-grounded validation, 3-tab
review, ordered execution with kind-aware plate and timer cards, converted
plan_demo, and outcome-based card keys.

## Success Criteria

- User file protocol_demo_v2.pdf imports to 4 liquids, 3 conditions,
  5 steps with 100/10 uL, 30 min, 37 C grounded; null volumes flag
  needs_review with sentences; its italic "Tip:" sentence becomes caution
  and no step carries a tip.
- Wash/centrifuge fixture imports (repeat 2, new kinds); table fixture
  yields stated layout with exact wells.
- Quoteless values always flag; Start expands repeats, resolves targets
  and liquids, archives, and resets.
- Voice edits work on v2 paths; invalid moves block Start with red errors.
- Timer-expiry suggestion and voice next_step to the same step yield 1 card.
- All suites + 5 evals green on the converted plan_demo.

## Approach

Schema v2 carries liquids[]/conditions[]/steps[] (arrays only, strict-clean)
plus a code-derived layout section. One unified grounding rule replaces all
per-number checks: every extracted value must appear in its node's quote,
and every quote must appear in the document (normalized); else needs_review
with the quote shown. Import never writes tips; document cautions ride
steps[].caution verbatim. The live plan migrates to {conditions, steps,
backups} with kinds; old records convert on load. Plate reacts to pipette
steps only; timed kinds propose timer cards on step entry.

## Key Decisions

1. Schema v2: liquids[{id,name,concentration?,prep?,quote}],
   conditions[{id,label,treatments[liquid ids],replicates?,wells?,quote}],
   steps[{kind enum(10),text,quote,minutes?,temp_c?,caution?,
   pipette?{liquid id,volume?,unit enum?,target{type enum,value?},mix?,repeat?}}].
   No tip field: import never creates tips (schema-structural guarantee).
   caution holds a verbatim document caution sentence ("do not…",
   Tip:/Note:/Caution: markers) or null. conditions[].wells added (spec
   deviation: stated-layout source; without it stated positions have
   nowhere to live). Wells ride as WellRef strings each substring-checked
   against the quote, then expanded. Step index dropped (spec deviation:
   array position rules; model misnumbers). mix is free text; repeat
   1..12 int, else error.
2. Quote rule (replaces grounded-per-number for import; grounded() itself
   untouched): only kind and text are exempt. Every other value (volumes,
   units, minutes, temp, concentration, repeats, wells, liquid and
   condition names, caution) must sit in its node's quote, and every quote
   must sit in the document. Numbers int-normalized before compare (30 not
   30.0); repeat also accepts word forms (once/twice/thrice, "N times").
   Null value or failed check becomes needs_review carrying the quote;
   review items gain a quote field.
3. Quote normalization (norm_quote, applied to document, quotes, values):
   lowercase; µ/μ/u unify to u (so µl/μl/uL all read ul); ° stripped
   (37 °C reads 37 c); unicode dashes/quotes/spaces unify to ASCII
   -/'/single space. Unit tests lock each mapping plus repeat word forms.
4. Stated wells: each WellRef string in conditions[].wells or a wells-type
   target must sit in its quote (transitively in the doc; no separate
   doc-presence pass). Absent becomes auto layout + dropped_positions flag.
   Step target contradicting its condition union becomes mismatch review.
5. Layout derived per condition: stated when wells extracted, else auto
   (existing row/col/reserve logic, backups key kept). meta.layout maps
   condition to stated|auto|hand. Live plan drops layout (wells flattened
   into conditions) and liquids (names inlined into pipette.liquid).
6. Start: expand pipette.repeat into N sequential steps, resolve targets
   (wells parse, condition union, all_used full union, other empty) and
   liquid ids to names; unknown ids, bad WellRefs, and overlap block.
   Live step: {kind,text,minutes?,temp_c?,tip?,caution?,pipette?{liquid,
   volume,unit,wells[],target_label},wells[]}. Live condition:
   {label,treatments (names),replicates,wells}. Imports never set tip;
   the live tip field, save_tip, and plan_demo's 2 tips stay untouched.
7. Ordered execution, no auto-advance (non-goal): step_index walks all
   kinds; plate emphasis and Catch use step wells (empty for non-pipette);
   entering an incubate/centrifuge/wait step with minutes raises a timer
   suggestion card (new timer_suggest voice key, set_timer op). whats_next
   and where_am_i follow step text; both append the step caution as
   "⚠️ Protocol: <caution>" when present.
8. Old records convert on load (groups to conditions with label=key and
   empty treatments, name to text, kind inferred: minutes or min in name
   means incubate else pipette, tip kept, caution absent). runs/ archives
   stay untouched.
9. edit_draft paths v2: liquids.<id>|conditions.<id>|layout-equivalent via
   conditions.<id>.wells|steps[N] (0-based, summary shows [N] labels).
   Fields mirror the schema with steps[N].caution (no tip path: drafts
   have no tips); target values use wells:<W>|condition:<id>|all|other;
   wells accept WellRef or row X. PATCH shares validator.
10. Card keys by outcome: next_step subject becomes the 1-based target step
    (key (next_step, 5)); timer-expiry suggestions route through _card with
    the same key and merge wells/count/detail on replace, so the pair
    collapses to 1 card. Other keys already outcome-shaped; op names stay.
11. Review UI: 3 tabs (Liquids prep checklist local-only, Steps ordered
    timeline with pipette emphasis + quotes, Layout mini plate grid by
    condition). Needs_review yellow with quote; errors red; Start disabled
    on errors. Live StepCard shows text + kind/minutes/temp and caution as
    "⚠️ Protocol"; bars read conditions/replicates.
12. Hand off: the live pipette object ({liquid, volume, unit, wells})
    maps 1:1 onto an Opentrons transfer template. Scope excludes Learn
    entirely; robot code untouched.

## Steps

1. Schema + fixtures + seed. plan.schema.json v2 (caution, no tip);
   verify in user file samples/protocol_demo_v2.pdf (table, µ, italic
   Tip:, no positions); generator extended to protocol_wash.pdf
   (wash/centrifuge mid-flow, repeat 2) + protocol_table.pdf (positions
   table); plan_demo.json rewritten in live v2 (same wells/semantics,
   current_step 3, 2 tips kept, no cautions).
2. protocol.py v2. Schema load, norm_quote + quote checker (incl. repeat
   word forms), build/validate/layout on new shapes, repeat expansion,
   target/liquid resolution, v2 edit paths + parsing, v2 draft summary,
   Start-shape builder.
3. Live migration. store (conditions keys, step text, on-load conversion,
   model_state/prompt_state strings, entry timer suggestion), api
   (edit_draft contract, draft block), timer_suggest voice key, card-key
   outcome + suggest merge.
4. App. Live structs + StepCard/bars migration, review 3 tabs + layout grid
   + v2 patch paths, draft models with quote/issues.
5. Tests + evals. Rewrite protocol fixtures; update state-string probes and
   plan-shape tests; norm_quote unit tests (µ/μ/u, °C, dashes, quotes, case,
   repeat words); import_check covers 4 PDFs (v1 regression, v2 user file
   with Tip-to-caution and empty tips, wash, table) + voice edits + Start
   expansion; full suites + smoke/latency/tool_choice/instruction re-runs.

## Validation Plan

- `.venv/bin/python -m unittest server.test_grammar server.test_store
  server.tests.test_api server.tests.test_regressions server.tests.test_v2
  server.tests.test_conversational server.tests.test_protocol`
- `.venv/bin/python eval/import_check.py` (4 PDFs, caution rule, edits,
  Start expansion). Blocked until samples/protocol_demo_v2.pdf is provided.
- `.venv/bin/python eval/tool_choice.py` + smoke + latency + instruction
  re-runs on converted plan_demo.
- Sim-SDK typecheck (known macro caveat) + iPad review/execution pass.

## Risks

- Live shape rename touches model_state strings, probes, app structs, and
  evals; converted plan_demo must keep wells/steps/evals stable.
- Model step-kind classification errors surface as wrong plate/timer
  behavior; review timeline makes kinds visible pre-run.
- 0-based step paths vs spoken ordinals: mitigated by [N] labels in the
  model summary; user never types paths.
- Second import still overwrites the open draft.
- protocol_demo_v2.pdf received 2026-10-28: 4 liquids, 3 conditions x5,
  5 steps, cautions on steps 1+3, no tips, auto layout B/C/D rows 2-8.

## Implementation notes (2026-10-28)

- unit is a code-checked string (ul/ml), not a schema enum: strict-mode
  null enums risk live rejection; code validates per standing rule.
- IMPORT_MAX_OUTPUT_TOKENS 4096: quotes inflate extraction output.
- parse_draft_path tolerates bracket/dot mixes (steps.[0] == steps[0] ==
  steps.0); the edit_draft contract says steps.N with N from [N] labels.
- EXTRACT_PROMPT gained two clauses from live misses: seeding/design
  sentences belong to conditions, never steps; pipette is set for
  pipette, wash, and aspirate steps.
- Null flags cover execution-blocking nulls only: pipette volume,
  unit-with-volume, timed-kind minutes; null replicates default to 5
  (flagged when 5 is not in the quote, as v1). temp/caution/prep/
  concentration nulls never flag.
- draft_edited voice variants reconstructed (first verified verbatim
  from eval/results/import.md; others reworded).
- Auto layout vs the v2 file's guidance (one row per group, outer wells
  empty, two spares): matches for this file (B/C/D rows, cols 2-8).
  Edge deviations kept: row H and col 12 are usable at 6+ groups / n>=10.

## Open Questions

None.

## Sources

- https://dev.meta.ai/docs/file-handling
- https://dev.meta.ai/docs/structured-output
- https://dev.meta.ai/docs/protocols/responses
- https://dev.meta.ai/docs/api-reference/responses/schemas
- https://pypi.org/project/pypdf/
