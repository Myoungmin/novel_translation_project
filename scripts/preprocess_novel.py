from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PatternSpec:
    name: str
    kind: str
    regex: re.Pattern[str]
    title_source: str = "line"
    title_regex: re.Pattern[str] | None = None
    section_code_regex: re.Pattern[str] | None = None
    title_literal: str | None = None


@dataclass
class Section:
    index: int
    kind: str
    pattern_name: str | None
    section_code: str | None
    stable_id: str
    start_line: int
    end_line: int
    title: str
    display_label: str
    header_line: str
    keep_in_clean: bool
    content: str
    file_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split a novel text file into reusable intermediate artifacts."
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to a JSON config file describing the source text and section rules.",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Base directory used to resolve relative paths from the config file.",
    )
    return parser.parse_args()


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def compile_patterns(raw_patterns: list[dict[str, Any]]) -> list[PatternSpec]:
    compiled: list[PatternSpec] = []
    for raw_pattern in raw_patterns:
        title_regex = raw_pattern.get("title_regex")
        section_code_regex = raw_pattern.get("section_code_regex")
        compiled.append(
            PatternSpec(
                name=raw_pattern["name"],
                kind=raw_pattern["kind"],
                regex=re.compile(raw_pattern["regex"]),
                title_source=raw_pattern.get("title_source", "line"),
                title_regex=re.compile(title_regex) if title_regex else None,
                section_code_regex=(
                    re.compile(section_code_regex)
                    if section_code_regex
                    else (re.compile(title_regex) if title_regex else None)
                ),
                title_literal=raw_pattern.get("title_literal"),
            )
        )
    return compiled


