import json
import os
import sys
import tempfile
from pathlib import Path
import re


ID_RE = re.compile(r"\b\d{7,}\b")
KEYWORD_PAREN_ID_RE = re.compile(r"\b(?P<kind>Thread|Client) \((?P<id>\d{7,})\)")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


class FacebookNameCache:
    def __init__(self) -> None:
        self.path = Path(
            os.getenv("FB_LOG_NAME_CACHE_PATH")
            or str(Path(tempfile.gettempdir()) / "murmur-fb-names.json")
        )
        self.keep_ids = env_bool("FB_LOG_NAMES_KEEP_IDS", True)
        self.mtime: float | None = None
        self.users: dict[str, str] = {}
        self.threads: dict[str, str] = {}

    def refresh(self) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            return

        if self.mtime == stat.st_mtime:
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        users = raw.get("users", {}) if isinstance(raw, dict) else {}
        threads = raw.get("threads", {}) if isinstance(raw, dict) else {}
        self.users = {
            str(user_id): str(name)
            for user_id, name in users.items()
            if user_id and name
        } if isinstance(users, dict) else {}
        self.threads = {
            str(thread_id): str(name)
            for thread_id, name in threads.items()
            if thread_id and name
        } if isinstance(threads, dict) else {}
        self.mtime = stat.st_mtime

    def label(self, item_id: str, name: str) -> str:
        if self.keep_ids:
            return f"{name} ({item_id})"
        return name

    def rewrite_line(self, line: str) -> str:
        if "fbchat-muqit" not in line:
            return line

        self.refresh()
        if not self.users and not self.threads:
            return line

        def replace_keyword(match: re.Match[str]) -> str:
            item_id = match.group("id")
            name = self.threads.get(item_id) or self.users.get(item_id)
            if not name:
                return match.group(0)
            return f"{match.group('kind')} {self.label(item_id, name)}"

        line = KEYWORD_PAREN_ID_RE.sub(replace_keyword, line)

        def replace(match: re.Match[str]) -> str:
            item_id = match.group(0)
            start, end = match.span()
            parenthesized = (
                start > 0
                and end < len(line)
                and line[start - 1] == "("
                and line[end] == ")"
            )
            if parenthesized:
                return item_id
            if item_id in self.users:
                return self.label(item_id, self.users[item_id])
            if item_id in self.threads:
                return self.label(item_id, self.threads[item_id])
            return item_id

        return ID_RE.sub(replace, line)


def should_drop(line: str) -> bool:
    return "?logs=container" in line and "__sign=" in line


def main() -> None:
    name_cache = FacebookNameCache()
    for line in sys.stdin:
        if should_drop(line):
            continue
        sys.stdout.write(name_cache.rewrite_line(line))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
