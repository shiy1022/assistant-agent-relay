"""The study page's storage: a participant's interview, chats, ratings and comparisons.

The console serves bench/study.html at /study. It walks one participant through a
task day: the pre-task interview (day 1), each agent's chat, the questions after
each agent, the blind comparison, the day's extra questions and the agents'
process reports. Every answer it collects is saved here, under
data/study/<participant>/ (data/ is never committed), in the layout of the
task's data spec:

    P001/
      session.json  history.jsonl  progress.json
      scenario/card.json  interview.md  interview.json  changes.jsonl
      day1/<agent>/conversation.jsonl  conversation.md  chat-notes.json  after-agent.json
      day1/comparison.json  what-you-would-do.json
      day2/<agent>/…  learning-check.json
      audio/…                          each spoken answer, as recorded

Every JSON file carries schema_version "1", and every change to a value in one
is also appended to history.jsonl as {at, file, variable, before, after, by},
so a changed answer can be traced. progress.json is the page's own state, for
picking up where it left off; it isn't in history.

The questions themselves are private (the agents must not see them), so they
aren't in this public repository: the page reads them from the task's folder,
tasks/<folder>/study.json, which tasks/ keeps out of git.
"""

import base64
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from bench import taskview
from bench.config import ROOT

STUDY_DIR = ROOT / "data" / "study"
SCHEMA_VERSION = "1"
MAX_AUDIO = 20_000_000  # bytes in one voice answer

PARTICIPANT = re.compile(r"P\d{3,4}")
AGENT = r"[a-z][a-z0-9_-]{0,31}"
JSON_FILES = re.compile(
    r"session\.json|progress\.json"
    r"|scenario/(card|interview)\.json"
    r"|day[12]/comparison\.json|day1/what-you-would-do\.json"
    rf"|day[12]/{AGENT}/(after-agent|chat-notes)\.json|day2/{AGENT}/learning-check\.json")
TEXT_FILES = re.compile(rf"scenario/interview\.md|day[12]/{AGENT}/conversation\.(md|jsonl)")
APPEND_FILES = re.compile(r"scenario/changes\.jsonl")
NO_HISTORY = {"progress.json"}
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav"}


AUDIO_OBJECT = re.compile(r"[A-Za-z0-9_.-]{1,80}-[a-f0-9]{32}\.(webm|ogg|m4a|mp3|wav)")


class StudyError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def participant_dir(pid: str, study_dir: Path = STUDY_DIR) -> Path:
    if not isinstance(pid, str) or not PARTICIPANT.fullmatch(pid):
        raise StudyError("the participant ID looks like P001")
    return study_dir / pid