def normalize_section_code(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    if normalized.isdigit():
        return normalized.zfill(5)

    return normalized


def build_stable_id(section_index: int, section_code: str | None) -> str:
    if section_code:
        return f"s-{section_code}"
    return f"i-{section_index:04d}"


def build_display_label(
    section_index: int,
    title: str,
    section_code: str | None,
) -> str:
    if section_code and title:
        return f"{section_code} {title}".strip()
    if title:
        return title
    return f"section-{section_index:04d}"


def read_source_text(source_path: Path, encodings: list[str]) -> tuple[str, str]:
    last_error: UnicodeDecodeError | None = None
    for encoding in encodings:
        try:
            return source_path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError as error:
            last_error = error

    encodings_text = ", ".join(encodings)
    raise RuntimeError(
        f"Unable to decode {source_path} with the configured encodings: {encodings_text}"
    ) from last_error


def match_pattern(line: str, patterns: list[PatternSpec]) -> PatternSpec | None:
    for pattern in patterns:
        if pattern.regex.search(line):
            return pattern
    return None


def extract_title(
    pattern: PatternSpec | None,
    header_line: str,
    section_lines: list[str],
    fallback_title: str,
) -> str:
    if pattern is None:
        return fallback_title

    if pattern.title_source == "literal" and pattern.title_literal:
        return pattern.title_literal

    if pattern.title_source == "match" and pattern.title_regex is not None:
        match = pattern.title_regex.search(header_line)
        if match:
            title = match.groupdict().get("title") or match.group(0)
            title = cleanup_title(title)
            if title:
                return title

    if pattern.title_source == "next_nonempty":
        for line in section_lines[1:]:
            candidate = cleanup_title(line)
            if candidate:
                return candidate

    candidate = cleanup_title(header_line)
    return candidate or fallback_title


def extract_section_code(pattern: PatternSpec | None, header_line: str) -> str | None:
    if pattern is None or pattern.section_code_regex is None:
        return None

    match = pattern.section_code_regex.search(header_line)
    if not match:
        return None

    section_code = match.groupdict().get("number")
    return normalize_section_code(section_code) if section_code else None


def cleanup_title(value: str) -> str:
    title = value.strip()
    title = re.sub(r"=+$", "", title).strip()
    title = re.sub(r"^<--\s*", "", title)
    title = re.sub(r"\s*-->$", "", title)
    return title.strip()


def finalize_section(
    sections: list[Section],
    current: dict[str, Any],
    clean_exclude_types: set[str],
) -> None:
    if not current["lines"]:
        return

    content = "\n".join(current["lines"]).rstrip() + "\n"
    fallback_title = f"section-{current['index']:04d}"
    title = extract_title(
        current["pattern"],
        current["header_line"],
        current["lines"],
        fallback_title,
    )
    section_code = extract_section_code(current["pattern"], current["header_line"])
    stable_id = build_stable_id(current["index"], section_code)
    display_label = build_display_label(current["index"], title, section_code)
    normalized_code = re.sub(r"[^0-9a-zA-Z]+", "-", section_code or "").strip("-").lower()
    if normalized_code:
        file_name = f"{current['index']:04d}_{current['kind']}_{normalized_code}.txt"
    else:
        file_name = f"{current['index']:04d}_{current['kind']}.txt"
    sections.append(
        Section(
            index=current["index"],
            kind=current["kind"],
            pattern_name=current["pattern"].name if current["pattern"] else None,
            section_code=section_code,
            stable_id=stable_id,
            start_line=current["start_line"],
            end_line=current["end_line"],
            title=title,
            display_label=display_label,
            header_line=current["header_line"],
            keep_in_clean=current["kind"] not in clean_exclude_types,
            content=content,
            file_name=file_name,
        )
    )


def split_sections(
    lines: list[str],
    patterns: list[PatternSpec],
    clean_exclude_types: set[str],
) -> list[Section]:
    sections: list[Section] = []
    current: dict[str, Any] | None = None

    for line_number, line in enumerate(lines, start=1):
        pattern = match_pattern(line, patterns)
        if pattern is not None:
            if current is not None:
                current["end_line"] = line_number - 1
                finalize_section(sections, current, clean_exclude_types)

            current = {
                "index": len(sections) + 1,
                "kind": pattern.kind,
                "pattern": pattern,
                "start_line": line_number,
                "end_line": line_number,
                "header_line": line,
                "lines": [line],
            }
            continue

        if current is None:
            current = {
                "index": len(sections) + 1,
                "kind": "front_matter",
                "pattern": None,
                "start_line": line_number,
                "end_line": line_number,
                "header_line": line,
                "lines": [line],
            }
            continue

        current["lines"].append(line)
        current["end_line"] = line_number

    if current is not None:
        finalize_section(sections, current, clean_exclude_types)

    return sections


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def ensure_safe_output_path(path: Path, safety_root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = safety_root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise RuntimeError(
            f"Refusing to modify output path outside safety root: {resolved_path}"
        ) from error


def reset_output_directory(path: Path, safety_root: Path) -> None:
    ensure_safe_output_path(path, safety_root)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def write_outputs(
    output_root: Path,
    sections: list[Section],
    source_name: str,
    encoding: str,
    safety_root: Path,
) -> None:
    raw_dir = output_root / "raw_split"
    clean_dir = output_root / "cleaned_split"
    manifests_dir = output_root / "manifests"

    reset_output_directory(raw_dir, safety_root)
    reset_output_directory(clean_dir, safety_root)
    reset_output_directory(manifests_dir, safety_root)

    merged_clean_parts: list[str] = []
    manifest_sections: list[dict[str, Any]] = []
    ordinal_in_clean = 0

    for section in sections:
        raw_path = raw_dir / section.file_name
        raw_path.write_text(section.content, encoding="utf-8")

        clean_file: str | None = None
        section_ordinal_in_clean: int | None = None
        if section.keep_in_clean:
            clean_path = clean_dir / section.file_name
            clean_path.write_text(section.content, encoding="utf-8")
            merged_clean_parts.append(section.content)
            clean_file = clean_path.name
            ordinal_in_clean += 1
            section_ordinal_in_clean = ordinal_in_clean

        manifest_sections.append(
            {
                "index": section.index,
                "kind": section.kind,
                "pattern_name": section.pattern_name,
                "section_code": section.section_code,
                "stable_id": section.stable_id,
                "display_label": section.display_label,
                "ordinal_in_clean": section_ordinal_in_clean,
                "title": section.title,
                "header_line": section.header_line,
                "start_line": section.start_line,
                "end_line": section.end_line,
                "keep_in_clean": section.keep_in_clean,
                "raw_file": raw_path.name,
                "clean_file": clean_file,
            }
        )

    (output_root / "merged_clean.txt").write_text("".join(merged_clean_parts), encoding="utf-8")

    summary = {
        "source_name": source_name,
        "source_encoding": encoding,
        "section_count": len(sections),
        "kept_section_count": sum(1 for section in sections if section.keep_in_clean),
        "excluded_section_count": sum(1 for section in sections if not section.keep_in_clean),
        "kind_counts": count_by_kind(sections),
        "sections": manifest_sections,
    }
    (manifests_dir / "sections.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def count_by_kind(sections: list[Section]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for section in sections:
        counts[section.kind] = counts.get(section.kind, 0) + 1
    return counts


def resolve_path(base_dir: Path, path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return base_dir / path


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_config(config_path)

    base_dir = args.base_dir.resolve() if args.base_dir else config_path.parent.resolve()
    source_path = resolve_path(base_dir, config["source_file"])
    output_root = resolve_path(base_dir, config["output_dir"])
    encodings = config.get("encodings", ["utf-8", "utf-8-sig", "cp949"])
    patterns = compile_patterns(config["patterns"])
    clean_exclude_types = set(config.get("clean_exclude_types", []))

    text, source_encoding = read_source_text(source_path, encodings)
    lines = text.splitlines()
    sections = split_sections(lines, patterns, clean_exclude_types)
    write_outputs(output_root, sections, source_path.name, source_encoding, base_dir)

    summary = {
        "source": source_path.name,
        "encoding": source_encoding,
        "output_dir": str(output_root),
        "section_count": len(sections),
        "kept_sections": sum(1 for section in sections if section.keep_in_clean),
        "excluded_sections": sum(1 for section in sections if not section.keep_in_clean),
        "kind_counts": count_by_kind(sections),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()