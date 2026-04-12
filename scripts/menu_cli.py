from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from getpass import getpass
from pathlib import Path
from typing import Any

try:
    from scripts.translate_pilot import call_model_api_with_retry
    from scripts.translate_pilot import extract_translation_text
    from scripts.translate_pilot import get_provider
except ModuleNotFoundError:
    # Allow running as `py scripts\menu_cli.py` where `scripts` is not a package import root.
    from translate_pilot import call_model_api_with_retry
    from translate_pilot import extract_translation_text
    from translate_pilot import get_provider


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "configs"
PILOT_CONFIG_DIR = PROJECT_ROOT / "pilot_configs"
GLOSSARY_DIR = PROJECT_ROOT / "glossaries"


def pause() -> None:
    input("\n엔터를 누르면 메인 메뉴로 돌아갑니다...")


def clear_screen() -> None:
    os.system("cls")


def should_clear_screen() -> bool:
    # Default is off so previous execution logs stay visible in CMD.
    return os.getenv("MENU_CLEAR_SCREEN", "0") == "1"


def choose_from_list(title: str, options: list[Path], display_names: list[str] | None = None) -> Path | None:
    if not options:
        print("선택 가능한 항목이 없습니다.")
        return None

    print(title)
    for index, option in enumerate(options, start=1):
        display_text = display_names[index - 1] if display_names else option.name
        print(f"{index}. {display_text}")
    print("0. 취소")

    while True:
        raw = input("번호를 입력하세요: ").strip()
        if not raw.isdigit():
            print("숫자만 입력하세요.")
            continue

        selected = int(raw)
        if selected == 0:
            return None
        if 1 <= selected <= len(options):
            return options[selected - 1]
        print("범위를 벗어난 번호입니다.")


def confirm(prompt: str) -> bool:
    raw = input(f"{prompt} [y/N]: ").strip().lower()
    return raw in {"y", "yes"}


def run_command(command: list[str], env: dict[str, str] | None = None) -> int:
    print("\n실행 명령:")
    print(" ".join(command))
    print()
    completed = subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=False)
    return completed.returncode


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def sanitize_run_name(value: str) -> str:
    lowered = value.lower().strip()
    if not lowered:
        return "pilot-run"
    return re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-") or "pilot-run"


def detect_kept_kinds(preprocess_config: dict[str, Any]) -> list[str]:
    output_dir_value = preprocess_config.get("output_dir")
    if not output_dir_value:
        return []

    output_root = Path(output_dir_value)
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root

    manifest_path = output_root / "manifests" / "sections.json"
    if not manifest_path.exists():
        return []

    manifest = load_json(manifest_path)
    sections = manifest.get("sections", [])
    kinds: list[str] = []
    for section in sections:
        if not section.get("keep_in_clean"):
            continue
        kind = str(section.get("kind") or "").strip()
        if not kind:
            continue
        if kind == "front_matter":
            continue
        if kind not in kinds:
            kinds.append(kind)

    if kinds:
        return kinds

    # Fallback: keep front_matter if that is all we have.
    for section in sections:
        if not section.get("keep_in_clean"):
            continue
        kind = str(section.get("kind") or "").strip()
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def select_existing_kinds(preprocess_config: dict[str, Any], preferred_kinds: list[str] | None) -> list[str]:
    """Return kinds that exist in the preprocess manifest while preserving order.

    If preferred_kinds does not match anything, return detected kept kinds.
    """
    detected = detect_kept_kinds(preprocess_config)
    if not detected:
        return []

    if not preferred_kinds:
        return detected

    detected_set = set(detected)
    matched = [kind for kind in preferred_kinds if kind in detected_set]
    if matched:
        return matched
    return detected


