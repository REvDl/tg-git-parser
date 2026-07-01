import json
import os
import re
import subprocess
import sys

# ---------- Настройки ----------
JSON_PATH = "result.json"
OUTPUT_DIR = "history_codes"
MANIFEST_PATH = ".synced_msg_ids.json"
LONG_TEXT_THRESHOLD = 50          # символов, ниже — считаем шумом (для одиночных сообщений)
CLUSTER_MIN_LEN = 500              # от скольки символов сообщение считается "частью дампа"
CLUSTER_MAX_GAP_SEC = 5            # макс. разрыв между сообщениями одного дампа

GIT_AUTHOR_NAME = "Telegram Archive"
GIT_AUTHOR_EMAIL = "archive@localhost"

# ---------- Детектор путей файлов внутри текста ----------
KNOWN_EXT = (
    "py|js|ts|jsx|tsx|cpp|c|h|hpp|cs|java|go|rs|php|rb|sql|sh|bash|"
    "html|css|json|yml|yaml|md|txt|kt|swift|env|toml|ini|cfg"
)

# Стиль 1: "# ======\n# 📂 path/to/file.py\n# ======"
FANCY_MARKER_RE = re.compile(
    r'#\s*[=\-]{10,}\s*\n'
    r'#\s*(?:📂\s*)?([\w\-./]+\.(?:' + KNOWN_EXT + r'))\s*\n'
    r'#\s*[=\-]{10,}\s*\n?',
    re.M,
)

# Стиль 2: "main.py:" / "app/route.py:" одна строка сама по себе
SIMPLE_MARKER_RE = re.compile(
    r'^([\w\-]+(?:/[\w\-]+)*\.(?:' + KNOWN_EXT + r'))\s*:\s*$',
    re.M,
)

# ---------- Эвристика "похоже на код" (для сообщений без явных маркеров) ----------
CODE_LINE_RE = re.compile(
    r'^\s*(def |class |import |from\s+\S+\s+import|return\b|if\s|elif\s|else\s*:|'
    r'for\s|while\s|try\s*:|except|with\s|async def|await\s|@\w+|print\(|'
    r'const\s|let\s|var\s|function\s*\(|#include|using namespace|public\s|private\s|'
    r'SELECT\s|INSERT\s|UPDATE\s|DELETE FROM|\}\s*$|\{\s*$)'
    r'|^\s*[\w\.\[\]]+\s*[:=]\s*.+'
    r'|^\s{2,}\S'
)

URL_LINE_RE = re.compile(r'^\s*[\w\-]+://\S+\s*$')

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


# ================= Базовые утилиты чтения сообщений =================

def get_full_text(msg):
    return "".join(e.get("text", "") for e in msg.get("text_entities", []) or [])


def get_code_entities(msg):
    return [e for e in msg.get("text_entities", []) or [] if e.get("type") in ("code", "pre")]


# ================= Эвристика классификации (fallback без маркеров) =================

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
    return "py"


def classify(text, code_entities):
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


# ================= Разбиение по маркерам путей =================

def sanitize_path(raw_path):
    """Защита от path traversal и приведение к безопасному относительному пути."""
    parts = [p for p in raw_path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        return None
    return "/".join(parts)


def find_file_markers(text):
    """Ищет оба стиля маркеров и возвращает единый список (start, end, path),
    отсортированный по позиции. При пересечении приоритет отдаётся 'fancy' стилю."""
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
    """Возвращает (preamble, [(path, content), ...]) либо (text, []) если маркеров нет."""
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


# ================= Git-утилиты =================

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
            print(f"git config {key} не был задан, установлен временный: {default}")


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_manifest(done_ids):
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(done_ids), f)


def commit_files(file_paths, git_date, commit_message, done_ids, msg_ids):
    """git add + git commit с подменой даты. Возвращает True если коммит создан."""
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


# ================= Группировка сообщений в "юниты" =================

def build_units(messages):
    """Разбивает отсортированные сообщения на юниты: одиночные сообщения
    остаются как есть, подряд идущие длинные (>=CLUSTER_MIN_LEN) сообщения
    с разрывом <=CLUSTER_MAX_GAP_SEC сек объединяются в один юнит (это,
    вероятно, один Ctrl+V, разрезанный Telegram по лимиту в 4096 символов)."""
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


