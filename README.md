# 소설 번역 프로젝트

## 목적

이 프로젝트는 대용량 한국어 소설을 영어로 번역하기 위한 자동화 파이프라인이다.

- 규칙 기반 전처리
- provider 교체 가능한 파일럿 번역
- glossary 기반 일관성 관리
- 중간 산출물 보존

## 프로젝트 구조

- `scripts/preprocess_novel.py` - 분할/필터 전처리 파이프라인
- `scripts/translate_pilot.py` - 파일럿 프롬프트/요청 생성 및 선택적 API 실행
- `sources/` - 원본 txt 파일 보관 폴더
- `configs/` - 전처리 규칙 템플릿 및 로컬 작품별 설정
- `pilot_configs/` - 파일럿 실행 템플릿 및 로컬 실행 설정
- `glossaries/` - glossary 템플릿 및 로컬 작품별 용어집
- `artifacts/` - 생성 결과물

## Provider 설정 파일명 규칙

- `work.json` = OpenAI
- `work.claude.json` = Claude
- `work.gemini.json` = Gemini

## 기본 작업 흐름

0. 템플릿 파일을 복사해 로컬 작품별 설정을 만든다.
1. 원본 파일 전처리 실행
2. dry-run 모드로 파일럿 실행
3. 프롬프트와 요청 페이로드 점검
4. 작은 범위에서 API 키로 execute 실행
5. 품질/비용 확인 후 범위 확대

공유 저장소에는 템플릿만 포함하고, 작품별 설정 파일은 각자 로컬에서 만든다.

예시:

```cmd
copy configs\template.json configs\my-work.json
copy pilot_configs\template.json pilot_configs\my-work.json
copy glossaries\template.json glossaries\my-work.json
```

## 명령어 (CMD + py 기준)

### 빠른 실행 메뉴 (권장)

긴 명령어 대신 번호 선택 메뉴로 실행하려면 아래 중 하나를 사용한다.

```cmd
cd /d d:\novel_translation_project
py scripts\menu_cli.py
```

또는

```cmd
cd /d d:\novel_translation_project
run-menu.cmd
```

메뉴에서 전처리를 실행하면 완료 직후 raw_split/cleaned_split 파일 수와 파일명 샘플 5개를 자동으로 출력한다.
메뉴 2번에서는 용어집 AI 초안(`glossaries/<work_id>.ai-draft.json`)을 생성한다.
기존 정식 용어집 파일은 덮어쓰지 않는다.
메뉴 3번은 파일럿 드라이런 실행이다.
메뉴에서 파일럿 실제 실행을 선택하면 API 키 입력값은 화면에 표시되지 않는다.
파일럿 실제 실행 메뉴에서는 기존 run_name 결과를 이어서 실행할지(`--resume`) 선택할 수 있다.
메뉴 4번 실행은 `final_translated.txt` 생성을 건너뛴다 (`--skip-final-translated`).
메뉴 5번에서는 완성 화 추출 스크립트를 실행하며 `--start-section`만 선택할 수 있다.
빈 입력 시 0번 블록부터 끝까지 재생성한다.

### 1) 전처리

```cmd
cd /d d:\novel_translation_project
py scripts\preprocess_novel.py configs\my-work.json --base-dir d:\novel_translation_project
```

### 2) 파일럿 Dry-Run

```cmd
cd /d d:\novel_translation_project
py scripts\translate_pilot.py pilot_configs\my-work.json --base-dir d:\novel_translation_project
```

파일럿 run의 요청/청크 파일명은 내부 section index 대신 안정 식별자 `stable_id`를 우선 사용한다.

- section_code가 있으면: `section-s-00005-chunk-001.txt`
- section_code가 없으면: `section-i-0837-chunk-001.txt`

`run_summary.json`에는 `legacy_request_id`도 같이 기록하므로 기존 index 기반 run과 병행 검토할 수 있다.

### 3) 실행 (OpenAI)