def ensure_related_configs(preprocess_config_path: Path) -> None:
    preprocess_payload = load_json(preprocess_config_path)
    work_id = str(preprocess_payload.get("work_id") or "").strip()
    source_file = str(preprocess_payload.get("source_file") or "").strip()
    if not work_id:
        print("\n[자동 생성] work_id를 찾지 못해 glossary/pilot 설정 생성을 건너뜁니다.")
        return

    created_files: list[Path] = []
    kept_kinds = detect_kept_kinds(preprocess_payload)

    glossary_template_path = GLOSSARY_DIR / "template.json"
    glossary_output_path = GLOSSARY_DIR / f"{work_id}.json"
    if glossary_template_path.exists() and not glossary_output_path.exists():
        glossary_payload = load_json(glossary_template_path)
        glossary_payload["work_id"] = work_id
        if source_file:
            glossary_payload["source_file"] = source_file
        write_json(glossary_output_path, glossary_payload)
        created_files.append(glossary_output_path)

    for template_path in sorted(PILOT_CONFIG_DIR.glob("template*.json")):
        suffix = template_path.name[len("template") :]
        output_path = PILOT_CONFIG_DIR / f"{work_id}{suffix}"
        if output_path.exists():
            continue

        pilot_payload = load_json(template_path)
        pilot_payload["work_id"] = work_id
        pilot_payload["preprocess_config"] = f"configs/{preprocess_config_path.name}"
        pilot_payload["glossary_file"] = f"glossaries/{work_id}.json"
        pilot_payload["output_dir"] = f"artifacts/{work_id}/runs"

        model_name = str(pilot_payload.get("model", {}).get("model") or "")
        pilot_payload["run_name"] = sanitize_run_name(model_name)

        if kept_kinds:
            selection = pilot_payload.setdefault("selection", {})
            if isinstance(selection, dict):
                selection["kinds"] = kept_kinds

        write_json(output_path, pilot_payload)
        created_files.append(output_path)

    print("\n[자동 생성] glossary/pilot 설정 점검 완료")
    if not created_files:
        print("  - 새로 생성된 파일 없음 (이미 존재)")
        return
    for created in created_files:
        rel_path = created.relative_to(PROJECT_ROOT).as_posix()
        print(f"  - 생성됨: {rel_path}")


def list_config_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def get_pilot_config_display_names(config_paths: list[Path]) -> list[str]:
    display_names: list[str] = []
    for config_path in config_paths:
        try:
            payload = load_json(config_path)
            model = payload.get("model", {}) if isinstance(payload, dict) else {}
            provider = str(model.get("provider") or "unknown-provider")
            model_name = str(model.get("model") or "unknown-model")
            display_names.append(f"{config_path.name} ({provider} / {model_name})")
        except Exception:
            display_names.append(f"{config_path.name} (읽기 실패)")
    return display_names


def filter_ai_draft_pilot_configs(config_paths: list[Path]) -> tuple[list[Path], str | None]:
    """Prefer configs that are immediately executable for AI draft generation.

    A config is considered ready when model.api_key_env exists and is already set
    in the current environment. If none are ready, return the original list.
    """
    ready_configs: list[Path] = []
    for config_path in config_paths:
        try:
            payload = load_json(config_path)
        except Exception:
            continue
        model = payload.get("model", {}) if isinstance(payload, dict) else {}
        key_name = str(model.get("api_key_env") or "").strip()
        if key_name and os.getenv(key_name):
            ready_configs.append(config_path)

    if ready_configs:
        return ready_configs, "API 키가 설정된 설정만 표시합니다."

    return config_paths, "설정된 API 키가 없어 전체 설정을 표시합니다."


def list_run_directories() -> list[Path]:
    artifacts_root = PROJECT_ROOT / "artifacts"
    if not artifacts_root.exists():
        return []

    run_dirs: list[Path] = []
    for work_dir in artifacts_root.iterdir():
        if not work_dir.is_dir():
            continue
        runs_dir = work_dir / "runs"
        if not runs_dir.exists() or not runs_dir.is_dir():
            continue
        for run_dir in runs_dir.iterdir():
            if run_dir.is_dir():
                run_dirs.append(run_dir)

    return sorted(run_dirs)


def get_run_directory_display_names(run_dirs: list[Path]) -> list[str]:
    """Generate display names for run directories showing system and run folder names."""
    display_names: list[str] = []
    for run_dir in run_dirs:
        # Get the system name (parent of runs directory)
        system_name = run_dir.parent.parent.name
        run_name = run_dir.name
        display_names.append(f"{system_name} / {run_name}")
    return display_names


def prompt_optional_int(prompt: str) -> int | None:
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return None
        if raw.isdigit():
            return int(raw)
        print("숫자 또는 빈 입력만 가능합니다.")


