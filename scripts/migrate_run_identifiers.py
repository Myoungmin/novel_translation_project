from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


LEGACY_CHUNK_NAME_PATTERN = re.compile(r"^section-(\d+)-chunk-(\d+)(\.(txt|json))$")
LEGACY_MERGED_SECTION_PATTERN = re.compile(r"^section-(\d+)\.txt$")
INVALID_FILENAME_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*]')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate a translation run from legacy index-based section identifiers "
            "to stable_id-based identifiers."
        )
    )
    parser.add_argument(
        "run_dir",
        type=Path,
        help="Run directory, e.g. artifacts/<work>/runs/<run-name>",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Workspace root (auto-detected when omitted).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. By default, only report planned changes.",
    )
    return parser.parse_args()


def sanitize_filename(value: str) -> str:
    sanitized = INVALID_FILENAME_CHARS_PATTERN.sub("_", value).strip().rstrip(".")
    return sanitized or "untitled"


def infer_work_id_from_run_dir(run_dir: Path) -> str | None:
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent.name
    if run_dir.parent.parent.name == "pilot-runs":
        return run_dir.parent.name
    return None


def find_workspace_root(run_dir: Path) -> Path | None:
    for candidate in [run_dir, *run_dir.parents]:
        if (candidate / "configs").is_dir():
            return candidate
    return None


