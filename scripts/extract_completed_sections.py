from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any


REQUEST_NAME_PATTERN = re.compile(r"^section-(.+)-chunk-(\d+)\.(txt|json)$")
MULTI_NEWLINE_PATTERN = re.compile(r"(\r?\n){2,}")
INVALID_FILENAME_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*]')


def to_sorted_unique(items: list[int]) -> list[int]:
    return sorted(set(items))


def normalize_blank_lines(text: str) -> str:
    if not text:
        return ""
    # Force every consecutive blank-line group to a single newline.
    return MULTI_NEWLINE_PATTERN.sub("\n", text)


def sanitize_filename(value: str) -> str:
    sanitized = INVALID_FILENAME_CHARS_PATTERN.sub("_", value).strip().rstrip(".")
    return sanitized or "untitled"


def infer_work_id_from_run_dir(run_dir: Path) -> str | None:
    # Expected layout: artifacts/<work-id>/runs/<run-name>
    if run_dir.parent.name != "runs":
        # Also support ad-hoc sample runs like artifacts/pilot-runs/<work-id>/<run-name>.
        if run_dir.parent.parent.name != "pilot-runs":
            return None
        return run_dir.parent.name
    return run_dir.parent.parent.name


def find_workspace_root(run_dir: Path) -> Path | None:
    for candidate in [run_dir, *run_dir.parents]:
        if (candidate / "configs").is_dir():
            return candidate
    return None


