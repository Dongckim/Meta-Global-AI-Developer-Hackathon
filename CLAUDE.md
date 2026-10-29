@AGENTS.md

## Claude Code 추가 규칙
- 작업 시작 전에 docs/design/pip_agent_v3.md와 shared/schemas/를 읽고 구조를 따른다.
- 원칙 단위로 고친다. 땜질(특정 문장/단어 예외 처리) 금지. 땜질이 불가피하면 이유를 말하고 묻는다.
- 시뮬레이터는 어떤 형태로도 쓰지 않는다: 시뮬레이터 destination, `xcrun simctl`, Simulator 앱, `xcodebuild test` 실행 금지.
  iOS 확인은 `-destination 'generic/platform=iOS'`로 `build-for-testing`(앱+테스트 컴파일만)과 Release 빌드까지. 테스트 실행 금지(iPad 포함).
  동작·화면 확인은 전부 사용자가 iPad에서 한다. 필요하면 로그를 심고,
  매번 "내가 iPad에서 할 확인 순서"를 짧은 번호 목록으로 준다.
- 같은 문제에 2번 실패하면 멈추고 보고. 15분 넘으면 중간 보고.
- 커밋은 사용자가 요청할 때만. 푸시 금지.
- 커밋 메시지에 Co-Authored-By 등 trailer/서명 줄을 넣지 않는다.
- 되돌리기(reset, revert, 히스토리 수정)가 필요하면 실행하지 말고 멈추고 묻는다.
- 답변은 한국어, 짧게.