def resolve_run_output_dir(payload: dict) -> Path | None:
    output_dir_value = payload.get("output_dir")
    run_name = payload.get("run_name")
    if not output_dir_value or not run_name:
        return None

    output_root = Path(output_dir_value)
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root
    return output_root / run_name


def resolve_project_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def strip_code_fence(text: str) -> str:
    fenced = text.strip()
    if fenced.startswith("```"):
        lines = fenced.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return fenced


def parse_json_from_model_text(text: str) -> dict[str, Any]:
    normalized = strip_code_fence(text)
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        pass

    start = normalized.find("{")
    end = normalized.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("모델 응답에서 JSON 객체를 찾지 못했습니다.")

    return json.loads(normalized[start : end + 1])


def run_generate_glossary_ai_draft() -> None:
    pilot_configs = [p for p in list_config_files(PILOT_CONFIG_DIR) if not p.name.startswith("template")]
    pilot_configs, filter_message = filter_ai_draft_pilot_configs(pilot_configs)
    if filter_message:
        print(f"\n{filter_message}")
    display_names = get_pilot_config_display_names(pilot_configs)
    selected = choose_from_list(
        "\n[용어집 AI 초안 생성] 파일럿 설정 파일을 선택하세요.",
        pilot_configs,
        display_names,
    )
    if selected is None:
        return

    config = load_json(selected)
    model_config = config.get("model", {})
    key_name = model_config.get("api_key_env")
    if key_name and not os.getenv(key_name):
        print(f"\n환경변수 {key_name} 가 설정되어 있지 않습니다.")
        key_value = getpass("API 키를 입력하세요(현재 세션에만 적용, 입력값 숨김): ").strip()
        if not key_value:
            print("API 키가 비어 있어 생성을 취소했습니다.")
            return
        os.environ[key_name] = key_value

    preprocess_config_value = str(config.get("preprocess_config") or "").strip()
    if not preprocess_config_value:
        print("\npilot 설정에 preprocess_config 값이 없어 생성을 진행할 수 없습니다.")
        return

    preprocess_config_path = resolve_project_path(preprocess_config_value)
    if not preprocess_config_path.exists() or not preprocess_config_path.is_file():
        print(f"\n전처리 설정 파일을 찾을 수 없습니다: {preprocess_config_path}")
        return

    try:
        preprocess_config = load_json(preprocess_config_path)
    except Exception as exc:
        print(f"\n전처리 설정 파일을 읽는 중 오류가 발생했습니다: {exc}")
        return
    output_dir = resolve_project_path(str(preprocess_config.get("output_dir", "")))
    manifest_path = output_dir / "manifests" / "sections.json"
    cleaned_dir = output_dir / "cleaned_split"
    if not manifest_path.exists() or not cleaned_dir.exists():
        print("\n전처리 산출물이 없습니다. 먼저 1번(전처리 실행)을 수행하세요.")
        return

    manifest = load_json(manifest_path)
    sections = [
        s
        for s in manifest.get("sections", [])
        if s.get("keep_in_clean") and str(s.get("kind") or "") != "front_matter"
    ]
    default_sections = sections[:]
    kinds = config.get("selection", {}).get("kinds")
    if isinstance(kinds, list) and kinds:
        allowed = {str(kind) for kind in kinds}
        sections = [s for s in sections if str(s.get("kind")) in allowed]

    if not sections and default_sections:
        print(
            "\n선택된 kinds로 샘플이 없어, keep_in_clean 섹션 전체(전면부 제외)로 자동 전환합니다."
        )
        sections = default_sections

    if not sections:
        print("\n샘플로 사용할 섹션이 없습니다. selection.kinds 또는 전처리 결과를 확인하세요.")
        return

    sample_count = prompt_optional_int("샘플 섹션 수를 입력하세요 (기본 2): ")
    max_chars_per_section = prompt_optional_int("섹션당 최대 글자 수를 입력하세요 (기본 3500): ")
    sample_count = sample_count if sample_count and sample_count > 0 else 2
    max_chars_per_section = max_chars_per_section if max_chars_per_section and max_chars_per_section > 0 else 3500

    sampled_sections = sections[:sample_count]
    sampled_chunks: list[str] = []
    used_labels: list[str] = []
    for section in sampled_sections:
        clean_file = section.get("clean_file")
        if not clean_file:
            continue
        clean_path = cleaned_dir / str(clean_file)
        if not clean_path.exists():
            continue
        text = clean_path.read_text(encoding="utf-8").strip()
        if len(text) > max_chars_per_section:
            text = text[:max_chars_per_section]
        label = str(section.get("display_label") or section.get("title") or section.get("stable_id"))
        used_labels.append(label)
        sampled_chunks.append(
            "\n".join(
                [
                    f"[section] stable_id={section.get('stable_id')} kind={section.get('kind')} title={label}",
                    text,
                ]
            )
        )

    if not sampled_chunks:
        print("\n샘플 텍스트를 읽지 못했습니다.")
        return

    work_id = str(preprocess_config.get("work_id") or "unknown-work")
    source_file = str(preprocess_config.get("source_file") or "")
    system_prompt = (
        "You are a bilingual Korean-to-English literary translation glossary assistant. "
        "Return strict JSON only with no markdown fences."
    )
    user_prompt = "\n".join(
        [
            "Create a draft glossary JSON from the sampled Korean novel text.",
            "Output JSON object with exactly these top-level keys:",
            "global_instructions, character_profiles, term_glossary, style_rules, do_not_translate, review_notes",
            "Constraints:",
            "- Keep arrays concise and practical for pilot translation.",
            "- Use English for target names/terms.",
            "- Include only terms supported by the sample text.",
            "- character_profiles item keys: source_name,target_name,role,speech_style_notes,personality_notes,status",
            "- term_glossary item keys: source_term,target_term,category,notes,status",
            "- style_rules item keys: rule,reason",
            "Sample text:",
            "\n\n".join(sampled_chunks),
        ]
    )

    print("\n[용어집 AI 초안 생성] 모델에 요청 중입니다...")
    try:
        provider = get_provider(model_config)
        api_response, _attempts = call_model_api_with_retry(model_config, system_prompt, user_prompt)
        model_text = extract_translation_text(api_response, provider)
        generated = parse_json_from_model_text(model_text)
    except Exception as exc:
        print(f"\nAI 초안 생성 중 오류가 발생했습니다: {exc}")
        print("설정을 확인한 뒤 다시 시도하세요.")
        return

    template_path = GLOSSARY_DIR / "template.json"
    if template_path.exists():
        draft_payload = load_json(template_path)
    else:
        draft_payload = {
            "work_id": work_id,
            "source_file": source_file,
            "language_pair": {"source": "ko", "target": "en"},
            "global_instructions": [],
            "character_profiles": [],
            "term_glossary": [],
            "style_rules": [],
            "do_not_translate": [],
            "review_notes": [],
        }

    draft_payload["work_id"] = work_id
    if source_file:
        draft_payload["source_file"] = source_file

    for key in [
        "global_instructions",
        "character_profiles",
        "term_glossary",
        "style_rules",
        "do_not_translate",
        "review_notes",
    ]:
        value = generated.get(key)
        if isinstance(value, list):
            draft_payload[key] = value

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    draft_payload.setdefault("review_notes", [])
    if isinstance(draft_payload["review_notes"], list):
        draft_payload["review_notes"].append(
            f"AI draft generated at {timestamp}; sampled sections: {', '.join(used_labels)}"
        )

    output_path = GLOSSARY_DIR / f"{work_id}.ai-draft.json"
    write_json(output_path, draft_payload)
    print("\n[용어집 AI 초안 생성 결과]")
    print(f"생성 파일: {output_path}")
    print("기존 정식 용어집은 덮어쓰지 않았습니다. 검토 후 반영하세요.")