def resolve_work_config_path(run_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    work_id = infer_work_id_from_run_dir(run_dir)
    workspace_root = find_workspace_root(run_dir)
    if not work_id or not workspace_root:
        return None, None

    config_dir = workspace_root / "configs"

    # Prefer direct filename match, but accept common slug variants.
    name_candidates = [
        f"{work_id}.json",
        f"{work_id.replace('-', '_')}.json",
        f"{work_id.replace('_', '-')}.json",
    ]
    seen: set[str] = set()
    for name in name_candidates:
        if name in seen:
            continue
        seen.add(name)
        config_path = config_dir / name
        if not config_path.exists() or not config_path.is_file():
            continue
        try:
            return config_path, json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return config_path, None

    # Fallback: locate config by internal work_id field.
    for config_path in sorted(config_dir.glob("*.json")):
        if config_path.name.startswith("template"):
            continue
        try:
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(config_data.get("work_id") or "").strip() == work_id:
            return config_path, config_data

    return None, None


def resolve_preprocess_output_dir(run_dir: Path) -> Path | None:
    config_path, config_data = resolve_work_config_path(run_dir)
    if not config_path or not config_data:
        return None

    output_dir = str(config_data.get("output_dir", "")).strip()
    if not output_dir:
        return None

    output_path = Path(output_dir)
    if output_path.is_absolute():
        return output_path

    workspace_root = find_workspace_root(run_dir)
    if not workspace_root:
        return None
    return (workspace_root / output_path).resolve()


def resolve_html_split_size(run_dir: Path) -> int | None:
    """
    config에서 HTML 분할 크기를 읽습니다.
    
    html_output.split_size 설정:
    - 숫자: 그 숫자 그대로 글자 수 (예: 30000)
    - 프리셋: "light"(15000) / "medium"(30000) / "heavy"(50000)
    - 미설정이나 0: 분할 없음
    
    Returns: 분할 크기 (글자), 또는 None (분할 안 함)
    """
    _, config_data = resolve_work_config_path(run_dir)
    if not config_data:
        return None
    
    html_config = config_data.get("html_output", {})
    if not isinstance(html_config, dict):
        return None
    
    # enabled가 False면 분할 안 함
    if html_config.get("enabled") is False:
        return None
    
    split_value = html_config.get("split_size")
    if split_value is None:
        return None
    
    # 프리셋 처리
    presets: dict[str, int] = {
        "light": 15000,
        "medium": 30000,
        "heavy": 50000,
    }
    
    if isinstance(split_value, str) and split_value.lower() in presets:
        return presets[split_value.lower()]
    
    # 숫자 처리
    try:
        split_size = int(split_value)
        if split_size > 0:
            return split_size
    except (TypeError, ValueError):
        pass
    
    return None


def load_section_metadata_map(run_dir: Path) -> dict[int, dict[str, Any]]:
    preprocess_output_dir = resolve_preprocess_output_dir(run_dir)
    if not preprocess_output_dir:
        return {}

    manifest_path = preprocess_output_dir / "manifests" / "sections.json"
    if not manifest_path.exists() or not manifest_path.is_file():
        return {}

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    metadata_by_index: dict[int, dict[str, Any]] = {}
    for section in manifest_data.get("sections", []):
        try:
            section_index = int(section.get("index"))
        except (TypeError, ValueError):
            continue
        metadata_by_index[section_index] = section

    return metadata_by_index


def build_section_index_lookup(section_metadata_map: dict[int, dict[str, Any]]) -> dict[str, int]:
    lookup: dict[str, int] = {}
    for section_index, metadata in section_metadata_map.items():
        lookup[str(section_index)] = section_index

        stable_id = str(metadata.get("stable_id") or "").strip()
        if not stable_id:
            section_code = str(metadata.get("section_code") or "").strip()
            stable_id = f"s-{section_code}" if section_code else f"i-{section_index:04d}"
        lookup[stable_id] = section_index

        section_code = str(metadata.get("section_code") or "").strip()
        if section_code:
            lookup[f"s-{section_code}"] = section_index

    return lookup


def build_completed_section_file_name(section_index: int, section_metadata: dict[str, Any] | None) -> str:
    if not section_metadata:
        return f"section-{section_index:04d}.txt"

    stable_id = str(section_metadata.get("stable_id") or "").strip() or f"i-{section_index:04d}"
    name_parts = [f"section-{stable_id}"]
    title = str(section_metadata.get("title") or "").strip()

    if title:
        name_parts.append(sanitize_filename(title))

    return "_".join(name_parts) + ".txt"


def resolve_novel_title(run_dir: Path) -> str:
    work_id = infer_work_id_from_run_dir(run_dir)
    if not work_id:
        return "novel"

    _, config_data = resolve_work_config_path(run_dir)
    if not config_data:
        return work_id

    source_file = str(config_data.get("source_file", "")).strip()
    if not source_file:
        return work_id

    return Path(source_file).stem.strip() or work_id


def build_html_document(body_text: str) -> str:
    escaped_text = html.escape(body_text)
    return (
        "<html>\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "</head>\n"
        "<body style=\"margin:0; background:#111; color:#eee; font-family:system-ui;\">\n"
        "<div style=\"max-width:800px; margin:40px auto; padding:20px;\">\n"
        "<pre style=\"font-size:20px; line-height:1.8; white-space:pre-wrap; word-break:break-word;\">\n"
        f"{escaped_text}\n"
        "</pre>\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


def split_text_by_size(text: str, max_size: int) -> list[str]:
    """텍스트를 최대 크기 기준으로 분할합니다. 최대한 문단 경계에서 분할하려고 시도합니다."""
    if max_size <= 0:
        return [text]
    
    if len(text) <= max_size:
        return [text]
    
    parts: list[str] = []
    current_pos = 0
    
    while current_pos < len(text):
        # 현재 위치에서 max_size만큼 떨어진 위치까지의 텍스트
        chunk_end = current_pos + max_size
        
        if chunk_end >= len(text):
            # 남은 텍스트가 max_size 이하면 그냥 추가
            parts.append(text[current_pos:])
            break
        
        # max_size 지점 근처에서 줄바꿈을 찾아서 분할점 조정
        split_point = chunk_end
        
        # 뒤쪽에서 줄바꿈 찾기 (최대 5000글자 범위)
        search_start = max(current_pos, chunk_end - 5000)
        last_newline = text.rfind('\n', search_start, chunk_end)
        
        if last_newline > current_pos:
            split_point = last_newline + 1
        else:
            # 줄바꿈이 없으면 앞쪽에서 찾기
            next_newline = text.find('\n', chunk_end)
            if next_newline > 0 and next_newline - chunk_end < 1000:
                split_point = next_newline + 1
        
        parts.append(text[current_pos:split_point])
        current_pos = split_point
    
    return [p for p in parts if p.strip()]  # 빈 부분 제거


def write_html_output_for_range(
    out_dir: Path,
    run_dir: Path,
    merged_file_name: str,
    start_section: int,
    end_section: int,
    split_size_chars: int | None = None,
) -> list[str]:
    """
    병합된 txt 파일을 HTML로 변환하여 저장합니다.
    split_size_chars가 설정되어 있으면 여러 개의 HTML 파일로 분할합니다.
    
    Returns: 생성된 HTML 파일 목록 (out_dir 기준 상대 경로)
    """
    merged_txt_path = out_dir / merged_file_name
    if not merged_txt_path.exists() or not merged_txt_path.is_file():
        return []

    html_dir = out_dir / "html"
    ensure_directory(html_dir)

    novel_title = sanitize_filename(resolve_novel_title(run_dir))
    txt_content = merged_txt_path.read_text(encoding="utf-8")
    
    # split_size_chars이 설정되지 않았거나 0 이하면 분할 안 함
    if not split_size_chars or split_size_chars <= 0:
        html_name = f"{novel_title}_{start_section:04d}-{end_section:04d}.html"
        html_path = html_dir / html_name
        html_content = build_html_document(txt_content)
        html_path.write_text(html_content, encoding="utf-8")
        return [str(html_path.relative_to(out_dir))]
    
    # 분할 로직: split_size_chars 기준으로 분할
    text_parts = split_text_by_size(txt_content, split_size_chars)
    
    if len(text_parts) == 1:
        # 분할이 불필요한 경우 (텍스트가 max_size 이하)
        html_name = f"{novel_title}_{start_section:04d}-{end_section:04d}.html"
        html_path = html_dir / html_name
        html_content = build_html_document(text_parts[0])
        html_path.write_text(html_content, encoding="utf-8")
        return [str(html_path.relative_to(out_dir))]
    
    # 여러 파일로 분할하는 경우에는 전체 섹션 범위는 유지하고 part 번호만 붙입니다.
    generated_files: list[str] = []
    part_count = len(text_parts)
    for part_index, part_text in enumerate(text_parts, start=1):
        html_name = (
            f"{novel_title}_{start_section:04d}-{end_section:04d}"
            f"_part-{part_index:02d}-of-{part_count:02d}.html"
        )
        html_path = html_dir / html_name
        html_content = build_html_document(part_text)
        html_path.write_text(html_content, encoding="utf-8")
        generated_files.append(str(html_path.relative_to(out_dir)))
    
    return generated_files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract fully completed sections from a pilot run and sequentially merge contiguous blocks. "
            "Each run appends the next contiguous block to the merged output file."
        )
    )
    parser.add_argument(
        "run_dir",
        type=Path,
        help="Run directory, e.g. artifacts/<work>/runs/<run-name>",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory for extracted files (default: <run_dir>/postprocess_completed)",
    )
    parser.add_argument(
        "--list-blocks",
        action="store_true",
        help="List all contiguous completed blocks and exit",
    )
    parser.add_argument(
        "--start-section",
        type=int,
        default=None,
        help="Force-select the contiguous block that includes or starts after this section index",
    )
    return parser.parse_args()


