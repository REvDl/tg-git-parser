import json
import os
import re
import subprocess
import sys
from datetime import datetime

# ---------- Settings ----------
JSON_PATH = "result.json"
OUTPUT_DIR = "history_codes"
MANIFEST_PATH = ".synced_msg_ids.json"
LONG_TEXT_THRESHOLD = 50          # chars; below this a single message is considered noise
CLUSTER_MIN_LEN = 500              # from how many chars a message is considered "part of a dump"
CLUSTER_MAX_GAP_SEC = 5            # max gap between messages of the same dump

GIT_AUTHOR_NAME = "Telegram Archive"
GIT_AUTHOR_EMAIL = "archive@localhost"

# ---------- Detector of file paths inside text ----------
KNOWN_EXT = (
    "py|js|ts|jsx|tsx|cpp|c|h|hpp|cs|java|go|rs|php|rb|sql|sh|bash|"
    "html|css|json|yml|yaml|md|txt|kt|swift|env|toml|ini|cfg"
)

# Style 1: "# ======\n# 📂 path/to/file.py\n# ======"
FANCY_MARKER_RE = re.compile(
    r'#\s*[=\-]{10,}\s*\n'
    r'#\s*(?:📂\s*)?([\w\-./]+\.(?:' + KNOWN_EXT + r'))\s*\n'
    r'#\s*[=\-]{10,}\s*\n?',
    re.M,
)

# Style 2: "main.py:" / "app/route.py:" on its own line
SIMPLE_MARKER_RE = re.compile(
    r'^([\w\-]+(?:/[\w\-]+)*\.(?:' + KNOWN_EXT + r'))\s*:\s*$',
    re.M,
)

# ---------- Heuristic "looks like code" (for messages without explicit markers) ----------
CODE_LINE_RE = re.compile(
    r'^\s*(def |class |import |from\s+\S+\s+import|return\b|if\s|elif\s|else\s*:|'
    r'for\s|while\s|try\s*:|except|with\s|async def|await\s|@\w+|print\(|'
    r'const\s|let\s|var\s|function\s*\(|#include|using namespace|public\s|private\s|'
    r'SELECT\s|INSERT\s|UPDATE\s|DELETE FROM|\}\s*$|\{\s*$)'
    r'|^\s*[\w\.\[\]]{2,}\s*[:=](?!\\)\s*.+'  # min 2 chars + no ":\" (blocks "C:\...")
    r'|^\s{2,}\S'
)

URL_LINE_RE = re.compile(r'^\s*[\w\-]+://\S+\s*$')

# Pasted terminal/console sessions (cmd.exe or shell prompts) look nothing like
# source code, but their prompts ("C:\Users\x>", "user@host:~$") happen to match
# the generic "key: value" heuristic below (a bare drive letter "C:" satisfies it),
# which used to inflate code_density and mislabel these as .py files.
TERMINAL_PROMPT_RE = re.compile(
    r'^[A-Za-z]:\\.*>|^\$\s|^[\w.\-]+@[\w.\-]+:.*[$#]\s*$',
    re.M,
)

LANG_TO_EXT = {
    "python": "py", "javascript": "js", "typescript": "ts", "cpp": "cpp",
    "c": "c", "bash": "sh", "shell": "sh", "sql": "sql", "html": "html",
    "css": "css", "json": "json", "java": "java", "go": "go", "rust": "rs",
    "php": "php", "ruby": "rb", "csharp": "cs", "kotlin": "kt", "swift": "swift",
}

KEYWORD_EXT_RULES = [
    ("cpp", re.compile(r'#include|using namespace|std::|#pragma\s|^\s*namespace\s+\w+\s*\{', re.M)),
    ("py", re.compile(r'\bdef \b|\bimport \b|\bself\.|print\(|\belif\b|\basync def\b')),
    ("js", re.compile(r'\bconst\s|\blet\s|=>|require\(|import .* from ["\']')),
    ("sql", re.compile(r'\bSELECT\b|\bINSERT INTO\b|\bCREATE TABLE\b', re.I)),
    ("sh", re.compile(r'^#!/bin/(ba)?sh|^\s*\$\s|\bsudo\b|\bapt-get\b')),
    ("html", re.compile(r'<html|<div|<!DOCTYPE', re.I)),
]


