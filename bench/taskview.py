"""What the console's task view shows besides the conversations.

A task day is every run of one task (run.json's "task") that started on one
local date: tonight's Housewarming test run is "Housewarming draft" on
2026-11-07. For it the console shows:

- its steps, in the order they were first run, with each agent's runs;
- each step's rubric, from the task sheet: the tab named after the task, in
  one of tasks/*.xlsx, row by the step's name in column A (or the run's cell);
- the task's own files, if tasks/<folder>/run.json names the task: what was
  planted (plants.json, calendar.json);
- the calendar events created that day, from data/calendar/<date>.json, which
  Claude writes with its Calendar connector (the console can't reach Google
  Calendar itself);
- your rankings of the agents' replies, kept in data/grades/<task>/<date>/<step>.json.

Email is in bench.mailview.
"""

import json
import os
import re
import tempfile
import zipfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from xml.etree import ElementTree

from bench.config import ROOT

TASKS_DIR = ROOT / "tasks"
GRADES_DIR = ROOT / "data" / "grades"  # data/ is never committed
CALENDAR_DIR = ROOT / "data" / "calendar"

# Columns of a task sheet, found by how their header starts (case doesn't matter)
COLUMNS = {
    "phase": "day / phase",
    "prompt": "exact prompts",
    "success": "success criteria",
    "functional": "functional grading",
    "subjective": "checkpoint subjective",
}


class TaskViewError(Exception):
    pass


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "untitled"


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """The day's start and the next day's start, in this Mac's time zone."""
    start = datetime.combine(day, time()).astimezone()
    return start, datetime.combine(day + timedelta(days=1), time()).astimezone()


# MARK: Task days, from runs/

def _runs(runs_dir: Path) -> list[dict]:
    out = []
    for path in sorted(runs_dir.glob("*/run.json")):
        try:
            info = json.loads(path.read_text())
            started = parse(info["started_at"]) if info.get("started_at") else \
                datetime.strptime(path.parent.name[:15], "%Y%m%d-%H%M%S").astimezone()
        except (ValueError, OSError, KeyError):
            continue
        if not info.get("task") or not info.get("agent"):
            continue
        out.append({"run": path.parent.name, "task": info["task"], "step": info.get("step") or "",
                    "cell": info.get("cell") or "", "agent": info["agent"], "channel": info.get("channel") or "",
                    "started": started, "ended": parse(info["ended_at"]) if info.get("ended_at") else None,
                    "finished": info.get("finished") or ""})
    return out


def sessions(runs_dir: Path) -> list[dict]:
    """Every task day, newest first: its steps in the order they were first run, and each agent's runs."""
    days: dict[tuple[str, str], list[dict]] = {}
    for r in _runs(runs_dir):
        days.setdefault((r["task"], r["started"].astimezone().date().isoformat()), []).append(r)
    out = []
    for (task, day), runs in days.items():
        runs.sort(key=lambda r: r["started"])
        _, day_end = local_day_bounds(date.fromisoformat(day))
        steps: dict[str, dict] = {}
        agents: dict[str, dict] = {}
        for r in runs:
            step = steps.setdefault(r["step"], {"step": r["step"], "cell": r["cell"], "first_run": iso(r["started"]), "runs": []})
            step["runs"].append({"run": r["run"], "agent": r["agent"], "started_at": iso(r["started"]),
                                 "ended_at": iso(r["ended"]) if r["ended"] else None, "finished": r["finished"]})
            a = agents.setdefault(r["agent"], {"name": r["agent"], "channel": r["channel"], "from": iso(r["started"])})
            a["channel"] = r["channel"] or a["channel"]  # the channel of its latest run
        ends = [r["ended"] for r in runs if r["ended"]]
        out.append({
            "key": f"{task}|{day}", "task": task, "date": day,
            "start": iso(runs[0]["started"]),
            "end": iso(max([day_end, *ends])),  # the rest of the day, or later if a run went past midnight
            "steps": list(steps.values()), "agents": list(agents.values()), "runs": len(runs),
        })
    return sorted(out, key=lambda s: s["start"], reverse=True)


def find_session(runs_dir: Path, task: str, day: str) -> dict:
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise TaskViewError("that's not a date") from None
    for s in sessions(runs_dir):
        if s["task"] == task and s["date"] == day:
            return s
    raise TaskViewError(f"no runs of {task!r} on {day}")


# MARK: The task sheet

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
_sheet_cache: dict[tuple[str, str, int], dict[int, dict[str, str]] | None] = {}


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(f"{{{_NS['m']}}}t"))