def parse_request_name(
    name: str,
    section_index_lookup: dict[str, int],
    unresolved_tokens: set[str] | None = None,
) -> tuple[int, int] | None:
    match = REQUEST_NAME_PATTERN.match(name)
    if not match:
        return None

    section_token = match.group(1)
    chunk_index = int(match.group(2))

    try:
        section_index = int(section_token)
    except ValueError:
        section_index = section_index_lookup.get(section_token, -1)

        # Fallback parser so stable-id file names still work even when
        # section metadata/manifest lookup is unavailable.
        if section_index < 0:
            stable_match = re.match(r"^i-(\d+)$", section_token)
            if stable_match:
                section_index = int(stable_match.group(1))

    if section_index < 0:
        if unresolved_tokens is not None:
            unresolved_tokens.add(section_token)
        return None

    return section_index, chunk_index


def collect_chunks(
    directory: Path,
    section_index_lookup: dict[str, int],
    unresolved_tokens: set[str] | None = None,
) -> dict[int, dict[int, Path]]:
    section_to_chunks: dict[int, dict[int, Path]] = {}
    if not directory.exists():
        return section_to_chunks

    for path in directory.iterdir():
        if not path.is_file():
            continue
        parsed = parse_request_name(path.name, section_index_lookup, unresolved_tokens)
        if not parsed:
            continue
        section_index, chunk_index = parsed
        section_to_chunks.setdefault(section_index, {})[chunk_index] = path

    return section_to_chunks