def _write(path: Path, text: str) -> None:
    """Writes the whole file at once, so a crash never leaves half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _flatten(value, prefix: str = "") -> dict:
    """{"a": {"b": 1}} -> {"a.b": 1}; lists are kept whole, as one value."""
    if isinstance(value, dict) and value:
        out = {}
        for k, v in value.items():
            out |= _flatten(v, f"{prefix}.{k}" if prefix else str(k))
        return out
    return {prefix: value}


def _append_history(pid: str, lines: list[dict], study_dir: Path) -> None:
    if not lines:
        return
    path = participant_dir(pid, study_dir) / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def result_payload(body: dict) -> tuple[str, dict | str | list]:
    """Validate a study file and normalize it for the local or hosted store."""
    path = body.get("path")
    if "data" in body:
        pattern, data = JSON_FILES, body["data"]
        if not isinstance(data, dict):
            raise StudyError("data must be a JSON object")
        content = data | {"schema_version": SCHEMA_VERSION}
    elif "text" in body:
        pattern, content = TEXT_FILES, body["text"]
        if not isinstance(content, str):
            raise StudyError("text must be a string")
    elif "append" in body:
        pattern, content = APPEND_FILES, body["append"]
        if not isinstance(content, list) or not all(isinstance(r, dict) for r in content):
            raise StudyError("append must be a list of JSON objects")
    else:
        raise StudyError("send data, text or append")
    if not isinstance(path, str) or not pattern.fullmatch(path):
        raise StudyError("This is not one of the study's files.")
    if pattern is TEXT_FILES and path.endswith(".jsonl"):
        for n, line in enumerate(content.splitlines(), 1):
            if line.strip():
                try:
                    json.loads(line)
                except ValueError:
                    raise StudyError(f"line {n} of {path} isn't JSON") from None
    return path, content


def save(body: dict, study_dir: Path = STUDY_DIR) -> dict:
    """Saves one file for a participant: {pid, path, data} for JSON, {pid, path, text} for Markdown
    and JSONL, {pid, path, append: [records]} for changes.jsonl. Returns {path, changed}."""
    pid, by = body.get("pid"), str(body.get("by") or "participant")[:40]
    path, content = result_payload(body)
    target = participant_dir(pid, study_dir) / path
    at = now_iso()
    if "data" in body:
        data = content
        before = {}
        if target.exists():
            try:
                before = json.loads(target.read_text())
            except ValueError:
                before = {}
        old, new = (_flatten(x) if x else {} for x in (before, data))
        changes = [{"at": at, "file": path, "variable": k, "before": old.get(k), "after": new.get(k), "by": by}
                   for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k) and k != "schema_version"]
        if not changes and target.exists():
            return {"path": path, "changed": 0}
        _write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if path not in NO_HISTORY:
            _append_history(pid, changes, study_dir)
        return {"path": path, "changed": len(changes)}
    if "text" in body:
        text = content
        old = target.read_text() if target.exists() else None
        if old == text:
            return {"path": path, "changed": 0}
        _write(target, text)
        _append_history(pid, [{"at": at, "file": path, "variable": None,
                               "before": None if old is None else f"{len(old)} characters",
                               "after": f"{len(text)} characters", "by": by}], study_dir)
        return {"path": path, "changed": 1}
    if "append" in body:
        records = content
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps({"at": at} | r, ensure_ascii=False) + "\n")
        _append_history(pid, [{"at": r.get("at", at), "file": path, "variable": r.get("field"),
                               "before": r.get("before"), "after": r.get("after"), "by": by} for r in records], study_dir)
        return {"path": path, "changed": len(records)}
    raise StudyError("send data, text or append")


def audio_payload(body: dict) -> tuple[str, str, bytes]:
    name = str(body.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise StudyError("name the recording with letters, digits, dots, dashes or underscores")
    mime = str(body.get("mime") or "").split(";")[0].strip()
    if mime not in AUDIO_TYPES:
        raise StudyError(f"can't keep audio of type {mime!r}")
    try:
        raw = base64.b64decode(str(body.get("b64") or ""), validate=True)
    except ValueError:
        raise StudyError("the recording isn't base64") from None
    if not raw:
        raise StudyError("the recording is empty")
    if len(raw) > MAX_AUDIO:
        raise StudyError("the recording is too long; save it in parts")
    return name, mime, raw


def save_audio(body: dict, study_dir: Path = STUDY_DIR) -> dict:
    """Saves one recorded voice answer: {pid, name, mime, b64}. Returns {path}."""
    folder = participant_dir(body.get("pid"), study_dir) / "audio"
    name, mime, raw = audio_payload(body)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = folder / f"{name}-{stamp}{AUDIO_TYPES[mime]}"
    target.write_bytes(raw)
    rel = str(target.relative_to(participant_dir(body.get("pid"), study_dir)))
    _append_history(body["pid"], [{"at": now_iso(), "file": rel, "variable": None, "before": None,
                                   "after": f"{len(raw)} bytes of {mime}", "by": "participant"}], study_dir)
    return {"path": rel}


def load(pid: str, study_dir: Path = STUDY_DIR) -> dict:
    """Everything saved for a participant: {files: {path: JSON or text}}."""
    folder = participant_dir(pid, study_dir)
    files = {}
    if folder.is_dir():
        for p in sorted(folder.rglob("*")):
            rel = str(p.relative_to(folder))
            if not p.is_file():
                continue
            if JSON_FILES.fullmatch(rel):
                try:
                    files[rel] = json.loads(p.read_text())
                except ValueError:
                    continue
            elif TEXT_FILES.fullmatch(rel) or APPEND_FILES.fullmatch(rel):
                files[rel] = p.read_text()
    return {"pid": pid, "files": files}


def participants(study_dir: Path = STUDY_DIR) -> list[dict]:
    """Each participant with a folder, newest first: {pid, updated, day, stage}."""
    out = []
    if not study_dir.is_dir():
        return out
    for folder in study_dir.iterdir():
        if not (folder.is_dir() and PARTICIPANT.fullmatch(folder.name)):
            continue
        progress = {}
        try:
            progress = json.loads((folder / "progress.json").read_text())
        except (OSError, ValueError):
            pass
        stamp = max((p.stat().st_mtime for p in folder.rglob("*") if p.is_file()), default=folder.stat().st_mtime)
        out.append({"pid": folder.name, "updated": datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds"),
                    "day": progress.get("day"), "stage": progress.get("stage"), "task": progress.get("task")})
    return sorted(out, key=lambda p: p["updated"], reverse=True)


def spec(task: str, tasks_dir: Path = taskview.TASKS_DIR) -> dict:
    """The task's questions and messages, from tasks/<folder>/study.json."""
    if not isinstance(task, str) or not task.strip():
        raise StudyError("name the task, like Restaurant")
    folder = taskview.task_folder(task, tasks_dir)
    path = folder / "study.json" if folder else None
    if not path or not path.is_file():
        raise StudyError(f"no study for {task!r}: put its questions in tasks/<folder>/study.json, "
                         f"next to a run.json that says {{\"task\": \"{task}\"}}")
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise StudyError(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path} isn't valid JSON: {e}") from None
    return data


def studies(tasks_dir: Path = taskview.TASKS_DIR) -> list[str]:
    """The tasks that have a study.json."""
    out = []
    for path in sorted(tasks_dir.glob("*/study.json")):
        try:
            out.append(json.loads((path.parent / "run.json").read_text())["task"])
        except (OSError, ValueError, KeyError):
            continue
    return out