```cmd
cd /d d:\novel_translation_project
set OPENAI_API_KEY=YOUR_KEY
py scripts\translate_pilot.py pilot_configs\my-work.json --base-dir d:\novel_translation_project --execute
```

### 4) 실행 (Claude)

```cmd
cd /d d:\novel_translation_project
set ANTHROPIC_API_KEY=YOUR_KEY
py scripts\translate_pilot.py pilot_configs\my-work.claude.json --base-dir d:\novel_translation_project --execute
```

### 5) 실행 (Gemini)

```cmd
cd /d d:\novel_translation_project
set GEMINI_API_KEY=YOUR_KEY
py scripts\translate_pilot.py pilot_configs\my-work.gemini.json --base-dir d:\novel_translation_project --execute
```

### 6) 완성 화 추출 + 연속 구간 병합

`source_chunks` 대비 번역 청크가 모두 존재하는 섹션만 완성 화로 판정한다.
실행할 때마다 `postprocess_completed` 산출물을 깨끗하게 재생성한다.
개별 연속 블록 파일(`contiguous_completed_merged_XXXX-YYYY.txt`)은 선택 범위 전체를 생성하고,
최종 누적 파일은 화수가 실제로 이어지는 블록만 누적한다.

```cmd
cd /d d:\novel_translation_project
py scripts\extract_completed_sections.py artifacts\my-work\runs\my-run
```

시작 화 지정 예시 (`start-section`이 포함된 블록은 건너뛰고 다음 블록부터 끝까지 생성):

```cmd
py scripts\extract_completed_sections.py artifacts\my-work\runs\my-run --start-section 121
```

## 출력 위치

- 전처리 결과: `artifacts/<work-id>/`
- 파일럿 결과: `artifacts/<work-id>/runs/<run-name>/`

파일럿 execute 결과 추가 산출물:

- `translations/` - chunk별 번역 결과
- `merged_sections/` - section별 병합 결과
- `final_translated.txt` - 전체 병합 결과
- `qa_report.json` - 길이/용어/이름 기준 QA 리포트
- `run_summary.json` - 요청별 상태, 실패 목록, usage, 비용 추정 집계

전처리 manifest(`artifacts/<work-id>/manifests/sections.json`) 주요 필드:

- `index` - 내부 분할 순번
- `section_code` - 실제 화수 코드가 있으면 기록
- `stable_id` - 파이프라인 전반에서 쓰는 안정 식별자 (`s-00005`, `i-0837` 형식)
- `display_label` - 사람이 읽는 표시 이름
- `ordinal_in_clean` - `keep_in_clean` 기준 연속 순번

전처리 분할 파일명 규칙:

- 기본: `NNNN_kind.txt`
- section_code가 있는 경우: `NNNN_kind_sectioncode.txt`
- 예시: `0002_chapter.txt`, `0211_chapter_00111.txt`

## 운영 메모

- 항상 dry-run을 먼저 실행한다.
- provider 비용/품질 검증 전까지 파일럿 범위를 작게 유지한다.
- 같은 `run_name`으로 파일럿을 다시 실행하면 기본적으로 해당 run 폴더를 먼저 초기화하고, `--resume` 사용 시에는 기존 성공 청크를 유지한 채 이어서 실행한다.

## 고급 설정 메모

- `py scripts\translate_pilot.py ... --execute --resume` 로 실패한 run을 이어서 실행할 수 있다.
- `model.retry` 설정으로 재시도 정책을 조정할 수 있다.
- `execution.max_workers` 값을 2 이상으로 두면 API 호출을 병렬 실행한다.
- `selection.offset` 으로 선택된 섹션 목록의 시작 위치를 이동할 수 있다 (`limit: 1`과 함께 사용하면 한 번에 다른 섹션 1개씩 테스트 가능).
- `selection.stable_ids` 로 특정 안정 식별자만 직접 선택할 수 있다.
- `model.pricing.input_cost_per_million_tokens`, `model.pricing.output_cost_per_million_tokens` 를 넣으면 비용 추정이 계산된다.