def read_sheet(path: Path, tab: str) -> dict[int, dict[str, str]] | None:
    """One tab of an .xlsx, as {row number: {column letter: text}}, or None if it has no such tab.
    Read with the standard library: shared and inline strings, numbers as written."""
    key = (str(path), tab, path.stat().st_mtime_ns)
    if key in _sheet_cache:
        return _sheet_cache[key]
    with zipfile.ZipFile(path) as z:
        book = ElementTree.fromstring(z.read("xl/workbook.xml"))
        sheet = next((s for s in book.iterfind("m:sheets/m:sheet", _NS) if s.get("name") == tab), None)
        if sheet is None:
            _sheet_cache[key] = None
            return None
        rid = sheet.get(f"{{{_NS['r']}}}id")
        rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels.iterfind("rel:Relationship", _NS) if r.get("Id") == rid)
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            shared = [_text(si) for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).iterfind("m:si", _NS)]
        rows: dict[int, dict[str, str]] = {}
        for c in ElementTree.fromstring(z.read(target)).iter(f"{{{_NS['m']}}}c"):
            ref = c.get("r") or ""
            m = re.fullmatch(r"([A-Z]+)(\d+)", ref)
            if not m:
                continue
            kind, v = c.get("t"), c.find("m:v", _NS)
            if kind == "s" and v is not None:
                value = shared[int(v.text)]
            elif kind == "inlineStr":
                value = _text(c)
            else:
                value = v.text if v is not None and v.text else ""
            if value:
                rows.setdefault(int(m.group(2)), {})[m.group(1)] = value
    _sheet_cache[key] = rows
    return rows


def find_sheet(task: str, tasks_dir: Path = TASKS_DIR) -> tuple[Path, dict[int, dict[str, str]]] | None:
    """The task's tab, in the newest workbook in tasks/ that has a tab named after it."""
    books = sorted(tasks_dir.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in books:
        if path.name.startswith("~$"):  # Excel's lock file
            continue
        try:
            rows = read_sheet(path, task)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, StopIteration, OSError):
            continue
        if rows is not None:
            return path, rows
    return None


def rubrics(task: str, steps: list[dict], tasks_dir: Path = TASKS_DIR) -> dict:
    """Each step's row of the task sheet: its prompt, success criteria and grading columns."""
    found = find_sheet(task, tasks_dir)
    if not found:
        return {"sheet": None, "steps": {}}
    path, rows = found
    header = rows.get(1, {})
    columns = {}
    for key, start in COLUMNS.items():
        col = next((c for c, h in sorted(header.items(), key=lambda kv: (len(kv[0]), kv[0]))
                    if h.strip().lower().startswith(start)), None)
        if col:
            columns[key] = col
    phase = columns.get("phase", "A")
    out = {}
    for s in steps:
        name = s["step"].strip().lower()
        row = next((n for n, cells in sorted(rows.items())
                    if n > 1 and (cells.get(phase) or "").split("\n")[0].strip().lower() == name), None)
        if row is None and (m := re.fullmatch(r"[A-Z]+(\d+)", s.get("cell") or "")):
            row = int(m.group(1))
        if row is None or row not in rows:
            continue
        cells = rows[row]
        out[s["step"]] = {"row": row, **{k: cells.get(col, "") for k, col in columns.items()}}
    return {"sheet": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path), "steps": out}


# MARK: The task's own files

def task_folder(task: str, tasks_dir: Path = TASKS_DIR) -> Path | None:
    """tasks/<folder>/ whose run.json names the task; else the folder named after its first word."""
    for path in sorted(tasks_dir.glob("*/run.json")):
        try:
            if json.loads(path.read_text()).get("task") == task:
                return path.parent
        except (ValueError, OSError):
            continue
    guess = tasks_dir / slug(task.split()[0]) if task.split() else None
    return guess if guess and guess.is_dir() else None


def load_json(path: Path | None, default=None):
    if not path or not path.is_file():
        return default
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return default


def plants(task: str, tasks_dir: Path = TASKS_DIR) -> dict:
    folder = task_folder(task, tasks_dir)
    return load_json(folder / "plants.json" if folder else None, {}) or {}


def calendar(task: str, day: str, *, tasks_dir: Path = TASKS_DIR, calendar_dir: Path = CALENDAR_DIR) -> dict:
    """The day's calendar events: Claude's reading of the calendar, or else only what was planted."""
    folder = task_folder(task, tasks_dir)
    planted = (load_json(folder / "calendar.json" if folder else None, {}) or {}).get("events", [])
    read = load_json(calendar_dir / f"{day}.json")
    if read:
        events = read.get("events", [])
        ids = {p.get("id") for p in planted}
        for e in events:
            e.setdefault("planted", e.get("id") in ids)
        return {"source": "read", "read_at": read.get("read_at"), "read_by": read.get("read_by"),
                "account": read.get("account"), "note": read.get("note"), "events": events,
                "file": f"data/calendar/{day}.json"}
    return {"source": "planted", "events": [{**p, "planted": True} for p in planted],
            "file": f"data/calendar/{day}.json", "note": "Only the planted events: nobody has read the calendar for this day."}


# MARK: Grades

def grade_path(task: str, day: str, step: str, grades_dir: Path = GRADES_DIR) -> Path:
    date.fromisoformat(day)  # refuses anything but a date
    return grades_dir / slug(task) / day / f"{slug(step)}.json"


def grades(task: str, day: str, steps: list[dict], grades_dir: Path = GRADES_DIR) -> dict:
    return {s["step"]: g for s in steps if (g := load_json(grade_path(task, day, s["step"], grades_dir)))}


