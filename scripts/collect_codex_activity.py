#!/usr/bin/env python3
"""Collect compact evidence from Codex session JSONL files for weekly reports."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PR_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+")
COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
FILE_PATCH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Inclusive end date, YYYY-MM-DD")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--max-sessions", type=int, default=40)
    parser.add_argument("--max-text", type=int, default=2400, help="Maximum characters kept per message snippet")
    parser.add_argument("--exclude-session-id", action="append", default=[])
    parser.add_argument("--include-subagents", action="store_true")
    return parser.parse_args()


def parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def date_bounds(start: str, end: str, timezone: str = "Asia/Shanghai") -> tuple[dt.datetime, dt.datetime]:
    start_date = dt.date.fromisoformat(start)
    end_date = dt.date.fromisoformat(end)
    if end_date < start_date:
        raise ValueError("end date must not precede start date")
    tz = ZoneInfo(timezone)
    start_dt = dt.datetime.combine(start_date, dt.time.min, tzinfo=tz)
    end_dt = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
    return start_dt, end_dt


def session_files(codex_home: Path) -> list[Path]:
    candidates = [
        codex_home / "sessions",
        codex_home / "archived_sessions",
        Path.home() / ".codex" / "sessions",
        Path.home() / ".codex" / "archived_sessions",
    ]
    seen: set[Path] = set()
    files: list[Path] = []
    for root in candidates:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(path)
    return files


def compact(text: Any, limit: int) -> str:
    if text is None:
        return ""
    value = str(text)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def payload_text(payload: dict[str, Any], max_text: int) -> str:
    if isinstance(payload.get("message"), str):
        return compact(payload["message"], max_text)
    content = payload.get("content")
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if text:
                    pieces.append(str(text))
            elif isinstance(item, str):
                pieces.append(item)
        return compact(" ".join(pieces), max_text)
    if isinstance(payload.get("arguments"), str):
        return compact(payload["arguments"], max_text)
    if isinstance(payload.get("input"), str):
        return compact(payload["input"], max_text)
    return ""


def append_unique(values: list[str], value: str, limit: int) -> None:
    if value and value not in values and len(values) < limit:
        values.append(value)


def is_noise_user_text(text: str) -> bool:
    prefixes = (
        "<environment_context>",
        "<permissions instructions>",
        "<skill>",
        "<turn_aborted>",
        "# AGENTS.md instructions",
        "You are Codex,",
    )
    return not text or any(text.startswith(prefix) for prefix in prefixes)


def extract_command(payload: dict[str, Any], max_text: int) -> str:
    if payload.get("type") != "function_call":
        return ""
    name = payload.get("name")
    raw_args = payload.get("arguments")
    if not isinstance(raw_args, str):
        return ""
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return ""
    cmd = args.get("cmd")
    workdir = args.get("workdir")
    if not cmd:
        return ""
    prefix = f"{workdir}: " if workdir else ""
    return compact(f"{prefix}{cmd}", max_text)


def summarize_session(path: Path, start_dt: dt.datetime, end_dt: dt.datetime, max_text: int) -> dict[str, Any] | None:
    timestamps: list[dt.datetime] = []
    active = False
    meta: dict[str, Any] = {}
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    final_messages: list[str] = []
    user_count = 0
    assistant_count = 0
    clipped_texts = 0
    commands: list[str] = []
    git_commands: list[str] = []
    changed_files: list[str] = []
    pr_urls: list[str] = []
    commits: list[str] = []

    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return None

    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = parse_timestamp(event.get("timestamp"))
            if ts:
                timestamps.append(ts)
                if start_dt <= ts < end_dt:
                    active = True

            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue

            if event.get("type") == "session_meta":
                meta = {
                    "id": payload.get("id"),
                    "cwd": payload.get("cwd"),
                    "originator": payload.get("originator"),
                    "source": payload.get("source"),
                }
                nested_ts = parse_timestamp(payload.get("timestamp"))
                if nested_ts:
                    timestamps.append(nested_ts)
                    if start_dt <= nested_ts < end_dt:
                        active = True

            # Session metadata is useful across the whole file; work evidence is
            # included only when this event itself falls within the local dates.
            if not ts or not start_dt <= ts < end_dt:
                continue

            text = payload_text(payload, max_text)
            if len(payload_text(payload, max_text + 1)) > max_text:
                clipped_texts += 1
            for url in PR_RE.findall(text):
                append_unique(pr_urls, url, 20)
            for sha in COMMIT_RE.findall(text):
                append_unique(commits, sha, 30)

            if event.get("type") == "event_msg" and payload.get("type") == "user_message":
                if not is_noise_user_text(text):
                    user_count += 1
                    append_unique(user_messages, text, 200)
            elif event.get("type") == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                if role == "user":
                    if not is_noise_user_text(text):
                        user_count += 1
                        append_unique(user_messages, text, 200)
                elif role == "assistant":
                    assistant_count += 1
                    if text:
                        assistant_messages.append(text)
                    if payload.get("phase") in {"final_answer", "final"} and text:
                        append_unique(final_messages, text, 200)
            elif event.get("type") == "event_msg" and payload.get("type") in {"task_complete", "agent_message"}:
                if payload.get("type") == "task_complete":
                    text = compact(payload.get("last_agent_message", ""), max_text)
                    append_unique(final_messages, text, 200)
                elif payload.get("phase") in {"final_answer", "final"}:
                    append_unique(final_messages, text, 200)

            command = extract_command(payload, max_text)
            if command:
                append_unique(commands, command, 24)
                if "git " in command or command.startswith("git "):
                    append_unique(git_commands, command, 16)

            if payload.get("type") in {"custom_tool_call", "function_call"}:
                raw = payload.get("input") or payload.get("arguments") or ""
                if isinstance(raw, str):
                    for changed in FILE_PATCH_RE.findall(raw):
                        append_unique(changed_files, changed.strip(), 40)

    if not active:
        return None

    timestamps.sort()
    return {
        "path": str(path),
        "id": meta.get("id"),
        "cwd": meta.get("cwd"),
        "originator": meta.get("originator"),
        "source": meta.get("source"),
        "started_at": timestamps[0].isoformat() if timestamps else None,
        "last_active_at": timestamps[-1].isoformat() if timestamps else None,
        "user_messages": user_messages,
        "assistant_messages_tail": assistant_messages[-5:],
        "assistant_final_messages": final_messages,
        "evidence_limits": {"user_message_events": user_count,
                            "assistant_message_events": assistant_count,
                            "clipped_texts": clipped_texts,
                            "message_list_at_limit": len(user_messages) >= 200 or len(final_messages) >= 200},
        "commands_sample": commands[:12],
        "git_commands": git_commands,
        "changed_files": changed_files,
        "pr_urls": pr_urls,
        "commit_hashes": commits,
    }


def collect(args: argparse.Namespace) -> dict[str, Any]:
    start_dt, end_dt = date_bounds(args.start, args.end, args.timezone)
    sessions = []
    for path in session_files(Path(args.codex_home)):
        summary = summarize_session(path, start_dt, end_dt, args.max_text)
        source = summary.get("source") if summary else None
        if not args.include_subagents and ((isinstance(source, dict) and "subagent" in source)
                                           or (isinstance(source, str) and source.startswith("subagent"))):
            continue
        if summary and summary.get("id") not in args.exclude_session_id:
            sessions.append(summary)
    sessions.sort(key=lambda item: item.get("last_active_at") or "", reverse=True)
    return {
        "date_range": {"start": args.start, "end": args.end, "timezone": args.timezone},
        "session_count": len(sessions),
        "sessions": sessions[: args.max_sessions],
        "truncated": len(sessions) > args.max_sessions,
    }


def print_markdown(data: dict[str, Any]) -> None:
    start = data["date_range"]["start"]
    end = data["date_range"]["end"]
    print(f"# Codex Activity Evidence {start} to {end}")
    print()
    print(f"Sessions found: {data['session_count']}")
    if data.get("truncated"):
        print("Output truncated by --max-sessions.")
    print()
    for idx, session in enumerate(data["sessions"], 1):
        cwd = session.get("cwd") or "unknown cwd"
        print(f"## {idx}. {cwd}")
        print(f"- Active: {session.get('started_at')} to {session.get('last_active_at')}")
        print(f"- File: `{session.get('path')}`")
        if session.get("user_messages"):
            print("- User prompts:")
            for message in session["user_messages"][:6]:
                print(f"  - {message}")
        if session.get("assistant_final_messages") or session.get("assistant_messages_tail"):
            print("- Assistant summaries/messages:")
            for message in session.get("assistant_final_messages") or session["assistant_messages_tail"][-3:]:
                print(f"  - {message}")
        if session.get("evidence_limits", {}).get("clipped_texts") or session.get("evidence_limits", {}).get("message_list_at_limit"):
            print("- Coverage: message evidence is clipped; inspect original in-range messages before finalizing claims.")
        if session.get("changed_files"):
            print("- Changed files:")
            for file_path in session["changed_files"][:12]:
                print(f"  - `{file_path}`")
        if session.get("pr_urls"):
            print("- PR URLs:")
            for url in session["pr_urls"]:
                print(f"  - {url}")
        if session.get("git_commands"):
            print("- Git commands:")
            for command in session["git_commands"][:6]:
                print(f"  - `{command}`")
        print()


def main() -> None:
    args = parse_args()
    data = collect(args)
    if args.format == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_markdown(data)


if __name__ == "__main__":
    main()