def find_contiguous_blocks_in_order(
    all_sections: list[int],
    completed_sections: set[int],
) -> list[list[int]]:
    blocks: list[list[int]] = []
    current_block: list[int] = []

    for section in all_sections:
        if section in completed_sections:
            current_block.append(section)
        else:
            if current_block:
                blocks.append(current_block)
                current_block = []

    if current_block:
        blocks.append(current_block)

    return blocks


def find_block_index_from_start(blocks: list[list[int]], start_section: int) -> int:
    for i, block in enumerate(blocks):
        if not block:
            continue
        # Start from the first block strictly after start_section.
        # If start_section is inside a block, skip that block and move to next.
        if block[0] <= start_section <= block[-1]:
            return i + 1 if (i + 1) < len(blocks) else -1
        if block[0] > start_section:
            return i
    return -1


def build_section_text(translation_chunks: dict[int, Path]) -> str:
    ordered = sorted(translation_chunks.items(), key=lambda item: item[0])
    chunk_texts: list[str] = []

    for _, chunk_path in ordered:
        text = chunk_path.read_text(encoding="utf-8").strip()
        if text:
            chunk_texts.append(text)

    if not chunk_texts:
        return ""

    merged = "\n".join(chunk_texts).strip()
    normalized = normalize_blank_lines(merged)
    return normalized + "\n"


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def clean_output_artifacts(
    out_dir: Path,
) -> None:
    completed_sections_dir = out_dir / "completed_sections"
    if completed_sections_dir.exists() and completed_sections_dir.is_dir():
        shutil.rmtree(completed_sections_dir)

    for artifact in out_dir.glob("contiguous_completed_merged*.txt"):
        if artifact.is_file():
            artifact.unlink()

    report_path = out_dir / "completed_sections_report.json"
    if report_path.exists() and report_path.is_file():
        report_path.unlink()

    html_dir = out_dir / "html"
    if html_dir.exists() and html_dir.is_dir():
        shutil.rmtree(html_dir)