# ================= Basic message-reading utilities =================

def get_full_text(msg):
    return "".join(e.get("text", "") for e in msg.get("text_entities", []) or [])


def get_code_entities(msg):
    return [e for e in msg.get("text_entities", []) or [] if e.get("type") in ("code", "pre")]


def date_slug(unixtime):
    """Human/sortable timestamp used inside filenames, e.g. 2025-05-10_20-14-35."""
    return datetime.utcfromtimestamp(unixtime).strftime("%Y-%m-%d_%H-%M-%S")


def date_human(unixtime):
    """Human-readable date used in the final commit summary, e.g. 10.05.2025."""
    return datetime.utcfromtimestamp(unixtime).strftime("%d.%m.%Y")


# ================= Heuristic classification (fallback without markers) =================

def code_density(text):
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return 0.0
    content_lines = [l for l in lines if not URL_LINE_RE.match(l)]
    if not content_lines:
        return 0.0
    hits = sum(1 for l in content_lines if CODE_LINE_RE.match(l))
    return hits / len(content_lines)


def detect_extension(text, code_entities=None):
    code_entities = code_entities or []
    for e in code_entities:
        lang = (e.get("language") or "").strip().lower()
        if lang in LANG_TO_EXT:
            return LANG_TO_EXT[lang]
    for ext, rx in KEYWORD_EXT_RULES:
        if rx.search(text):
            return ext
    # No known language keywords matched: don't guess "py" by default, since
    # that mislabels plain text/logs. Plain text is a safer fallback.
    return "txt"


def looks_like_terminal_session(text):
    """A pasted console/terminal session (cmd.exe or shell prompts repeated
    several times) should never be classified as source code, even though its
    prompts can otherwise satisfy the generic code heuristics."""
    return len(TERMINAL_PROMPT_RE.findall(text)) >= 2


def classify(text, code_entities):
    if looks_like_terminal_session(text):
        return "log", "log"

    density = code_density(text)
    has_named_code_block = any(len(e.get("text", "")) > 20 for e in code_entities)
    if density >= 0.4 or (has_named_code_block and density >= 0.25):
        return "code", detect_extension(text, code_entities)
    elif density >= 0.15 or has_named_code_block:
        return "mixed", "md"
    else:
        return "note", "txt"


def extract_commit_summary(text, category):
    if category == "code":
        m = re.search(r'\b(def|class)\s+(\w+)', text)
        if m:
            return f"{m.group(1)} {m.group(2)}"
    first_line = text.strip().split("\n")[0].strip()
    return first_line[:60] if first_line else "sync entry"


# ================= Splitting by file path markers =================

