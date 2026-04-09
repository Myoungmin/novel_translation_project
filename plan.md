# 프로젝트 계획 (현재)

## 목표

- 대용량 한국어 웹소설 txt 파일을 영어로 번역한다.
- 분할본, 정제본, 프롬프트, 요청, 번역 결과 등 중간 산출물을 모두 보존한다.
- chunk 단위 번역 후 최종 병합 txt 결과물을 생성한다.

## 현재 결정 사항

- 분할 및 필터링은 규칙 기반으로 처리한다.
- 번역은 glossary/style 주입을 포함한 LLM 기반으로 처리한다.
- provider는 pilot config에서 교체 가능하다.
- 파일명 규칙:
  - `*.json` = OpenAI
  - `*.claude.json` = Claude
  - `*.gemini.json` = Gemini

## 구현 완료 범위

- 전처리 파이프라인:
  - `scripts/preprocess_novel.py`
  - `configs/template.json`
- 파일럿 번역 파이프라인:
  - `scripts/translate_pilot.py`
  - `pilot_configs/template.json`
- glossary 자산:
  - `glossaries/template.json`

## 현재 작업 흐름

1. 작품별 config로 preprocess 실행

- 공유 템플릿을 복사해 로컬 파일을 만든 뒤 사용

2. dry-run 모드로 pilot 먼저 실행
3. prompts/requests 결과 검토
4. 제한된 section/chunk 범위로 execute 실행
5. 품질/비용 검토
6. 범위를 점진적으로 확대

## 다음 작업

1. 첫 전체 파일럿 실행용 provider 확정
2. 엄격한 제한을 둔 최소 비용 파일럿 실행
3. 결과 일관성과 glossary 누락 항목 점검
4. 작품별 glossary v1 확정 후 확장 실행

## 문서 운영 원칙

- 이 파일은 현재 계획과 운영 상태만 유지한다.
- 상세 실행 명령은 `README.md`에 유지한다.
- 의사결정 이력과 근거는 `docs/decisions.md`에 유지한다.