def print_pilot_attempt_summary(selected: Path, payload: dict, execute: bool, resume: bool) -> None:
    provider = payload.get("model", {}).get("provider", "unknown")
    model_name = payload.get("model", {}).get("model", "unknown")
    selection = payload.get("selection", {})
    run_dir = resolve_run_output_dir(payload)

    print("\n[이번 실행에서 시도하는 내용]")
    print(f"설정 파일: {selected.name}")
    print(f"모드: {'execute' if execute else 'dry-run'}")
    if execute:
        print(f"resume: {resume}")
    print(f"provider/model: {provider} / {model_name}")
    print(
        "selection: "
        f"kinds={selection.get('kinds')}, offset={selection.get('offset')}, limit={selection.get('limit')}"
    )
    if run_dir:
        print(f"예상 run 폴더: {run_dir}")


def print_pilot_result_summary(payload: dict, return_code: int) -> None:
    run_dir = resolve_run_output_dir(payload)
    if not run_dir:
        print("\n[실행 결과 요약]")
        print("run_name/output_dir를 설정에서 찾지 못해 요약 파일 경로를 계산할 수 없습니다.")
        print(f"종료 코드: {return_code}")
        return

    summary_path = run_dir / "run_summary.json"
    print("\n[실행 결과 요약]")
    print(f"run 폴더: {run_dir}")
    print(f"요약 파일: {summary_path}")

    if not summary_path.exists():
        print("요약 파일이 아직 생성되지 않았습니다.")
        print(f"종료 코드: {return_code}")
        return

    with summary_path.open("r", encoding="utf-8") as handle:
        summary = json.load(handle)

    request_count = summary.get("request_count", 0)
    failed_count = summary.get("failed_request_count", 0)
    status_text = "성공" if return_code == 0 and failed_count == 0 else "실패"

    print(f"상태: {status_text} (exit={return_code})")
    print(f"요청 수: {request_count}, 실패 수: {failed_count}")

    failed_requests = summary.get("failed_requests", [])
    if failed_requests:
        print("실패 request_id:")
        for item in failed_requests[:5]:
            print(f"  - {item.get('request_id')}")


