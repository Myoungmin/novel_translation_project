from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any


REQUEST_NAME_PATTERN = re.compile(r"^section-(\d+)-chunk-(\d+)\.(txt|json)$")
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
        return None
    return run_dir.parent.parent.name


def find_workspace_root(run_dir: Path) -> Path | None:
    for candidate in [run_dir, *run_dir.parents]:
        if (candidate / "configs").is_dir():
            return candidate
    return None


def resolve_novel_title(run_dir: Path) -> str:
    work_id = infer_work_id_from_run_dir(run_dir)
    workspace_root = find_workspace_root(run_dir)
    if not work_id or not workspace_root:
        return "novel"

    config_path = workspace_root / "configs" / f"{work_id}.json"
    if not config_path.exists() or not config_path.is_file():
        return work_id

    try:
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
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


def write_html_output_for_range(
    out_dir: Path,
    run_dir: Path,
    merged_file_name: str,
    start_section: int,
    end_section: int,
) -> str | None:
    merged_txt_path = out_dir / merged_file_name
    if not merged_txt_path.exists() or not merged_txt_path.is_file():
        return None

    html_dir = out_dir / "html"
    ensure_directory(html_dir)

    novel_title = sanitize_filename(resolve_novel_title(run_dir))
    html_name = f"{novel_title}_{start_section:04d}-{end_section:04d}.html"
    html_path = html_dir / html_name

    txt_content = merged_txt_path.read_text(encoding="utf-8")
    html_content = build_html_document(txt_content)
    html_path.write_text(html_content, encoding="utf-8")
    return str(html_path.relative_to(out_dir))


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


def parse_request_name(name: str) -> tuple[int, int] | None:
    match = REQUEST_NAME_PATTERN.match(name)
    if not match:
        return None
    section_index = int(match.group(1))
    chunk_index = int(match.group(2))
    return section_index, chunk_index


def collect_chunks(directory: Path) -> dict[int, dict[int, Path]]:
    section_to_chunks: dict[int, dict[int, Path]] = {}
    if not directory.exists():
        return section_to_chunks

    for path in directory.iterdir():
        if not path.is_file():
            continue
        parsed = parse_request_name(path.name)
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

    source_map = collect_chunks(source_dir)
    translation_map = collect_chunks(translation_dir)

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
        file_name = f"section-{section_index:04d}.txt"
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
        html_output_file = write_html_output_for_range(
            out_dir=out_dir,
            run_dir=run_dir,
            merged_file_name=merged_output_file,
            start_section=start_section,
            end_section=end_section,
        )
        if html_output_file:
            html_output_files.append(html_output_file)

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