def sanitize_path(raw_path):
    """Guards against path traversal and normalizes to a safe relative path."""
    parts = [p for p in raw_path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        return None
    return "/".join(parts)


def find_file_markers(text):
    """Finds both marker styles and returns a single list (start, end, path),
    sorted by position. On overlap, the 'fancy' style takes priority."""
    markers = []
    covered = []

    for m in FANCY_MARKER_RE.finditer(text):
        path = sanitize_path(m.group(1))
        if path:
            markers.append((m.start(), m.end(), path))
            covered.append((m.start(), m.end()))

    for m in SIMPLE_MARKER_RE.finditer(text):
        if any(m.start() >= s and m.start() < e for s, e in covered):
            continue
        path = sanitize_path(m.group(1))
        if path:
            markers.append((m.start(), m.end(), path))

    markers.sort(key=lambda t: t[0])
    return markers


def split_by_markers(text):
    """Returns (preamble, [(path, content), ...]) or (text, []) if no markers found."""
    markers = find_file_markers(text)
    if not markers:
        return text, []

    preamble = text[:markers[0][0]]
    segments = []
    for i, (start, end, path) in enumerate(markers):
        content_end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        content = text[end:content_end].strip("\n")
        if content.strip():
            segments.append((path, content))
    return preamble, segments


# ================= Git utilities =================

def run(cmd, env=None, check=True):
    return subprocess.run(
        cmd, env=env, check=check,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def ensure_git_identity():
    for key, default in (("user.name", GIT_AUTHOR_NAME), ("user.email", GIT_AUTHOR_EMAIL)):
        res = subprocess.run(["git", "config", key], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if not res.stdout.strip():
            subprocess.run(["git", "config", key, default], check=True)
            print(f"git config {key} was not set, applied temporary value: {default}")


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_manifest(done_ids):
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(done_ids), f)


def commit_files(file_paths, git_date, commit_message, done_ids, msg_ids):
    """git add + git commit with a spoofed date. Returns True if a commit was created."""
    for fp in file_paths:
        run(["git", "add", fp])

    diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff_check.returncode == 0:
        done_ids.update(msg_ids)
        return False

    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = git_date
    env["GIT_COMMITTER_DATE"] = git_date
    env["GIT_AUTHOR_NAME"] = GIT_AUTHOR_NAME
    env["GIT_AUTHOR_EMAIL"] = GIT_AUTHOR_EMAIL

    run(["git", "commit", "-m", commit_message, "--date", git_date], env=env)
    done_ids.update(msg_ids)
    return True


# ================= Grouping messages into "units" =================

def build_units(messages):
    """Splits sorted messages into units: single messages stay as-is;
    consecutive long (>=CLUSTER_MIN_LEN) messages with a gap <=CLUSTER_MAX_GAP_SEC
    seconds are merged into one unit (this is likely a single Ctrl+V paste
    that Telegram cut up at its 4096-char limit)."""
    units = []
    current = []

    def flush():
        if current:
            units.append(list(current))
            current.clear()

    prev_len_ok = False
    for msg in messages:
        text = get_full_text(msg)
        is_long = len(text) >= CLUSTER_MIN_LEN
        if current and is_long and prev_len_ok and (
            msg["date_unixtime"] - current[-1]["date_unixtime"] <= CLUSTER_MAX_GAP_SEC
        ):
            current.append(msg)
        else:
            flush()
            current = [msg]
        prev_len_ok = is_long
    flush()
    return units


# ================= Main loop =================

def parse_and_commit(json_path=JSON_PATH):
    if not os.path.exists(json_path):
        print(f"Error: file {json_path} not found!")
        return

    print("Reading and parsing json...")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = [m for m in data.get("messages", []) if m.get("type") == "message"]
    if not messages:
        print("No messages found in the file.")
        return

    for m in messages:
        m["date_unixtime"] = int(m["date_unixtime"])
    messages.sort(key=lambda m: m["date_unixtime"])

    if not os.path.exists(".git"):
        run(["git", "init"])
        print("Initialized a new Git repository.")

    ensure_git_identity()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    done_ids = load_manifest()

    units = build_units(messages)

    stats = {"project_files": 0, "merged_dumps": 0, "code": 0, "mixed": 0, "note": 0,
              "log": 0, "skipped": 0, "errors": 0}
    commit_count = 0
    commit_log = []  # list of (human_date, display_name) for the final summary

    for unit in units:
        unit_ids = [m["id"] for m in unit]
        if all(i in done_ids for i in unit_ids):
            continue

        unit_text = "".join(get_full_text(m) for m in unit)
        first_unixtime = unit[0]["date_unixtime"]
        git_date = f"{first_unixtime} +0000"
        human_date = date_human(first_unixtime)

        try:
            preamble, segments = split_by_markers(unit_text)

            if segments:
                # ---- Real file paths found: write as an actual project ----
                written = []
                for path, content in segments:
                    dest = os.path.join(OUTPUT_DIR, path)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(content)
                    written.append(dest)
                    stats["project_files"] += 1

                names = ", ".join(p for p, _ in segments[:5])
                more = "" if len(segments) <= 5 else f" (+{len(segments) - 5})"
                commit_message = f"[project] {names}{more}"
                if commit_files(written, git_date, commit_message, done_ids, unit_ids):
                    commit_count += 1
                    commit_log.append((human_date, f"{names}{more}"))

            elif len(unit) > 1:
                # ---- Telegram-split dump with no explicit paths: merge into one file ----
                category, ext = classify(unit_text, [])
                first_id, last_id = unit[0]["id"], unit[-1]["id"]
                fname = f"dump_{date_slug(first_unixtime)}_{first_id}-{last_id}.{ext}"
                dest = os.path.join(OUTPUT_DIR, fname)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(unit_text)
                stats["merged_dumps"] += 1
                commit_message = f"[merged {len(unit)} msgs] dump {first_id}-{last_id} ({human_date})"
                if commit_files([dest], git_date, commit_message, done_ids, unit_ids):
                    commit_count += 1
                    commit_log.append((human_date, fname))

            else:
                # ---- Regular single message ----
                msg = unit[0]
                text = unit_text
                code_entities = get_code_entities(msg)
                if len(text.strip()) <= LONG_TEXT_THRESHOLD and not any(
                    len(e.get("text", "")) > LONG_TEXT_THRESHOLD for e in code_entities
                ):
                    stats["skipped"] += 1
                    done_ids.update(unit_ids)
                    continue

                category, ext = classify(text, code_entities)
                stats[category] += 1
                fname = f"msg_{date_slug(msg['date_unixtime'])}_id{msg['id']}.{ext}"
                dest = os.path.join(OUTPUT_DIR, fname)

                if category == "mixed":
                    content = f"<!-- Telegram msg_id={msg['id']}, date={msg.get('date')} -->\n\n{text}"
                elif category == "note":
                    content = f"# Note from channel ({msg.get('date')}):\n\n{text}"
                elif category == "log":
                    content = f"# Terminal session from channel ({msg.get('date')}):\n\n{text}"
                else:
                    content = text

                with open(dest, "w", encoding="utf-8") as f:
                    f.write(content)

                summary = extract_commit_summary(text, category)
                prefix = {"code": "code", "mixed": "task+code", "note": "note", "log": "log"}[category]
                commit_message = f"[{prefix}] msg {msg['id']} ({human_date}): {summary}"
                if commit_files([dest], git_date, commit_message, done_ids, unit_ids):
                    commit_count += 1
                    commit_log.append((human_date, fname))

            if commit_count and commit_count % 100 == 0:
                print(f"Commits created so far: {commit_count}")
                save_manifest(done_ids)

        except subprocess.CalledProcessError as e:
            stats["errors"] += 1
            print(f"[ERROR] unit_ids={unit_ids}: {e}", file=sys.stderr)
            continue

    save_manifest(done_ids)

    print(
        f"\nDone! Commits created: {commit_count}\n"
        f"  real project files (matched by path markers): {stats['project_files']}\n"
        f"  merged dumps without markers (multi-msg): {stats['merged_dumps']}\n"
        f"  single code messages (.py/.js/...): {stats['code']}\n"
        f"  mixed (task+code, .md): {stats['mixed']}\n"
        f"  notes (.txt): {stats['note']}\n"
        f"  terminal/console sessions (.log): {stats['log']}\n"
        f"  skipped (short/empty): {stats['skipped']}\n"
        f"  errors: {stats['errors']}\n"
        f"\nNext: git remote add origin <url> && git push -u origin main"
    )

    if commit_log:
        answer = input(
            f"\nPrint all {len(commit_log)} commit names with their dates? (Y/n): "
        ).strip().lower()
        if answer in ("", "y", "yes"):
            print()
            for human_date, name in commit_log:
                print(f"{human_date}: {name}")


if __name__ == "__main__":
    parse_and_commit()