# ================= Основной цикл =================

def parse_and_commit(json_path=JSON_PATH):
    if not os.path.exists(json_path):
        print(f"Ошибка: Файл {json_path} не найден!")
        return

    print("Читаем и парсим json...")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = [m for m in data.get("messages", []) if m.get("type") == "message"]
    if not messages:
        print("В файле не найдено сообщений.")
        return

    for m in messages:
        m["date_unixtime"] = int(m["date_unixtime"])
    messages.sort(key=lambda m: m["date_unixtime"])

    if not os.path.exists(".git"):
        run(["git", "init"])
        print("Инициализирован новый Git-репозиторий.")

    ensure_git_identity()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    done_ids = load_manifest()

    units = build_units(messages)

    stats = {"project_files": 0, "merged_dumps": 0, "code": 0, "mixed": 0, "note": 0,
              "skipped": 0, "errors": 0}
    commit_count = 0

    for unit in units:
        unit_ids = [m["id"] for m in unit]
        if all(i in done_ids for i in unit_ids):
            continue

        unit_text = "".join(get_full_text(m) for m in unit)
        git_date = f"{unit[0]['date_unixtime']} +0000"

        try:
            preamble, segments = split_by_markers(unit_text)

            if segments:
                # ---- Найдены реальные пути файлов: пишем как настоящий проект ----
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

            elif len(unit) > 1:
                # ---- Разбитый Telegram-ом дамп без явных путей: склеиваем в один файл ----
                category, ext = classify(unit_text, [])
                first_id, last_id = unit[0]["id"], unit[-1]["id"]
                fname = f"dump_{first_id}-{last_id}.{ext}"
                dest = os.path.join(OUTPUT_DIR, fname)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(unit_text)
                stats["merged_dumps"] += 1
                commit_message = f"[merged {len(unit)} msgs] dump {first_id}-{last_id}"
                if commit_files([dest], git_date, commit_message, done_ids, unit_ids):
                    commit_count += 1

            else:
                # ---- Обычное одиночное сообщение ----
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
                fname = f"msg_{msg['id']}.{ext}"
                dest = os.path.join(OUTPUT_DIR, fname)

                if category == "mixed":
                    content = f"<!-- Telegram msg_id={msg['id']}, date={msg.get('date')} -->\n\n{text}"
                elif category == "note":
                    content = f"# Запись из канала ({msg.get('date')}):\n\n{text}"
                else:
                    content = text

                with open(dest, "w", encoding="utf-8") as f:
                    f.write(content)

                summary = extract_commit_summary(text, category)
                prefix = {"code": "code", "mixed": "task+code", "note": "note"}[category]
                commit_message = f"[{prefix}] msg {msg['id']}: {summary}"
                if commit_files([dest], git_date, commit_message, done_ids, unit_ids):
                    commit_count += 1

            if commit_count and commit_count % 100 == 0:
                print(f"Успешно создано коммитов: {commit_count}")
                save_manifest(done_ids)

        except subprocess.CalledProcessError as e:
            stats["errors"] += 1
            print(f"[ОШИБКА] unit_ids={unit_ids}: {e}", file=sys.stderr)
            continue

    save_manifest(done_ids)

    print(
        f"\nГотово! Коммитов создано: {commit_count}\n"
        f"  файлов реального проекта (по маркерам путей): {stats['project_files']}\n"
        f"  склеенных дампов без маркеров (multi-msg): {stats['merged_dumps']}\n"
        f"  одиночный код (.py/.js/...): {stats['code']}\n"
        f"  смешанное (задание+код, .md): {stats['mixed']}\n"
        f"  заметки (.txt): {stats['note']}\n"
        f"  пропущено (короткие/пустые): {stats['skipped']}\n"
        f"  ошибок: {stats['errors']}\n"
        f"\nТеперь: git remote add origin <url> && git push -u origin main"
    )


if __name__ == "__main__":
    parse_and_commit()