def main() -> None:
    args = parse_args()

    run_dir = args.run_dir.resolve()
    source_dir = run_dir / "source_chunks"
    translation_dir = run_dir / "translations"

    if not source_dir.exists():
        raise SystemExit(f"source_chunks directory not found: {source_dir}")
    if not translation_dir.exists():
        raise SystemExit(f"translations directory not found: {translation_dir}")

    out_dir = (args.out_dir.resolve() if args.out_dir else (run_dir / "postprocess_completed").resolve())
    ensure_directory(out_dir)
    clean_output_artifacts(out_dir)

    section_metadata_map = load_section_metadata_map(run_dir)
    section_index_lookup = build_section_index_lookup(section_metadata_map)
    unresolved_source_tokens: set[str] = set()
    unresolved_translation_tokens: set[str] = set()
    source_map = collect_chunks(source_dir, section_index_lookup, unresolved_source_tokens)
    translation_map = collect_chunks(translation_dir, section_index_lookup, unresolved_translation_tokens)

    unresolved_tokens = sorted(unresolved_source_tokens | unresolved_translation_tokens)
    if unresolved_tokens:
        print(
            "Warning: some chunk file section tokens could not be resolved to section indices. "
            "Check sections manifest and work config mapping."
        )
        for token in unresolved_tokens[:20]:
            print(f"  - unresolved section token: {token}")
        if len(unresolved_tokens) > 20:
            print(f"  ... and {len(unresolved_tokens) - 20} more")

    section_stats: list[dict[str, Any]] = []
    completed_sections: list[int] = []

    all_sections = to_sorted_unique(list(source_map.keys()))
    for section_index in all_sections:
        source_chunks = source_map.get(section_index, {})
        translated_chunks = translation_map.get(section_index, {})

        source_chunk_ids = sorted(source_chunks.keys())
        translated_chunk_ids = sorted(translated_chunks.keys())
        missing_chunk_ids = [chunk_id for chunk_id in source_chunk_ids if chunk_id not in translated_chunks]

        is_completed = len(source_chunk_ids) > 0 and len(missing_chunk_ids) == 0
        if is_completed:
            completed_sections.append(section_index)

        section_stats.append(
            {
                "section_index": section_index,
                "section_code": section_metadata_map.get(section_index, {}).get("section_code"),
                "stable_id": section_metadata_map.get(section_index, {}).get("stable_id"),
                "display_label": section_metadata_map.get(section_index, {}).get("display_label"),
                "section_title": section_metadata_map.get(section_index, {}).get("title"),
                "source_chunk_count": len(source_chunk_ids),
                "translated_chunk_count": len(translated_chunk_ids),
                "missing_chunk_ids": missing_chunk_ids,
                "is_completed": is_completed,
            }
        )

    completed_sections = sorted(completed_sections)
    blocks = find_contiguous_blocks_in_order(all_sections, set(completed_sections))

    if args.list_blocks:
        print("Available contiguous blocks:")
        for i, block in enumerate(blocks):
            block_str = f"{block[0]:04d}-{block[-1]:04d}" if len(block) > 1 else f"{block[0]:04d}"
            print(f"  [{i}] {block_str} (count={len(block)})")
        return

    start_block_index = -1
    if args.start_section is not None:
        start_block_index = find_block_index_from_start(blocks, args.start_section)
        if start_block_index == -1:
            print(f"No contiguous block found at/after start-section={args.start_section}")
            return
        print(f"[Manual selection] start-section={args.start_section} -> start block [{start_block_index}]")
    else:
        # Default behavior: regenerate all contiguous blocks from the beginning.
        # This keeps menu option 4 deterministic on every run.
        if not blocks:
            print("No contiguous blocks found!")
            return
        start_block_index = 0

    blocks_to_process = list(range(start_block_index, len(blocks)))
    if not blocks_to_process:
        print("No blocks selected for processing.")
        return

    completed_sections_dir = out_dir / "completed_sections"
    ensure_directory(completed_sections_dir)
    completed_section_files: list[str] = []

    for section_index in completed_sections:
        section_text = build_section_text(translation_map[section_index])
        if not section_text:
            continue
        file_name = build_completed_section_file_name(
            section_index,
            section_metadata_map.get(section_index),
        )
        (completed_sections_dir / file_name).write_text(section_text, encoding="utf-8")
        completed_section_files.append(file_name)

    merged_output_files: list[str] = []
    final_merged_file = None
    final_merged_range = None
    current_final_range = None
    current_final_file = None

    for block_index in blocks_to_process:
        selected_block = blocks[block_index]
        if not selected_block:
            continue

        merged_parts: list[str] = []
        for section_index in selected_block:
            section_text = build_section_text(translation_map[section_index]).rstrip()
            if section_text:
                merged_parts.append(section_text)

        if not merged_parts:
            continue

        block_start = selected_block[0]
        block_end = selected_block[-1]

        merged_output_file = f"contiguous_completed_merged_{block_start:04d}-{block_end:04d}.txt"
        merged_output_files.append(merged_output_file)
        block_text = "\n".join(merged_parts).strip() + "\n"
        (out_dir / merged_output_file).write_text(block_text, encoding="utf-8")
        print(f"[Individual block] Created: {merged_output_file}")

        should_append_to_final = True
        final_start = block_start
        final_end = block_end

        if current_final_range:
            prev_final_end = current_final_range["end"]
            if block_start != prev_final_end + 1:
                should_append_to_final = False
                print(
                    f"[Cumulative] Block is NOT continuous (prev ended at {prev_final_end}, current starts at {block_start}). Skipping final file."
                )
            else:
                final_start = current_final_range["start"]
                final_end = block_end

        if should_append_to_final:
            old_final_file = current_final_file
            has_previous_final = current_final_range is not None

            final_merged_file = f"contiguous_completed_merged_{final_start:04d}-{final_end:04d}.txt"
            final_merged_range = {"start": final_start, "end": final_end}
            final_path = out_dir / final_merged_file

            if not has_previous_final and final_merged_file == merged_output_file:
                print(f"[Cumulative] Using individual block file as initial cumulative file: {final_merged_file}")
            elif final_path.exists():
                existing_text = final_path.read_text(encoding="utf-8")
                combined_text = existing_text.rstrip() + "\n" + block_text
                print(f"[Cumulative] Appended block [{block_index}] {merged_output_file}")
                final_path.write_text(combined_text, encoding="utf-8")
            else:
                # The cumulative range was extended (new file name). Read the previous
                # cumulative file's content and prepend it so no content is lost.
                if old_final_file and (out_dir / old_final_file).exists():
                    old_content = (out_dir / old_final_file).read_text(encoding="utf-8")
                    combined_text = old_content.rstrip() + "\n" + block_text
                else:
                    combined_text = block_text
                print(f"[Cumulative] Created new cumulative file with block [{block_index}] {merged_output_file}")
                final_path.write_text(combined_text, encoding="utf-8")

            if old_final_file and old_final_file != final_merged_file:
                old_path = out_dir / old_final_file
                if old_path.exists():
                    old_path.unlink()
                    print(f"[Cumulative] Renamed: {old_final_file} -> {final_merged_file}")

            current_final_range = final_merged_range
            current_final_file = final_merged_file

    # config에서 HTML 분할 설정 읽기
    html_split_size = resolve_html_split_size(run_dir)
    if html_split_size:
        print(f"[HTML Output] Splitting enabled: {html_split_size} characters per file")
    else:
        print(f"[HTML Output] Splitting disabled: creating single files per merged range")

    html_output_files: list[str] = []
    for merged_output_file in merged_output_files:
        range_match = re.match(
            r"^contiguous_completed_merged_(\d{4})-(\d{4})\.txt$",
            merged_output_file,
        )
        if not range_match:
            continue

        start_section = int(range_match.group(1))
        end_section = int(range_match.group(2))
        generated_html_files = write_html_output_for_range(
            out_dir=out_dir,
            run_dir=run_dir,
            merged_file_name=merged_output_file,
            start_section=start_section,
            end_section=end_section,
            split_size_chars=html_split_size,
        )
        html_output_files.extend(generated_html_files)

    report = {
        "run_dir": str(run_dir),
        "source_section_count": len(all_sections),
        "completed_section_count": len(completed_sections),
        "completed_sections": completed_sections,
        "incomplete_section_count": len(all_sections) - len(completed_sections),
        "contiguous_blocks_info": [
            {
                "block_index": i,
                "start_section": block[0],
                "end_section": block[-1],
                "count": len(block),
            }
            for i, block in enumerate(blocks)
        ],
        "selected_block_index": blocks_to_process[-1],
        "selected_block_sections": blocks[blocks_to_process[-1]],
        "selected_block_count": len(blocks[blocks_to_process[-1]]),
        "selected_block": {
            "start": blocks[blocks_to_process[-1]][0],
            "end": blocks[blocks_to_process[-1]][-1],
        },
        "start_section_override": args.start_section,
        "processed_block_indices": blocks_to_process,
        "merged_output_files": merged_output_files,
        "merged_output_file": merged_output_files[-1] if merged_output_files else None,
        "completed_section_files_written": completed_section_files,
        "final_merged_file": current_final_file,
        "final_merged_range": current_final_range,
        "html_output_files": html_output_files,
        "final_html_file": html_output_files[-1] if html_output_files else None,
        "sections": section_stats,
    }

    report_path = out_dir / "completed_sections_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Run directory: {run_dir}")
    print(f"Total sections: {len(all_sections)}")
    print(f"Completed sections: {len(completed_sections)}")
    print(f"Incomplete sections: {len(all_sections) - len(completed_sections)}")
    print(f"Contiguous blocks found: {len(blocks)}")
    for i, block in enumerate(blocks):
        block_str = f"{block[0]:04d}-{block[-1]:04d}" if len(block) > 1 else f"{block[0]:04d}"
        selected_mark = " [SELECTED FOR THIS RUN]" if i in blocks_to_process else ""
        print(f"  [{i}] {block_str} (count={len(block)}){selected_mark}")
    if current_final_file:
        print(f"Cumulative merged file: {out_dir / current_final_file}")
    else:
        print("No cumulative merged file created (blocks not merged or not continuous)")
    if html_output_files:
        print(f"HTML files created: {len(html_output_files)}")
        for html_output_file in html_output_files:
            print(f"  - {out_dir / html_output_file}")
    print(f"Completed sections folder: {completed_sections_dir}")
    print(f"Report file: {report_path}")


if __name__ == "__main__":
    main()
