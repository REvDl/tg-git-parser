# Telegram Archive → Git

This script turns a Telegram chat/channel export (`result.json`, from the Telegram Desktop "Export chat history" feature) into a git repository with commits. Every message (or group of messages) becomes a file and a commit dated to match the message's original send date in Telegram.

Useful if you've been dumping code, configs, and notes into a private Telegram chat/channel for years instead of a proper repository, and now want to turn that mess into a git history that preserves the original timeline.

## What the script does

1. Reads `result.json` from the Telegram export.
2. Sorts messages by time.
3. Groups adjacent "long" messages (≥500 characters) sent within 5 seconds of each other into a single block — this compensates for Telegram Desktop sometimes splitting one long text into multiple messages on send.
4. For each block, determines what it is:
   - **Project files** — if the text contains path markers (see below), the content is split by file and committed with real file names/paths.
   - **Merged dump** — a group of several messages with no path markers; everything goes into one dump file.
   - **Single message** — classified as `code`, `mixed` (text + a bit of code, `.md`), `note` (plain text), or `log` (looks like terminal output).
5. Writes the file to `history_codes/` and runs `git commit` with the date taken from Telegram (`--date`, `GIT_AUTHOR_DATE`, `GIT_COMMITTER_DATE`).
6. Keeps a manifest of already-processed messages (`.synced_msg_ids.json`), so the script can be re-run safely — previously processed messages won't be duplicated.

## Getting `result.json`

Telegram Desktop → the chat/channel you need → menu (three dots) → **Export chat history** → choose **JSON** format, make sure message text is included. Place the resulting `result.json` next to the script.

## Running

```bash
python parser.py
```

Before running, make sure git identity is configured (otherwise commits will use the fallback name "Telegram Archive", see below):

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

The script initializes the repository itself (`git init`) if the folder isn't a git repo yet.

At the end, it prints a summary with commit counts and offers to print the full list of commits with their dates.

## Detecting files inside a message (path markers)

If a message's text contains something like:

```python
# ==========================
# 📂 src/utils/helpers.py
# ==========================
```

or simply:

```
src/utils/helpers.py:
```

— the script recognizes it as the start of a new file and splits the message content along these markers, rebuilding the directory structure inside `history_codes/`. Supported extensions are listed in `KNOWN_EXT` (py, js, ts, cpp, java, go, rs, php, html, css, json, yml, md, sql, etc.).

## Classifying single messages

For messages without explicit path markers, a heuristic based on the density of "code-like" lines is used (`code_density`):

| Category | Condition | Extension |
|---|---|---|
| `log` | ≥2 lines resemble a terminal prompt (`$`, `C:\>`, `user@host:~$`) | `.log` |
| `code` | high density of code-like lines | determined by the Telegram code block's language or keyword matching (py/js/sql/sh/html/cpp), otherwise `.txt` |
| `mixed` | medium density / a code block is present | `.md` |
| `note` | everything else | `.txt` |

Messages shorter than `LONG_TEXT_THRESHOLD` (50 characters, with no long code blocks) are skipped — treated as "noise" short replies and not committed.

## Commit message format

```
[code] msg 1502 (16.07.2026)
[mixed] msg 1503 (16.07.2026)
[note] msg 1504 (16.07.2026)
[log] msg 1505 (16.07.2026)
[merged 4 msgs] 1497-1501 (16.07.2026)
[project] src/main.py, src/utils.py (+2) (16.07.2026)
```

The original message text from Telegram is never included in the commit message — only the type, id, and date. The full message content is preserved in the file itself.

## Commit author

By default, `git commit` uses your regular `git config user.name` / `user.email`. The `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` constants ("Telegram Archive" / "archive@localhost") are a **fallback** applied only if git has no identity configured at all (neither globally nor locally), so that `git commit` doesn't fail with "Please tell me who you are".

## Commit dates

The commit date is the `date_unixtime` of the first message in the block, converted to UTC. It's applied as both the author date and the committer date, so `git log` and GitHub will lay the commits out in the actual chronological order of the conversation, not by the date you happened to run the script.

## Re-running / syncing new messages

If you re-run the script on an updated `result.json` (e.g. you re-exported the chat after new messages came in), previously committed messages are left untouched — their ids are stored in `.synced_msg_ids.json`. Only new messages will be processed and committed.

## Known limitations

- The script doesn't rewrite existing commits — if you want to change the message format retroactively, you'll need `git filter-branch` / `git filter-repo` separately.
- Classification (`code` / `mixed` / `note` / `log`) is heuristic, regex-based, and can misfire on unusual text.
- Path markers are only recognized for extensions listed in `KNOWN_EXT` — files with other extensions won't be picked up as separate project files.
- Commit dates are always set in UTC, regardless of the sender's local timezone in Telegram.

## Files the script creates

- `history_codes/` — all recovered files/dumps/notes.
- `.synced_msg_ids.json` — manifest of processed message ids (don't delete it if you want to sync new messages without duplicates).

## Next steps

```bash
git remote add origin <url>
git push -u origin main
```