def show_preprocess_output_preview(config_path: Path) -> None:
    config = load_json(config_path)

    output_dir_value = config.get("output_dir")
    if not output_dir_value:
        print("\n전처리 출력 경로를 설정에서 찾지 못했습니다.")
        return

    output_root = Path(output_dir_value)
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root

    raw_dir = output_root / "raw_split"
    clean_dir = output_root / "cleaned_split"

    raw_files = sorted(path.name for path in raw_dir.glob("*.txt")) if raw_dir.exists() else []
    clean_files = sorted(path.name for path in clean_dir.glob("*.txt")) if clean_dir.exists() else []

    print("\n[전처리 결과 미리보기]")
    print(f"출력 경로: {output_root}")
    print(f"raw_split 파일 수: {len(raw_files)}")
    for name in raw_files[:5]:
        print(f"  - {name}")

    print(f"cleaned_split 파일 수: {len(clean_files)}")
    for name in clean_files[:5]:
        print(f"  - {name}")


def run_preprocess() -> None:
    config_files = list_config_files(CONFIG_DIR)
    selected = choose_from_list("\n[전처리] 설정 파일을 선택하세요.", config_files)
    if selected is None:
        return

    command = [
        "py",
        "scripts\\preprocess_novel.py",
        str(selected.relative_to(PROJECT_ROOT)).replace("/", "\\"),
        "--base-dir",
        str(PROJECT_ROOT),
    ]
    return_code = run_command(command)
    if return_code == 0:
        show_preprocess_output_preview(selected)
        ensure_related_configs(selected)
    else:
        print(f"\n전처리 실행에 실패했습니다. 종료 코드: {return_code}")