def load_section_metadata_map(run_dir: Path, workspace_root: Path) -> dict[int, dict[str, Any]]:
    work_id = infer_work_id_from_run_dir(run_dir)
    if not work_id:
        raise RuntimeError(f"Unable to infer work id from run dir: {run_dir}")

    config_path = workspace_root / "configs" / f"{work_id}.json"
    if not config_path.exists():
        raise RuntimeError(f"Work config not found: {config_path}")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    output_dir = str(config.get("output_dir", "")).strip()
    if not output_dir:
        raise RuntimeError(f"output_dir missing in config: {config_path}")

    preprocess_output = Path(output_dir)
    if not preprocess_output.is_absolute():
        preprocess_output = (workspace_root / preprocess_output).resolve()

    manifest_path = preprocess_output / "manifests" / "sections.json"
    if not manifest_path.exists():
        raise RuntimeError(f"sections manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata: dict[int, dict[str, Any]] = {}
    for section in manifest.get("sections", []):
        try:
            section_index = int(section.get("index"))
        except (TypeError, ValueError):
            continue
        metadata[section_index] = section
    return metadata


def stable_id_for_index(index: int, metadata_by_index: dict[int, dict[str, Any]]) -> str:
    metadata = metadata_by_index.get(index, {})
    stable_id = str(metadata.get("stable_id") or "").strip()
    if stable_id:
        return stable_id

    section_code = str(metadata.get("section_code") or "").strip()
    if section_code:
        return f"s-{section_code}"

    return f"i-{index:04d}"


def normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def display_label_for_index(index: int, metadata_by_index: dict[int, dict[str, Any]]) -> str | None:
    metadata = metadata_by_index.get(index, {})
    display_label = normalize_optional_string(metadata.get("display_label"))
    if display_label:
        return display_label

    title = normalize_optional_string(metadata.get("title"))
    section_code = normalize_optional_string(metadata.get("section_code"))
    if section_code and title:
        return f"{section_code} {title}"
    if title:
        return title
    return None


def build_section_metadata(index: int, metadata_by_index: dict[int, dict[str, Any]]) -> dict[str, str | None]:
    metadata = metadata_by_index.get(index, {})
    return {
        "section_code": normalize_optional_string(metadata.get("section_code")),
        "stable_id": stable_id_for_index(index, metadata_by_index),
        "display_label": display_label_for_index(index, metadata_by_index),
        "title": normalize_optional_string(metadata.get("title")),
        "clean_file": normalize_optional_string(metadata.get("clean_file")),
    }


def build_request_id(stable_id: str, chunk_index: int) -> str:
    return f"section-{stable_id}-chunk-{chunk_index:03d}"


def build_merged_file_name(index: int, metadata_by_index: dict[int, dict[str, Any]]) -> str:
    metadata = metadata_by_index.get(index, {})
    stable_id = stable_id_for_index(index, metadata_by_index)
    title = str(metadata.get("title") or "").strip()
    if title:
        return f"section-{stable_id}_{sanitize_filename(title)}.txt"
    return f"section-{stable_id}.txt"


def collect_legacy_request_ids(run_dir: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for folder_name in ["source_chunks", "translations", "responses", "prompts", "requests"]:
        folder = run_dir / folder_name
        if not folder.exists() or not folder.is_dir():
            continue

        for file in folder.iterdir():
            if not file.is_file():
                continue
            match = LEGACY_CHUNK_NAME_PATTERN.match(file.name)
            if not match:
                continue
            section_token = match.group(1)
            chunk_token = match.group(2)
            section_index = int(section_token)
            chunk_index = int(chunk_token)

            # Keep both raw and canonical legacy forms so runs that were created
            # without zero-padding can still be migrated reliably.
            raw_old_request_id = f"section-{section_token}-chunk-{chunk_token}"
            canonical_old_request_id = f"section-{section_index:04d}-chunk-{chunk_index:03d}"
            mapping.setdefault(raw_old_request_id, "")
            mapping.setdefault(canonical_old_request_id, "")

    return mapping


def rename_file(path: Path, target_name: str, apply: bool) -> tuple[bool, str | None]:
    target = path.with_name(target_name)
    if path.name == target_name:
        return False, None
    if target.exists():
        return False, f"Target already exists: {target}"
    if apply:
        path.rename(target)
    return True, None


def migrate_request_files(
    run_dir: Path,
    id_map: dict[str, str],
    apply: bool,
) -> tuple[int, list[str]]:
    changed = 0
    errors: list[str] = []

    for folder_name in ["source_chunks", "translations", "responses", "prompts", "requests"]:
        folder = run_dir / folder_name
        if not folder.exists() or not folder.is_dir():
            continue

        for file in sorted(folder.iterdir()):
            if not file.is_file():
                continue

            stem = file.stem
            if stem not in id_map:
                continue

            new_stem = id_map[stem]
            did_change, err = rename_file(file, f"{new_stem}{file.suffix}", apply)
            if err:
                errors.append(err)
            if did_change:
                changed += 1

    return changed, errors


def migrate_merged_sections(
    run_dir: Path,
    metadata_by_index: dict[int, dict[str, Any]],
    apply: bool,
) -> tuple[int, list[str], dict[str, str]]:
    merged_dir = run_dir / "merged_sections"
    if not merged_dir.exists() or not merged_dir.is_dir():
        return 0, [], {}

    changed = 0
    errors: list[str] = []
    mapping: dict[str, str] = {}

    for file in sorted(merged_dir.iterdir()):
        if not file.is_file():
            continue

        match = LEGACY_MERGED_SECTION_PATTERN.match(file.name)
        if not match:
            continue

        section_index = int(match.group(1))
        new_name = build_merged_file_name(section_index, metadata_by_index)
        mapping[file.name] = new_name
        did_change, err = rename_file(file, new_name, apply)
        if err:
            errors.append(err)
        if did_change:
            changed += 1

    return changed, errors, mapping


def migrate_json_payload(
    path: Path,
    id_map: dict[str, str],
    metadata_by_index: dict[int, dict[str, Any]],
    merged_file_map: dict[str, str],
    apply: bool,
) -> bool:
    if not path.exists() or not path.is_file():
        return False

    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False

    requests = data.get("requests")
    if isinstance(requests, list):
        for item in requests:
            if not isinstance(item, dict):
                continue
            old_request_id = str(item.get("request_id") or "")
            if old_request_id in id_map:
                item["legacy_request_id"] = old_request_id
                item["request_id"] = id_map[old_request_id]
                changed = True

            translation_file = str(item.get("translation_file") or "")
            if translation_file.endswith(".txt"):
                stem = translation_file[:-4]
                if stem in id_map:
                    item["translation_file"] = f"{id_map[stem]}.txt"
                    changed = True

            section_index = item.get("section_index")
            try:
                section_index = int(section_index)
            except (TypeError, ValueError):
                section_index = None

            if section_index is not None:
                section_metadata = build_section_metadata(section_index, metadata_by_index)
                expected_fields = {
                    "section_code": section_metadata["section_code"],
                    "stable_id": section_metadata["stable_id"],
                    "display_label": section_metadata["display_label"],
                    "section_title": section_metadata["title"],
                }
                for key, expected_value in expected_fields.items():
                    if item.get(key) != expected_value:
                        item[key] = expected_value
                        changed = True

    failed_requests = data.get("failed_requests")
    if isinstance(failed_requests, list):
        for item in failed_requests:
            if not isinstance(item, dict):
                continue
            old_request_id = str(item.get("request_id") or "")
            if old_request_id in id_map:
                item["legacy_request_id"] = old_request_id
                item["request_id"] = id_map[old_request_id]
                changed = True

    merge_data = data.get("merge")
    if isinstance(merge_data, dict):
        merged_sections = merge_data.get("merged_sections")
        if isinstance(merged_sections, list):
            for item in merged_sections:
                if not isinstance(item, dict):
                    continue

                merged_file = str(item.get("merged_file") or "")
                expected_merged_file = merged_file_map.get(merged_file)
                if expected_merged_file and expected_merged_file != merged_file:
                    item["merged_file"] = expected_merged_file
                    changed = True

                section_index = item.get("section_index")
                try:
                    section_index = int(section_index)
                except (TypeError, ValueError):
                    section_index = None

                if section_index is None:
                    continue

                section_metadata = build_section_metadata(section_index, metadata_by_index)
                expected_fields = {
                    "section_code": section_metadata["section_code"],
                    "stable_id": section_metadata["stable_id"],
                    "display_label": section_metadata["display_label"],
                    "section_title": section_metadata["title"],
                }
                for key, expected_value in expected_fields.items():
                    if item.get(key) != expected_value:
                        item[key] = expected_value
                        changed = True

    if changed and apply:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return changed


def migrate_json_prompts_and_requests(
    run_dir: Path,
    id_map: dict[str, str],
    metadata_by_index: dict[int, dict[str, Any]],
    apply: bool,
) -> int:
    changed = 0
    for folder_name in ["prompts", "requests"]:
        folder = run_dir / folder_name
        if not folder.exists() or not folder.is_dir():
            continue
        for file in sorted(folder.glob("*.json")):
            data = json.loads(file.read_text(encoding="utf-8"))
            local_changed = False
            is_prompt_payload = folder_name == "prompts"

            old_request_id = str(data.get("request_id") or "")
            if old_request_id in id_map:
                data["legacy_request_id"] = old_request_id
                data["request_id"] = id_map[old_request_id]
                local_changed = True

            section = data.get("section")
            if isinstance(section, dict):
                section_index = section.get("index")
                try:
                    section_index = int(section_index)
                except (TypeError, ValueError):
                    section_index = None
                if section_index is not None:
                    section_metadata = build_section_metadata(section_index, metadata_by_index)
                    expected_section_fields = {
                        "section_code": section_metadata["section_code"],
                        "stable_id": section_metadata["stable_id"],
                        "display_label": section_metadata["display_label"],
                        "title": section_metadata["title"],
                    }
                    if is_prompt_payload:
                        expected_section_fields["clean_file"] = section_metadata["clean_file"]

                    for key, expected_value in expected_section_fields.items():
                        if section.get(key) != expected_value:
                            section[key] = expected_value
                            local_changed = True

            if folder_name == "requests":
                legacy_request_id = str(data.get("legacy_request_id") or "")
                match_source = legacy_request_id or old_request_id
                match = re.match(r"^section-(\d+)-chunk-(\d+)$", match_source)
                if match:
                    section_index = int(match.group(1))
                    section_metadata = build_section_metadata(section_index, metadata_by_index)
                    if data.get("stable_id") != section_metadata["stable_id"]:
                        data["stable_id"] = section_metadata["stable_id"]
                        local_changed = True
                    if data.get("display_label") != section_metadata["display_label"]:
                        data["display_label"] = section_metadata["display_label"]
                        local_changed = True

            if local_changed:
                changed += 1
                if apply:
                    file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return changed


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    workspace_root = args.base_dir.resolve() if args.base_dir else find_workspace_root(run_dir)
    if workspace_root is None:
        raise SystemExit("Unable to locate workspace root. Pass --base-dir.")

    metadata_by_index = load_section_metadata_map(run_dir, workspace_root)

    legacy_id_candidates = collect_legacy_request_ids(run_dir)
    id_map: dict[str, str] = {}
    for old_request_id in sorted(legacy_id_candidates.keys()):
        match = re.match(r"^section-(\d+)-chunk-(\d+)$", old_request_id)
        if not match:
            continue
        section_index = int(match.group(1))
        chunk_index = int(match.group(2))
        stable_id = stable_id_for_index(section_index, metadata_by_index)
        new_request_id = build_request_id(stable_id, chunk_index)
        id_map[old_request_id] = new_request_id

    request_file_changes, request_file_errors = migrate_request_files(run_dir, id_map, args.apply)

    merged_changes, merged_errors, merged_file_map = migrate_merged_sections(
        run_dir,
        metadata_by_index,
        args.apply,
    )

    summary_path = run_dir / "run_summary.json"
    summary_changed = migrate_json_payload(
        summary_path,
        id_map,
        metadata_by_index,
        merged_file_map,
        args.apply,
    )

    prompt_request_json_changed = migrate_json_prompts_and_requests(
        run_dir,
        id_map,
        metadata_by_index,
        args.apply,
    )

    file_action = "Renamed" if args.apply else "Would rename"
    json_action = "Updated" if args.apply else "Would update"
    print(f"Run directory: {run_dir}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Legacy request ids found: {len(id_map)}")
    print(f"{file_action} request/chunk files: {request_file_changes}")
    print(f"{file_action} merged section files: {merged_changes}")
    print(f"{json_action} prompt/request JSON payloads: {prompt_request_json_changed}")
    print(f"{json_action} run_summary.json: {'yes' if summary_changed else 'no'}")

    if request_file_errors or merged_errors:
        print("Errors:")
        for error_message in [*request_file_errors, *merged_errors]:
            print(f"  - {error_message}")

    if id_map:
        print("Sample mapping:")
        for old_id in sorted(id_map.keys())[:10]:
            print(f"  - {old_id} -> {id_map[old_id]}")


if __name__ == "__main__":
    main()
