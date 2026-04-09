from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import parse
from urllib import error, request


@dataclass
class Chunk:
    chunk_index: int
    text: str
    start_paragraph: int
    end_paragraph: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare or execute a translation pilot for selected split novel sections."
    )
    parser.add_argument("config", type=Path, help="Path to the pilot config JSON file.")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Base directory used to resolve relative paths from the config file.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Send the prepared prompts to the configured provider API.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing execute run by skipping chunks with saved translations.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_path(base_dir: Path, path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def reset_output_directory(path: Path, safety_root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = safety_root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise RuntimeError(
            f"Refusing to reset output path outside safety root: {resolved_path}"
        ) from error

    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def select_sections(
    sections: list[dict[str, Any]], selection: dict[str, Any]
) -> list[dict[str, Any]]:
    selected = [section for section in sections if section.get("keep_in_clean")]

    kinds = selection.get("kinds")
    if kinds:
        selected = [section for section in selected if section["kind"] in kinds]

    section_indices = selection.get("section_indices")
    if section_indices:
        indices = set(section_indices)
        selected = [section for i, section in enumerate(selected, 1) if i in indices]

    section_codes = selection.get("section_codes")
    if section_codes:
        codes = {str(code) for code in section_codes}
        selected = [
            section for section in selected if str(section.get("section_code") or "") in codes
        ]
        
    offset = selection.get("offset", 0)
    if isinstance(offset, int) and offset > 0:
        selected = selected[offset:]

    limit = selection.get("limit")
    if isinstance(limit, int) and limit > 0:
        selected = selected[:limit]

    return selected


def split_into_chunks(text: str, max_chars_per_chunk: int) -> list[Chunk]:
    stripped = text.strip()
    if not stripped:
        return [Chunk(chunk_index=1, text="", start_paragraph=1, end_paragraph=1)]

    paragraphs = stripped.split("\n\n")
    chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_length = 0
    chunk_start = 1

    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        paragraph_text = paragraph.strip("\n")
        if not paragraph_text:
            continue

        if len(paragraph_text) > max_chars_per_chunk:
            if current_parts:
                chunks.append(
                    Chunk(
                        chunk_index=len(chunks) + 1,
                        text="\n\n".join(current_parts),
                        start_paragraph=chunk_start,
                        end_paragraph=paragraph_index - 1,
                    )
                )
                current_parts = []
                current_length = 0

            line_parts = hard_split_long_text(paragraph_text, max_chars_per_chunk)
            for line_part_index, line_part in enumerate(line_parts, start=1):
                chunks.append(
                    Chunk(
                        chunk_index=len(chunks) + 1,
                        text=line_part,
                        start_paragraph=paragraph_index,
                        end_paragraph=paragraph_index,
                    )
                )
            chunk_start = paragraph_index + 1
            continue

        projected_length = current_length + len(paragraph_text)
        if current_parts:
            projected_length += 2

        if current_parts and projected_length > max_chars_per_chunk:
            chunks.append(
                Chunk(
                    chunk_index=len(chunks) + 1,
                    text="\n\n".join(current_parts),
                    start_paragraph=chunk_start,
                    end_paragraph=paragraph_index - 1,
                )
            )
            current_parts = [paragraph_text]
            current_length = len(paragraph_text)
            chunk_start = paragraph_index
            continue

        if not current_parts:
            chunk_start = paragraph_index

        current_parts.append(paragraph_text)
        current_length = projected_length

    if current_parts:
        chunks.append(
            Chunk(
                chunk_index=len(chunks) + 1,
                text="\n\n".join(current_parts),
                start_paragraph=chunk_start,
                end_paragraph=len(paragraphs),
            )
        )

    return chunks


def hard_split_long_text(text: str, max_chars_per_chunk: int) -> list[str]:
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars_per_chunk, len(text))
        parts.append(text[start:end])
        start = end
    return parts


def filter_glossary_entries(glossary: dict[str, Any], source_text: str) -> dict[str, Any]:
    characters = []
    for entry in glossary.get("character_profiles", []):
        source_name = entry.get("source_name", "")
        if source_name and source_name in source_text:
            characters.append(entry)

    terms = []
    for entry in glossary.get("term_glossary", []):
        source_term = entry.get("source_term", "")
        if source_term and source_term in source_text:
            terms.append(entry)

    return {
        "global_instructions": glossary.get("global_instructions", []),
        "character_profiles": characters,
        "term_glossary": terms,
        "style_rules": glossary.get("style_rules", []),
        "do_not_translate": glossary.get("do_not_translate", []),
        "review_notes": glossary.get("review_notes", []),
    }


def prepare_section_text(section: dict[str, Any], source_text: str) -> str:
    lines = source_text.splitlines()
    if lines and lines[0].strip() == str(section.get("header_line", "")).strip():
        lines = lines[1:]

    while lines and (not lines[0].strip() or is_decorative_heading_line(lines[0])):
        lines = lines[1:]

    prepared = "\n".join(lines).strip()
    return prepared + "\n" if prepared else ""


def is_decorative_heading_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.match(r"^<--.*-->$", stripped):
        return True
    if re.match(r"^#\d+={5,}$", stripped):
        return True
    if re.match(r"^\d{5}\s+.*={10,}$", stripped):
        return True
    return False


def format_section_heading(section: dict[str, Any]) -> str:
    section_code = section.get("section_code")
    title = section.get("title") or "Untitled"
    if section_code:
        return f"{section_code} {title}"
    return title


def build_system_prompt(glossary: dict[str, Any], translation: dict[str, Any]) -> str:
    lines = [
        f"You are translating a Korean web novel into {translation['target_language']}.",
        "Return only the translated text with no explanation.",
        "Preserve paragraph breaks and scene structure.",
        "Do not summarize, censor, or omit content unless it is outside the supplied source chunk.",
    ]

    for instruction in glossary.get("global_instructions", []):
        if instruction:
            lines.append(f"Instruction: {instruction}")

    for rule in glossary.get("style_rules", []):
        rule_text = rule.get("rule", "")
        reason_text = rule.get("reason", "")
        if rule_text:
            if reason_text:
                lines.append(f"Style rule: {rule_text} Reason: {reason_text}")
            else:
                lines.append(f"Style rule: {rule_text}")

    do_not_translate = [value for value in glossary.get("do_not_translate", []) if value]
    if do_not_translate:
        lines.append("Do not translate these strings literally unless context requires otherwise: " + ", ".join(do_not_translate))

    characters = glossary.get("character_profiles", [])
    if characters:
        lines.append("Character references:")
        for entry in characters:
            source_name = entry.get("source_name", "")
            target_name = entry.get("target_name", "")
            speech_style_notes = entry.get("speech_style_notes", "")
            personality_notes = entry.get("personality_notes", "")
            lines.append(
                f"- {source_name} -> {target_name}; speech: {speech_style_notes}; personality: {personality_notes}"
            )

    terms = glossary.get("term_glossary", [])
    if terms:
        lines.append("Preferred terminology:")
        for entry in terms:
            source_term = entry.get("source_term", "")
            target_term = entry.get("target_term", "")
            notes = entry.get("notes", "")
            lines.append(f"- {source_term} -> {target_term}; notes: {notes}")

    return "\n".join(lines)


def build_user_prompt(
    section: dict[str, Any], chunk: Chunk, translation: dict[str, Any]
) -> str:
    metadata = [
        f"Section index: {section['index']}",
        f"Section kind: {section['kind']}",
        f"Section code: {section.get('section_code') or 'n/a'}",
        f"Section title: {section.get('title') or 'n/a'}",
        f"Section heading metadata: {format_section_heading(section)}",
        f"Chunk index: {chunk.chunk_index}",
        f"Paragraph span: {chunk.start_paragraph}-{chunk.end_paragraph}",
        f"Translate from {translation['source_language']} to {translation['target_language']}",
        "Keep names and terms consistent with the supplied instructions.",
        "If the output includes a section heading, render it naturally in English without decorative divider characters.",
        "Source text follows:",
        chunk.text,
    ]
    return "\n".join(metadata)


def get_provider(model_config: dict[str, Any]) -> str:
    return model_config.get("provider", "openai-chat-completions")


def build_openai_payload(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    payload = {
        "model": model_config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": model_config.get("temperature", 0.2),
    }
    max_output_tokens = model_config.get("max_output_tokens")
    if max_output_tokens:
        payload["max_tokens"] = max_output_tokens
    return payload


def build_anthropic_payload(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    payload = {
        "model": model_config["model"],
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "temperature": model_config.get("temperature", 0.2),
        "max_tokens": model_config.get("max_output_tokens", 4096),
    }
    return payload


def build_gemini_payload(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_prompt}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generationConfig": {
            "temperature": model_config.get("temperature", 0.2),
        },
    }
    max_output_tokens = model_config.get("max_output_tokens")
    if max_output_tokens:
        payload["generationConfig"]["maxOutputTokens"] = max_output_tokens
    return payload


def build_provider_request_payload(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    provider = get_provider(model_config)
    if provider == "openai-chat-completions":
        payload = build_openai_payload(model_config, system_prompt, user_prompt)
    elif provider == "anthropic-messages":
        payload = build_anthropic_payload(model_config, system_prompt, user_prompt)
    elif provider == "gemini-generate-content":
        payload = build_gemini_payload(model_config, system_prompt, user_prompt)
    else:
        raise RuntimeError(f"Unsupported provider: {provider}")

    return {
        "provider": provider,
        "base_url": model_config["base_url"],
        "payload": payload,
    }


def call_openai_chat_completion(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    api_key_env = model_config["api_key_env"]
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise RuntimeError(f"Environment variable {api_key_env} is not set.")

    payload = build_openai_payload(model_config, system_prompt, user_prompt)

    request_body = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
        url=model_config["base_url"],
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI-compatible request failed: {exc.code} {error_body}") from exc

    return json.loads(response_body)


def call_anthropic_messages(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    api_key_env = model_config["api_key_env"]
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise RuntimeError(f"Environment variable {api_key_env} is not set.")

    payload = build_anthropic_payload(model_config, system_prompt, user_prompt)
    request_body = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
        url=model_config["base_url"],
        data=request_body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": model_config.get("anthropic_version", "2023-06-01"),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic request failed: {exc.code} {error_body}") from exc

    return json.loads(response_body)


def call_gemini_generate_content(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    api_key_env = model_config["api_key_env"]
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise RuntimeError(f"Environment variable {api_key_env} is not set.")

    payload = build_gemini_payload(model_config, system_prompt, user_prompt)
    request_body = json.dumps(payload).encode("utf-8")

    auth_mode = model_config.get("gemini_auth_mode", "query")
    request_url = model_config["base_url"]
    headers = {
        "Content-Type": "application/json",
    }
    if auth_mode == "query":
        separator = "&" if "?" in request_url else "?"
        request_url = f"{request_url}{separator}key={parse.quote(api_key)}"
    elif auth_mode == "header":
        headers["x-goog-api-key"] = api_key
    else:
        raise RuntimeError(f"Unsupported gemini_auth_mode: {auth_mode}")

    http_request = request.Request(
        url=request_url,
        data=request_body,
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini request failed: {exc.code} {error_body}") from exc

    return json.loads(response_body)


def call_model_api(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    provider = get_provider(model_config)
    if provider == "openai-chat-completions":
        return call_openai_chat_completion(model_config, system_prompt, user_prompt, timeout_seconds)
    if provider == "anthropic-messages":
        return call_anthropic_messages(model_config, system_prompt, user_prompt, timeout_seconds)
    if provider == "gemini-generate-content":
        return call_gemini_generate_content(model_config, system_prompt, user_prompt, timeout_seconds)
    raise RuntimeError(f"Unsupported provider: {provider}")


def call_model_api_with_retry(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
) -> tuple[dict[str, Any], int]:
    retry_config = model_config.get("retry", {})
    max_attempts = int(retry_config.get("max_attempts", 4))
    initial_delay_seconds = float(retry_config.get("initial_delay_seconds", 2))
    backoff_multiplier = float(retry_config.get("backoff_multiplier", 2.0))
    max_delay_seconds = float(retry_config.get("max_delay_seconds", 30))
    total_timeout_raw = retry_config.get("total_timeout_seconds")
    per_attempt_timeout = float(model_config.get("timeout_seconds", 120))

    if max_attempts < 1:
        max_attempts = 1
    if per_attempt_timeout <= 0:
        per_attempt_timeout = 120.0

    total_timeout_seconds: float | None
    if total_timeout_raw is None:
        total_timeout_seconds = None
    else:
        try:
            parsed_total_timeout = float(total_timeout_raw)
            total_timeout_seconds = parsed_total_timeout if parsed_total_timeout > 0 else None
        except (TypeError, ValueError):
            total_timeout_seconds = None

    delay_seconds = max(0.0, initial_delay_seconds)
    last_error: Exception | None = None
    start_time = time.monotonic()

    for attempt in range(1, max_attempts + 1):
        if total_timeout_seconds is not None:
            elapsed_seconds = time.monotonic() - start_time
            remaining_seconds = total_timeout_seconds - elapsed_seconds
            if remaining_seconds <= 0:
                raise RuntimeError(
                    f"Request exceeded retry.total_timeout_seconds={total_timeout_seconds}s "
                    f"before attempt {attempt}."
                ) from last_error
            attempt_timeout = min(per_attempt_timeout, remaining_seconds)
        else:
            attempt_timeout = per_attempt_timeout

        try:
            api_response = call_model_api(
                model_config,
                system_prompt,
                user_prompt,
                timeout_seconds=attempt_timeout,
            )
            return api_response, attempt
        except Exception as error:
            last_error = error
            if attempt == max_attempts:
                break
            if delay_seconds > 0:
                if total_timeout_seconds is not None:
                    elapsed_seconds = time.monotonic() - start_time
                    remaining_seconds = total_timeout_seconds - elapsed_seconds
                    if remaining_seconds <= 0:
                        raise RuntimeError(
                            f"Request exceeded retry.total_timeout_seconds={total_timeout_seconds}s "
                            f"after attempt {attempt}."
                        ) from error
                    time.sleep(min(delay_seconds, remaining_seconds))
                else:
                    time.sleep(delay_seconds)
            delay_seconds = min(max_delay_seconds, delay_seconds * backoff_multiplier or max_delay_seconds)

    raise RuntimeError(f"Request failed after {max_attempts} attempts: {last_error}") from last_error


def extract_openai_translation_text(api_response: dict[str, Any]) -> str:
    choices = api_response.get("choices", [])
    if not choices:
        raise RuntimeError("OpenAI response did not contain any choices.")

    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        if parts:
            return "\n".join(parts).strip()

    raise RuntimeError("Unable to extract translated text from OpenAI response.")


def extract_anthropic_translation_text(api_response: dict[str, Any]) -> str:
    content = api_response.get("content", [])
    if not isinstance(content, list):
        raise RuntimeError("Anthropic response content was not a list.")

    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if text:
                parts.append(text)

    if parts:
        return "\n".join(parts).strip()

    raise RuntimeError("Unable to extract translated text from Anthropic response.")


def extract_gemini_translation_text(api_response: dict[str, Any]) -> str:
    candidates = api_response.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini response did not contain any candidates.")

    parts = []
    first_candidate = candidates[0]
    content = first_candidate.get("content", {}) if isinstance(first_candidate, dict) else {}
    content_parts = content.get("parts", []) if isinstance(content, dict) else []
    for item in content_parts:
        if isinstance(item, dict):
            text = item.get("text", "")
            if text:
                parts.append(text)

    if parts:
        return "\n".join(parts).strip()

    raise RuntimeError("Unable to extract translated text from Gemini response.")


def extract_translation_text(api_response: dict[str, Any], provider: str) -> str:
    if provider == "openai-chat-completions":
        return extract_openai_translation_text(api_response)
    if provider == "anthropic-messages":
        return extract_anthropic_translation_text(api_response)
    if provider == "gemini-generate-content":
        return extract_gemini_translation_text(api_response)
    raise RuntimeError(f"Unsupported provider: {provider}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_usage(api_response: dict[str, Any], provider: str) -> dict[str, int | None]:
    if provider == "openai-chat-completions":
        usage = api_response.get("usage", {})
        return {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }

    if provider == "anthropic-messages":
        usage = api_response.get("usage", {})
        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")
        total_tokens = None
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
            total_tokens = prompt_tokens + completion_tokens
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    if provider == "gemini-generate-content":
        usage = api_response.get("usageMetadata", {})
        return {
            "prompt_tokens": usage.get("promptTokenCount"),
            "completion_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
        }

    return {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }


def estimate_cost(model_config: dict[str, Any], usage: dict[str, int | None]) -> float | None:
    pricing = model_config.get("pricing", {})
    input_cost_per_million = pricing.get("input_cost_per_million_tokens")
    output_cost_per_million = pricing.get("output_cost_per_million_tokens")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")

    if not isinstance(input_cost_per_million, (int, float)):
        return None
    if not isinstance(output_cost_per_million, (int, float)):
        return None
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None

    input_cost = (prompt_tokens / 1_000_000) * float(input_cost_per_million)
    output_cost = (completion_tokens / 1_000_000) * float(output_cost_per_million)
    return round(input_cost + output_cost, 6)


def summarize_usage(requests: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    has_prompt = False
    has_completion = False
    has_total = False
    cost_estimate = 0.0
    has_cost = False

    for item in requests:
        usage = item.get("usage", {})
        prompt_value = usage.get("prompt_tokens")
        completion_value = usage.get("completion_tokens")
        total_value = usage.get("total_tokens")
        if isinstance(prompt_value, int):
            prompt_tokens += prompt_value
            has_prompt = True
        if isinstance(completion_value, int):
            completion_tokens += completion_value
            has_completion = True
        if isinstance(total_value, int):
            total_tokens += total_value
            has_total = True

        cost_value = item.get("cost_estimate")
        if isinstance(cost_value, (int, float)):
            cost_estimate += float(cost_value)
            has_cost = True

    return {
        "prompt_tokens": prompt_tokens if has_prompt else None,
        "completion_tokens": completion_tokens if has_completion else None,
        "total_tokens": total_tokens if has_total else None,
        "cost_estimate": round(cost_estimate, 6) if has_cost else None,
    }


def generate_qa_report(
    output_root: Path,
    requests: list[dict[str, Any]],
    glossary: dict[str, Any],
) -> dict[str, Any]:
    translation_dir = output_root / "translations"
    source_dir = output_root / "source_chunks"
    issues: list[dict[str, Any]] = []

    for item in requests:
        translation_file = item.get("translation_file")
        request_id = item.get("request_id")
        status = item.get("status")
        if status not in {"completed", "skipped_existing"}:
            continue
        if not translation_file or not request_id:
            continue

        translation_path = translation_dir / translation_file
        source_path = source_dir / f"{request_id}.txt"
        if not translation_path.exists() or not source_path.exists():
            continue

        translation_text = translation_path.read_text(encoding="utf-8")
        source_text = source_path.read_text(encoding="utf-8")

        source_length = len(source_text.strip())
        translation_length = len(translation_text.strip())
        if source_length > 0 and translation_length > 0:
            ratio = translation_length / source_length
            if ratio < 0.35:
                issues.append(
                    {
                        "request_id": request_id,
                        "type": "length_ratio",
                        "severity": "warning",
                        "details": f"Translation length ratio is low: {ratio:.2f}",
                    }
                )

        for entry in glossary.get("character_profiles", []):
            source_name = entry.get("source_name", "")
            target_name = entry.get("target_name", "")
            if source_name and target_name and source_name in source_text and target_name not in translation_text:
                issues.append(
                    {
                        "request_id": request_id,
                        "type": "character_glossary_missing",
                        "severity": "warning",
                        "details": f"Source name '{source_name}' appeared but target name '{target_name}' was not found.",
                    }
                )

        for entry in glossary.get("term_glossary", []):
            source_term = entry.get("source_term", "")
            target_term = entry.get("target_term", "")
            if source_term and target_term and source_term in source_text and target_term not in translation_text:
                issues.append(
                    {
                        "request_id": request_id,
                        "type": "term_glossary_missing",
                        "severity": "warning",
                        "details": f"Source term '{source_term}' appeared but target term '{target_term}' was not found.",
                    }
                )

    report = {
        "issue_count": len(issues),
        "issues": issues,
    }
    write_json(output_root / "qa_report.json", report)
    return report


def merge_translation_outputs(
    output_root: Path,
    selected_sections: list[dict[str, Any]],
    summary_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    translation_dir = output_root / "translations"
    merged_sections_dir = output_root / "merged_sections"
    ensure_directory(merged_sections_dir)

    requests_by_section: dict[int, list[dict[str, Any]]] = {}
    for item in summary_requests:
        translation_file = item.get("translation_file")
        if not translation_file:
            continue
        requests_by_section.setdefault(item["section_index"], []).append(item)

    merged_sections: list[dict[str, Any]] = []
    final_parts: list[str] = []

    for section in selected_sections:
        request_items = requests_by_section.get(section["index"], [])
        if not request_items:
            continue

        ordered_items = sorted(request_items, key=lambda item: item["chunk_index"])
        chunk_texts: list[str] = []
        for item in ordered_items:
            translation_file = item.get("translation_file")
            if not translation_file:
                continue
            translation_path = translation_dir / translation_file
            if not translation_path.exists():
                continue
            chunk_text = translation_path.read_text(encoding="utf-8").strip()
            if chunk_text:
                chunk_texts.append(chunk_text)

        if not chunk_texts:
            continue

        merged_text = "\n\n".join(chunk_texts).strip() + "\n"
        merged_file = f"section-{section['index']:04d}.txt"
        (merged_sections_dir / merged_file).write_text(merged_text, encoding="utf-8")
        merged_sections.append(
            {
                "section_index": section["index"],
                "section_code": section.get("section_code"),
                "section_title": section.get("title"),
                "merged_file": merged_file,
                "chunk_count": len(chunk_texts),
            }
        )
        final_parts.append(merged_text.rstrip())

    final_translation_file = None
    if final_parts:
        final_translation_file = "final_translated.txt"
        (output_root / final_translation_file).write_text(
            "\n\n".join(final_parts).strip() + "\n",
            encoding="utf-8",
        )

    return {
        "merged_section_count": len(merged_sections),
        "merged_sections": merged_sections,
        "final_translation_file": final_translation_file,
    }


def load_existing_run_summary(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return load_json(path)


def execute_translation_request(
    model_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    provider: str,
    response_path: Path,
    translation_path: Path,
) -> dict[str, Any]:
    api_response, attempts = call_model_api_with_retry(
        model_config,
        system_prompt,
        user_prompt,
    )
    translation_text = extract_translation_text(api_response, provider)
    usage = extract_usage(api_response, provider)
    write_json(response_path, api_response)
    translation_path.write_text(translation_text + "\n", encoding="utf-8")
    return {
        "status": "completed",
        "attempts": attempts,
        "usage": usage,
        "cost_estimate": estimate_cost(model_config, usage),
    }


def get_parallel_worker_count(config: dict[str, Any]) -> int:
    execution = config.get("execution", {})
    max_workers = execution.get("max_workers", 1)
    try:
        max_workers = int(max_workers)
    except (TypeError, ValueError):
        max_workers = 1
    return max(1, max_workers)


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_json(config_path)
    base_dir = args.base_dir.resolve() if args.base_dir else config_path.parent.resolve()

    preprocess_config = load_json(resolve_path(base_dir, config["preprocess_config"]))
    glossary = load_json(resolve_path(base_dir, config["glossary_file"]))
    preprocess_output_dir = resolve_path(base_dir, preprocess_config["output_dir"])
    manifest_path = preprocess_output_dir / "manifests" / "sections.json"
    manifest = load_json(manifest_path)
    cleaned_dir = preprocess_output_dir / "cleaned_split"

    selected_sections = select_sections(manifest["sections"], config["selection"])
    if not selected_sections:
        raise RuntimeError("No sections matched the pilot selection.")

    run_name = config.get("run_name") or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_base_dir = resolve_path(base_dir, config["output_dir"])
    ensure_directory(run_base_dir)
    output_root = run_base_dir / run_name
    if args.resume:
        ensure_directory(output_root)
    else:
        reset_output_directory(output_root, run_base_dir)
    prompts_dir = output_root / "prompts"
    request_dir = output_root / "requests"
    source_dir = output_root / "source_chunks"
    response_dir = output_root / "responses"
    translation_dir = output_root / "translations"
    ensure_directory(prompts_dir)
    ensure_directory(request_dir)
    ensure_directory(source_dir)
    if args.execute:
        ensure_directory(response_dir)
        ensure_directory(translation_dir)

    previous_summary = load_existing_run_summary(output_root / "run_summary.json") if args.resume else None
    summary_requests: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    pending_execute_tasks: list[dict[str, Any]] = []

    for section in selected_sections:
        clean_file = section.get("clean_file")
        if not clean_file:
            continue

        source_text = (cleaned_dir / clean_file).read_text(encoding="utf-8")
        prepared_text = prepare_section_text(section, source_text)
        chunks = split_into_chunks(prepared_text, config["translation"]["max_chars_per_chunk"])

        for chunk in chunks:
            filtered_glossary = filter_glossary_entries(glossary, chunk.text)
            system_prompt = build_system_prompt(filtered_glossary, config["translation"])
            user_prompt = build_user_prompt(section, chunk, config["translation"])

            request_id = (
                f"section-{section['index']:04d}-chunk-{chunk.chunk_index:03d}"
            )
            prompt_payload = {
                "request_id": request_id,
                "section": {
                    "index": section["index"],
                    "kind": section["kind"],
                    "section_code": section.get("section_code"),
                    "title": section.get("title"),
                    "clean_file": clean_file,
                },
                "chunk": {
                    "chunk_index": chunk.chunk_index,
                    "start_paragraph": chunk.start_paragraph,
                    "end_paragraph": chunk.end_paragraph,
                    "character_count": len(chunk.text),
                },
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
            write_json(prompts_dir / f"{request_id}.json", prompt_payload)

            request_payload = {
                "request_id": request_id,
                "provider": get_provider(config["model"]),
                "model": config["model"]["model"],
                "base_url": config["model"]["base_url"],
                "temperature": config["model"].get("temperature", 0.2),
                "max_output_tokens": config["model"].get("max_output_tokens"),
                "provider_request": build_provider_request_payload(
                    config["model"],
                    system_prompt,
                    user_prompt,
                ),
            }
            write_json(request_dir / f"{request_id}.json", request_payload)
            (source_dir / f"{request_id}.txt").write_text(chunk.text, encoding="utf-8")

            request_summary = {
                "request_id": request_id,
                "section_index": section["index"],
                "section_code": section.get("section_code"),
                "section_title": section.get("title"),
                "chunk_index": chunk.chunk_index,
                "character_count": len(chunk.text),
            }

            if args.execute:
                translation_file_name = f"{request_id}.txt"
                translation_path = translation_dir / translation_file_name
                response_path = response_dir / f"{request_id}.json"
                if args.resume and translation_path.exists():
                    request_summary["translation_file"] = translation_file_name
                    request_summary["status"] = "skipped_existing"
                    if previous_summary:
                        for item in previous_summary.get("requests", []):
                            if item.get("request_id") == request_id and item.get("attempts"):
                                request_summary["attempts"] = item["attempts"]
                                break
                else:
                    provider = get_provider(config["model"])
                    request_summary["translation_file"] = translation_file_name
                    pending_execute_tasks.append(
                        {
                            "request_id": request_id,
                            "section_index": section["index"],
                            "chunk_index": chunk.chunk_index,
                            "summary_index": len(summary_requests),
                            "provider": provider,
                            "system_prompt": system_prompt,
                            "user_prompt": user_prompt,
                            "response_path": response_path,
                            "translation_path": translation_path,
                        }
                    )

            summary_requests.append(request_summary)

    if args.execute and pending_execute_tasks:
        max_workers = get_parallel_worker_count(config)
        total_tasks = len(pending_execute_tasks)
        print(
            f"[execute] API 요청 실행 시작: total={total_tasks}, workers={max_workers}",
            flush=True,
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_map = {
                executor.submit(
                    execute_translation_request,
                    config["model"],
                    task["system_prompt"],
                    task["user_prompt"],
                    task["provider"],
                    task["response_path"],
                    task["translation_path"],
                ): task
                for task in pending_execute_tasks
            }

            completed_count = 0
            pending_futures = set(future_map.keys())
            next_progress_log_at = time.monotonic() + 10.0

            while pending_futures:
                done_futures, pending_futures = concurrent.futures.wait(
                    pending_futures,
                    timeout=2.0,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                if not done_futures:
                    now = time.monotonic()
                    if now >= next_progress_log_at:
                        print(
                            f"[execute] 진행 중... completed={completed_count}/{total_tasks}, "
                            f"in_flight={len(pending_futures)}",
                            flush=True,
                        )
                        next_progress_log_at = now + 10.0
                    continue

                for future in done_futures:
                    completed_count += 1
                    next_progress_log_at = time.monotonic() + 10.0
                    task = future_map[future]
                    request_summary = summary_requests[task["summary_index"]]
                    try:
                        result = future.result()
                        request_summary.update(result)
                        if (
                            completed_count == total_tasks
                            or completed_count == 1
                            or completed_count % 10 == 0
                        ):
                            print(
                                f"[execute] 완료 {completed_count}/{total_tasks}: {task['request_id']}",
                                flush=True,
                            )
                    except Exception as error:
                        request_summary["status"] = "failed"
                        request_summary["error"] = str(error)
                        failed_requests.append(
                            {
                                "request_id": task["request_id"],
                                "section_index": task["section_index"],
                                "chunk_index": task["chunk_index"],
                                "error": str(error),
                            }
                        )
                        print(
                            f"[execute] 실패 {completed_count}/{total_tasks}: {task['request_id']} -> {error}",
                            flush=True,
                        )
        except KeyboardInterrupt:
            # Stop waiting for worker shutdown so Ctrl+C can terminate promptly.
            executor.shutdown(wait=False, cancel_futures=True)
            raise SystemExit("Interrupted by user.")
        else:
            executor.shutdown(wait=True)
    elif args.execute:
        print("[execute] 실행할 신규 요청이 없습니다. (resume으로 모두 건너뜀)", flush=True)

    summary = {
        "work_id": config["work_id"],
        "run_name": run_name,
        "execute": args.execute,
        "resume": args.resume,
        "parallel_workers": get_parallel_worker_count(config) if args.execute else 1,
        "output_dir": str(output_root),
        "selected_section_count": len(selected_sections),
        "request_count": len(summary_requests),
        "failed_request_count": len(failed_requests),
        "failed_requests": failed_requests,
        "requests": summary_requests,
    }

    if args.execute:
        summary["usage"] = summarize_usage(summary_requests)
        summary["merge"] = merge_translation_outputs(output_root, selected_sections, summary_requests)
        summary["qa"] = generate_qa_report(output_root, summary_requests, glossary)

    write_json(output_root / "run_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if failed_requests:
        raise SystemExit(1)


if __name__ == "__main__":
    main()