def run_pilot(execute: bool) -> None:
    config_files = [p for p in list_config_files(PILOT_CONFIG_DIR) if not p.name.startswith("template")]
    display_names = get_pilot_config_display_names(config_files)
    mode_text = "실행" if execute else "드라이런"
    selected = choose_from_list(
        f"\n[파일럿 {mode_text}] 설정 파일을 선택하세요.",
        config_files,
        display_names,
    )
    if selected is None:
        return

    payload = load_json(selected)
    command_config_path = selected

    preprocess_config_text = str(payload.get("preprocess_config") or "").strip()
    selection = payload.get("selection", {})
    preferred_kinds_raw = selection.get("kinds") if isinstance(selection, dict) else None
    preferred_kinds = (
        [str(kind) for kind in preferred_kinds_raw if str(kind).strip()]
        if isinstance(preferred_kinds_raw, list)
        else None
    )

    if preprocess_config_text:
        preprocess_config_path = resolve_project_path(preprocess_config_text)
        if preprocess_config_path.exists():
            preprocess_config = load_json(preprocess_config_path)
            effective_kinds = select_existing_kinds(preprocess_config, preferred_kinds)
            if effective_kinds and effective_kinds != (preferred_kinds or []):
                print(
                    "\n선택된 kinds가 전처리 결과와 맞지 않아 "
                    f"자동으로 kinds={effective_kinds} 로 조정합니다."
                )
                adjusted_payload = dict(payload)
                adjusted_selection = dict(adjusted_payload.get("selection", {}))
                adjusted_selection["kinds"] = effective_kinds
                adjusted_payload["selection"] = adjusted_selection

                temp_dir = PROJECT_ROOT / "artifacts" / "tmp-pilot-configs"
                temp_dir.mkdir(parents=True, exist_ok=True)
                temp_path = temp_dir / f"{selected.stem}.effective{selected.suffix}"
                write_json(temp_path, adjusted_payload)
                command_config_path = temp_path
                payload = adjusted_payload

    env = os.environ.copy()
    resume_requested = False

    if execute:
        key_name = payload.get("model", {}).get("api_key_env")
        if key_name and not env.get(key_name):
            print(f"\n환경변수 {key_name} 가 설정되어 있지 않습니다.")
            key_value = getpass("API 키를 입력하세요(현재 세션에만 적용, 입력값 숨김): ").strip()
            if not key_value:
                print("API 키가 비어 있어서 실행을 취소했습니다.")
                return
            env[key_name] = key_value

    command = [
        "py",
        "scripts\\translate_pilot.py",
        str(command_config_path.relative_to(PROJECT_ROOT)).replace("/", "\\"),
        "--base-dir",
        str(PROJECT_ROOT),
    ]
    if execute:
        command.append("--execute")
        command.append("--skip-final-translated")
        if confirm("기존 run_name 결과를 이어서 실행할까요?"):
            command.append("--resume")
            resume_requested = True

    print_pilot_attempt_summary(selected, payload, execute=execute, resume=resume_requested)

    return_code = run_command(command, env=env)
    print_pilot_result_summary(payload, return_code)
    if return_code != 0:
        print(f"\n파일럿 실행에 실패했습니다. 종료 코드: {return_code}")


def run_extract_completed_sections() -> None:
    run_dirs = list_run_directories()
    display_names = get_run_directory_display_names(run_dirs)
    selected_run_dir = choose_from_list("\n[완성 화 추출] run 폴더를 선택하세요.", run_dirs, display_names)
    if selected_run_dir is None:
        return

    start_section = prompt_optional_int(
        "합칠 시작 화를 입력하세요 (--start-section, 빈 입력 시 0번 블록부터 끝까지): "
    )

    command = [
        "py",
        "scripts\\extract_completed_sections.py",
        str(selected_run_dir.relative_to(PROJECT_ROOT)).replace("/", "\\"),
    ]

    if start_section is not None:
        command.extend(["--start-section", str(start_section)])

    return_code = run_command(command)
    if return_code != 0:
        print(f"\n완성 화 추출 실행에 실패했습니다. 종료 코드: {return_code}")


def main() -> None:
    while True:
        if should_clear_screen():
            clear_screen()
        print("=== 소설 번역 실행 메뉴 ===")
        print(f"프로젝트 경로: {PROJECT_ROOT}")
        print()
        print("1. 전처리 실행")
        print("2. 용어집 AI 초안 생성")
        print("3. 파일럿 드라이런 실행")
        print("4. 파일럿 실제 실행 (--execute)")
        print("5. 완성 화 추출 + 연속 병합")
        print("0. 종료")

        choice = input("\n메뉴 번호를 입력하세요: ").strip()

        if choice == "1":
            run_preprocess()
            pause()
        elif choice == "2":
            run_generate_glossary_ai_draft()
            pause()
        elif choice == "3":
            run_pilot(execute=False)
            pause()
        elif choice == "4":
            run_pilot(execute=True)
            pause()
        elif choice == "5":
            run_extract_completed_sections()
            pause()
        elif choice == "0":
            print("종료합니다.")
            break
        else:
            print("유효하지 않은 선택입니다.")
            pause()


if __name__ == "__main__":
    main()