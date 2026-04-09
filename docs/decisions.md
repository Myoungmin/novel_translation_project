# 의사결정 로그

## 2026-04-08

### 아키텍처

- 전처리는 규칙 기반으로 처리한다(LLM 미사용).
- 번역은 chunk 단위이며 glossary/style 프롬프트 주입을 사용한다.
- 재실행 및 복구를 위해 중간 산출물을 보존한다.

### Provider 전략

- 파일럿 레이어에 멀티 provider 지원을 추가했다.
- 지원 provider:
  - OpenAI chat completions
  - Anthropic messages (Claude)
  - Gemini generateContent
- 설정 파일명 규칙 통일:
  - `.json` = OpenAI
  - `.claude.json` = Claude
  - `.gemini.json` = Gemini

### 프롬프트/품질

- 가능한 경우 chunking 전에 장식용 heading 줄을 제거한다.
- heading 메타데이터는 프롬프트 컨텍스트로 별도 전달한다.
- glossary 기반 용어/스타일 힌트를 chunk별로 주입한다.

### 비용 통제

- 기본 흐름은 execute 전에 dry-run을 선행한다.
- 초기 과금을 제어하기 위해 pilot selection limit를 사용한다.