def save_grade(body: dict, runs_dir: Path, grades_dir: Path = GRADES_DIR) -> dict:
    """Saves your ranking of the agents' replies at one step. Ranks are 1 (best) to the number of agents, each used once."""
    task, day, step = (str(body.get(k) or "") for k in ("task", "date", "step"))
    session = find_session(runs_dir, task, day)
    if step not in {s["step"] for s in session["steps"]}:
        raise TaskViewError(f"{task} on {day} has no step {step!r}")
    agents = [a["name"] for a in session["agents"]]
    ranks = body.get("ranks") or {}
    marked = body.get("marked") or {}
    if not isinstance(ranks, dict) or not isinstance(marked, dict):
        raise TaskViewError("ranks and marked must be objects")
    unknown = sorted((set(ranks) | set(marked)) - set(agents))
    if unknown:
        raise TaskViewError(f"{', '.join(unknown)} didn't run {task} on {day}")
    clean = {}
    for name, rank in ranks.items():
        if rank is None:
            continue
        if not isinstance(rank, int) or isinstance(rank, bool) or not 1 <= rank <= len(agents):
            raise TaskViewError(f"a rank is a whole number from 1 to {len(agents)}")
        clean[name] = rank
    if len(set(clean.values())) != len(clean):
        raise TaskViewError("two agents can't have the same rank")
    def message_ids(v) -> bool:
        """One message id (its whole turn is graded), or a list of the exact messages, where [] means nothing to grade."""
        if isinstance(v, str):
            return len(v) < 200
        return isinstance(v, list) and len(v) <= 30 and all(isinstance(x, str) and 0 < len(x) < 200 for x in v)
    if not all(message_ids(v) for v in marked.values()):
        raise TaskViewError("marked replies are message ids")
    # The parts of a reply to grade: exact passages of a message's text, each with what it's graded on
    excerpts = body.get("excerpts") or {}
    if not isinstance(excerpts, dict) or set(excerpts) - set(agents):
        raise TaskViewError("excerpts must be an object of the agents' passages")
    def passage(x) -> bool:
        return (isinstance(x, dict) and set(x) <= {"guid", "quote", "label", "aspect"}
                and isinstance(x.get("aspect", ""), str) and len(x.get("aspect", "")) <= 60
                and isinstance(x.get("guid"), str) and 0 < len(x["guid"]) < 200
                and isinstance(x.get("quote"), str) and 0 < len(x["quote"]) <= 5000
                and isinstance(x.get("label", ""), str) and len(x.get("label", "")) <= 80)
    if not all(isinstance(v, list) and len(v) <= 20 and all(passage(x) for x in v) for v in excerpts.values()):
        raise TaskViewError("an excerpt is a message id, the exact text to highlight, and a label")
    # Steps graded on more than one thing (CP1: the guest list, and the invite's voice) rank each separately
    aspect_ranks = body.get("aspect_ranks") or {}
    if not isinstance(aspect_ranks, dict) or not all(isinstance(k, str) and 0 < len(k) <= 60 and isinstance(v, dict)
                                                     for k, v in aspect_ranks.items()):
        raise TaskViewError("aspect_ranks must be an object of rankings")
    clean_aspects = {}
    for aspect, ranking in aspect_ranks.items():
        if set(ranking) - set(agents):
            raise TaskViewError(f"{', '.join(sorted(set(ranking) - set(agents)))} didn't run {task} on {day}")
        kept = {}
        for name, rank in ranking.items():
            if rank is None:
                continue
            if not isinstance(rank, int) or isinstance(rank, bool) or not 1 <= rank <= len(agents):
                raise TaskViewError(f"a rank is a whole number from 1 to {len(agents)}")
            kept[name] = rank
        if len(set(kept.values())) != len(kept):
            raise TaskViewError("two agents can't have the same rank")
        if kept:
            clean_aspects[aspect] = {a: kept[a] for a in agents if a in kept}
    path = grade_path(task, day, step, grades_dir)
    record = {"task": task, "date": day, "step": step,
              "ranks": {a: clean[a] for a in agents if a in clean},
              "marked": {a: marked[a] for a in agents if isinstance(marked.get(a), list) or marked.get(a)},
              "excerpts": {a: excerpts[a] for a in agents if excerpts.get(a)},
              "aspect_ranks": clean_aspects,
              "blind": bool(body.get("blind")), "updated_at": iso(datetime.now(UTC))}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as f:
        f.write(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    os.replace(f.name, path)
    return record


def view(task: str, day: str, *, runs_dir: Path, tasks_dir: Path = TASKS_DIR, grades_dir: Path = GRADES_DIR,
         calendar_dir: Path = CALENDAR_DIR) -> dict:
    """Everything the page needs about one task day, except the conversations and email."""
    session = find_session(runs_dir, task, day)
    return {
        "session": session,
        "rubrics": rubrics(task, session["steps"], tasks_dir),
        "grades": grades(task, day, session["steps"], grades_dir),
        "calendar": calendar(task, day, tasks_dir=tasks_dir, calendar_dir=calendar_dir),
        "grades_dir": f"data/grades/{slug(task)}/{day}/",
    }
