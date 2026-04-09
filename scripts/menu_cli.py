from __future__ import annotations

import json
import os
import subprocess
from getpass import getpass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "configs"
PILOT_CONFIG_DIR = PROJECT_ROOT / "pilot_configs"


def pause() -> None:
    input("\n엔터를 누르면 메인 메뉴로 돌아갑니다...")


def clear_screen() -> None:
    os.system("cls")


def should_clear_screen() -> bool:
    # Default is off so previous execution logs stay visible in CMD.
    return os.getenv("MENU_CLEAR_SCREEN", "0") == "1"


def choose_from_list(title: str, options: list[Path]) -> Path | None:
    if not options:
        print("선택 가능한 항목이 없습니다.")
        return None

    print(title)
    for index, option in enumerate(options, start=1):
        print(f"{index}. {option.name}")
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


def list_config_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def resolve_run_output_dir(payload: dict) -> Path | None:
    output_dir_value = payload.get("output_dir")
    run_name = payload.get("run_name")
    if not output_dir_value or not run_name:
        return None

    output_root = Path(output_dir_value)
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root
    return output_root / run_name


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
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

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
    else:
        print(f"\n전처리 실행에 실패했습니다. 종료 코드: {return_code}")


def run_pilot(execute: bool) -> None:
    config_files = list_config_files(PILOT_CONFIG_DIR)
    mode_text = "실행" if execute else "드라이런"
    selected = choose_from_list(f"\n[파일럿 {mode_text}] 설정 파일을 선택하세요.", config_files)
    if selected is None:
        return

    with selected.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

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
        str(selected.relative_to(PROJECT_ROOT)).replace("/", "\\"),
        "--base-dir",
        str(PROJECT_ROOT),
    ]
    if execute:
        command.append("--execute")
        if confirm("기존 run_name 결과를 이어서 실행할까요?"):
            command.append("--resume")
            resume_requested = True

    print_pilot_attempt_summary(selected, payload, execute=execute, resume=resume_requested)

    return_code = run_command(command, env=env)
    print_pilot_result_summary(payload, return_code)
    if return_code != 0:
        print(f"\n파일럿 실행에 실패했습니다. 종료 코드: {return_code}")


def main() -> None:
    while True:
        if should_clear_screen():
            clear_screen()
        print("=== 소설 번역 실행 메뉴 ===")
        print(f"프로젝트 경로: {PROJECT_ROOT}")
        print()
        print("1. 전처리 실행")
        print("2. 파일럿 드라이런 실행")
        print("3. 파일럿 실제 실행 (--execute)")
        print("0. 종료")

        choice = input("\n메뉴 번호를 입력하세요: ").strip()

        if choice == "1":
            run_preprocess()
            pause()
        elif choice == "2":
            run_pilot(execute=False)
            pause()
        elif choice == "3":
            run_pilot(execute=True)
            pause()
        elif choice == "0":
            print("종료합니다.")
            break
        else:
            print("유효하지 않은 선택입니다.")
            pause()


if __name__ == "__main__":
    main()