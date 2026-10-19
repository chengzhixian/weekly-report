#!/usr/bin/env python3
"""Collect compact evidence from Claude Code session JSONL files for weekly reports.

Claude Code stores one JSONL transcript per session under
`<claude-home>/projects/<slugged-cwd>/<session-id>.jsonl`. Each line is a typed
event. Unlike raw log scraping, this format gives us structured fields directly:

  * `pr-link` events  -> PR number / url / repository
  * `tool_use` (Bash) -> the exact command + description
  * `tool_use` (Edit/Write/NotebookEdit) -> changed file paths
  * per-event `cwd`, `gitBranch`, `version`, `timestamp`

We read those fields instead of guessing them with regex, then emit a compact
markdown/JSON digest the report writer can turn into prose.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any


PR_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+")
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Inclusive end date, YYYY-MM-DD")
    parser.add_argument(
        "--claude-home",
        default=os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")),
        help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--max-sessions", type=int, default=40)
    parser.add_argument("--max-text", type=int, default=360, help="Maximum characters kept per message snippet")
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


def date_bounds(start: str, end: str) -> tuple[dt.datetime, dt.datetime]:
    start_date = dt.date.fromisoformat(start)
    end_date = dt.date.fromisoformat(end)
    start_dt = dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
    end_dt = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc)
    return start_dt, end_dt


def session_files(claude_home: Path) -> list[Path]:
    roots = [claude_home / "projects"]
    seen: set[Path] = set()
    files: list[Path] = []
    for root in roots:
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
    value = re.sub(r"\s+", " ", str(text)).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def append_unique(values: list[str], value: str, limit: int) -> None:
    if value and value not in values and len(values) < limit:
        values.append(value)


def is_noise_user_text(text: str) -> bool:
    """Drop slash-command scaffolding, tool plumbing and harness-injected blocks."""
    if not text:
        return True
    prefixes = (
        "<command-message>",
        "<command-name>",
        "<command-args>",
        "<local-command-stdout>",
        "<local-command-stderr>",
        "<bash-input>",
        "<bash-stdout>",
        "<bash-stderr>",
        "<system-reminder>",
        "<environment_context>",
        "<user-prompt-submit-hook>",
        "Caveat: The messages below",
        "[Request interrupted",
        "API Error",
    )
    return any(text.startswith(p) for p in prefixes)


def user_text(content: Any, max_text: int) -> str:
    """User message content is either a plain string or a list of content blocks."""
    if isinstance(content, str):
        return compact(content, max_text)
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                pieces.append(str(item.get("text", "")))
            elif isinstance(item, str):
                pieces.append(item)
        return compact(" ".join(pieces), max_text)
    return ""


def assistant_text(content: Any, max_text: int) -> str:
    if not isinstance(content, list):
        return ""
    pieces = [
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return compact(" ".join(pieces), max_text)


def summarize_session(path: Path, start_dt: dt.datetime, end_dt: dt.datetime, max_text: int) -> dict[str, Any] | None:
    timestamps: list[dt.datetime] = []
    active = False
    cwd: str | None = None
    branches: list[str] = []
    version: str | None = None
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    bash_commands: list[str] = []
    git_commands: list[str] = []
    changed_files: list[str] = []
    pr_urls: list[str] = []
    pr_links: list[dict[str, Any]] = []

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

            if event.get("cwd") and cwd is None:
                cwd = event["cwd"]
            if event.get("gitBranch"):
                append_unique(branches, event["gitBranch"], 8)
            if event.get("version"):
                version = event["version"]

            etype = event.get("type")

            if etype == "pr-link":
                url = event.get("prUrl")
                if url:
                    append_unique(pr_urls, url, 20)
                    link = {
                        "url": url,
                        "repository": event.get("prRepository"),
                        "number": event.get("prNumber"),
                    }
                    if link not in pr_links and len(pr_links) < 20:
                        pr_links.append(link)
                continue

            message = event.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")

            if role == "user" and not event.get("isMeta"):
                text = user_text(content, max_text)
                if not is_noise_user_text(text):
                    append_unique(user_messages, text, 12)
                # PR links sometimes only appear inside pasted text.
                for url in PR_RE.findall(text):
                    append_unique(pr_urls, url, 20)
            elif role == "assistant":
                text = assistant_text(content, max_text)
                if text:
                    append_unique(assistant_messages, text, 12)
                for url in PR_RE.findall(text):
                    append_unique(pr_urls, url, 20)
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict) or item.get("type") != "tool_use":
                            continue
                        name = item.get("name")
                        inp = item.get("input") or {}
                        if name == "Bash" and isinstance(inp.get("command"), str):
                            command = compact(inp["command"], max_text)
                            append_unique(bash_commands, command, 24)
                            if re.search(r"(^|\s)git\s", command):
                                append_unique(git_commands, command, 16)
                        elif name in EDIT_TOOLS and isinstance(inp.get("file_path"), str):
                            append_unique(changed_files, inp["file_path"], 60)

    if not active:
        return None

    timestamps.sort()
    return {
        "path": str(path),
        "session_id": path.stem,
        "cwd": cwd,
        "git_branches": branches,
        "version": version,
        "started_at": timestamps[0].isoformat() if timestamps else None,
        "last_active_at": timestamps[-1].isoformat() if timestamps else None,
        "user_messages": user_messages,
        "assistant_messages_tail": assistant_messages[-5:],
        "bash_commands_sample": bash_commands[:12],
        "git_commands": git_commands,
        "changed_files": changed_files,
        "pr_urls": pr_urls,
        "pr_links": pr_links,
    }


def collect(args: argparse.Namespace) -> dict[str, Any]:
    start_dt, end_dt = date_bounds(args.start, args.end)
    sessions = []
    for path in session_files(Path(args.claude_home).expanduser()):
        summary = summarize_session(path, start_dt, end_dt, args.max_text)
        if summary:
            sessions.append(summary)
    sessions.sort(key=lambda item: item.get("last_active_at") or "", reverse=True)
    return {
        "date_range": {"start": args.start, "end": args.end},
        "session_count": len(sessions),
        "sessions": sessions[: args.max_sessions],
        "truncated": len(sessions) > args.max_sessions,
    }


def print_markdown(data: dict[str, Any]) -> None:
    start = data["date_range"]["start"]
    end = data["date_range"]["end"]
    print(f"# Claude Code Activity Evidence {start} to {end}")
    print()
    print(f"Sessions found: {data['session_count']}")
    if data.get("truncated"):
        print("Output truncated by --max-sessions.")
    print()
    for idx, session in enumerate(data["sessions"], 1):
        cwd = session.get("cwd") or "unknown cwd"
        branches = ", ".join(session.get("git_branches") or []) or "n/a"
        print(f"## {idx}. {cwd}")
        print(f"- Active: {session.get('started_at')} to {session.get('last_active_at')}")
        print(f"- Branch: {branches}")
        print(f"- File: `{session.get('path')}`")
        if session.get("user_messages"):
            print("- User prompts:")
            for message in session["user_messages"][:6]:
                print(f"  - {message}")
        if session.get("assistant_messages_tail"):
            print("- Assistant summaries:")
            for message in session["assistant_messages_tail"][-3:]:
                print(f"  - {message}")
        if session.get("changed_files"):
            print("- Changed files:")
            for file_path in session["changed_files"][:12]:
                print(f"  - `{file_path}`")
        if session.get("pr_links"):
            print("- PRs:")
            for link in session["pr_links"]:
                repo = link.get("repository") or ""
                num = link.get("number")
                label = f"{repo}#{num}" if repo and num else (link.get("url") or "")
                print(f"  - {label} {link.get('url')}")
        elif session.get("pr_urls"):
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
