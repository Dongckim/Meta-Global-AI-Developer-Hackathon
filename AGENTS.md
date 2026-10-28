기존 동작을 "버그"라고 판단해서 바꾸려면 먼저 물어볼 것.
Model picks from code-computed candidates; code validates.
New tests live in the repo (server/tests/, ios Tests/); /tmp is for throwaway debug scripts only.
세션 시작 시 docs/design/pip_agent_v3.md를 먼저 읽고 그 기준으로 작업한다.

## 커밋 메시지 규칙
- 커밋은 사용자가 요청할 때만 한다. 푸시는 하지 않는다.
- 작업이 끝나면 답변 마지막에 "제안 커밋" 블록을 준다.
  변경을 성격별로 나눠야 하면 커밋 여러 개로 제안하고, 각 커밋에 넣을 파일 목록을 같이 적는다.
- 형식: <type>(<scope>): <요약>
  - type
    - feat: 새 기능
    - fix: 버그 수정
    - refactor: 동작 변화 없는 구조 변경
    - perf: 속도 개선
    - test: 테스트 추가 또는 수정
    - docs: 문서
    - chore: 설정, 빌드, 의존성, 기타
    - style: 포맷만 변경 (동작 변화 없음)
  - scope: server, ios, shared, eval, docs, protocol, voice, glasses 중 해당하는 것. 여러 영역이면 생략 가능
  - 요약: 영어, 현재형 명령문, 소문자 시작, 마침표 없음, 72자 이내
  - 필요하면 빈 줄 뒤에 본문: 무엇을, 왜 (어떻게는 코드가 보여줌).
    API 계약(snapshot api_version 등)이 바뀌면 본문에 "BREAKING:" 줄 추가
- 예시
  feat(glasses): add bluetooth hfp audio route toggle
  fix(server): keep run version monotonic on start and end
  refactor(server): route all inputs through single action handler
- 커밋 제안에 절대 포함하지 말 것: .env, Secrets.xcconfig, server/data/, *.wav
  (변경 목록에 있으면 경고만 표시)
