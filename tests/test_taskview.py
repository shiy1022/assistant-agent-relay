import json
import time
import zipfile
from xml.sax.saxutils import escape

import pytest

from bench import taskview
from bench.taskview import TaskViewError


@pytest.fixture(autouse=True)
def pacific(monkeypatch):
    """Task days are local dates; these tests are written in Pacific time."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def write_run(runs_dir, name, *, task="Housewarming draft", step="DAY 1 CHECKPOINT 1", cell="D3", agent="hark",
              channel="hark", started="2026-11-08T03:23:06.000Z", ended="2026-11-08T03:26:00.000Z"):
    folder = runs_dir / name
    folder.mkdir(parents=True)
    info = {"schema": 1, "run": name, "task": task, "step": step, "cell": cell, "agent": agent, "channel": channel,
            "started_at": started, "ended_at": ended, "finished": "quiet for 180s", "messages": []}
    (folder / "run.json").write_text(json.dumps(info))


def write_xlsx(path, tab, rows, other_tabs=()):
    """A minimal workbook: the named tab holds rows {number: {column: text}}, half as shared strings, half inline."""
    shared, cells_xml = [], []
    for n, cells in sorted(rows.items()):
        row = []
        for i, (col, text) in enumerate(sorted(cells.items())):
            if i % 2 == 0:
                shared.append(text)
                row.append(f'<c r="{col}{n}" t="s"><v>{len(shared) - 1}</v></c>')
            else:
                row.append(f'<c r="{col}{n}" t="inlineStr"><is><t>{escape(text)}</t></is></c>')
        cells_xml.append(f'<row r="{n}">{"".join(row)}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    tabs = [*other_tabs, tab]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>' + "".join(
            f'<sheet name="{escape(t)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, t in enumerate(tabs)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(
            f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml" Type="x"/>' for i in range(len(tabs))) + "</Relationships>")
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}">' + "".join(f"<si><t>{escape(t)}</t></si>" for t in shared) + "</sst>")
        for i in range(len(tabs) - 1):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", f'<worksheet xmlns="{main}"><sheetData/></worksheet>')
        z.writestr(f"xl/worksheets/sheet{len(tabs)}.xml", f'<worksheet xmlns="{main}"><sheetData>{"".join(cells_xml)}</sheetData></worksheet>')


HEADER = {"A": "DAY / PHASE", "D": "EXACT PROMPTS TO SEND", "E": "SUCCESS CRITERIA", "G": "Functional grading",
          "H": "Score + Comments Agent 1", "L": "Checkpoint Subjective Expereince", "M": "Score + Comments Agent 1"}
ROWS = {
    1: HEADER,
    3: {"A": "DAY 1 CHECKPOINT 1\n(5 min after the last plant)", "D": "Plan my party", "E": "Picks the 21st",
        "G": "10 = all of it", "L": "(0–10) Does it know who you'd invite?\n\n- Not verbose"},
    4: {"A": "DAY 1 LEARNING 1", "D": "Here's how I host"},
    9: {"A": "renamed in the sheet", "L": "(0–10) Does it feel like you?"},
}


def test_a_task_day_is_every_run_of_one_task_that_started_on_one_date(tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "a", agent="wajo", channel="telegram", started="2026-11-08T03:23:06.000Z")
    write_run(runs, "b", agent="hark", started="2026-11-08T03:24:00.000Z")
    write_run(runs, "c", agent="wajo", channel="wajo", step="DAY 1 LEARNING 1", cell="D4", started="2026-11-08T03:33:42.000Z")
    write_run(runs, "d", agent="hark", step="DAY 1 CHECKPOINT 1", started="2026-11-08T03:37:26.000Z")  # the nudge's run
    write_run(runs, "e", task="Shopping", started="2026-11-01T20:00:00.000Z")
    write_run(runs, "f", step="DAY 5 CHECKPOINT 4", started="2026-11-08T08:30:00.000Z", ended="2026-11-08T08:40:00.000Z")  # 00:30 on Nov 8
    (runs / "broken").mkdir()
    (runs / "broken" / "run.json").write_text("{")

    days = taskview.sessions(runs)
    assert [(s["task"], s["date"]) for s in days] == [("Housewarming draft", "2026-11-08"), ("Housewarming draft", "2026-11-07"),
                                                      ("Shopping", "2026-11-01")]
    day = days[1]
    assert day["key"] == "Housewarming draft|2026-11-07" and day["runs"] == 4
    assert [s["step"] for s in day["steps"]] == ["DAY 1 CHECKPOINT 1", "DAY 1 LEARNING 1"]
    assert [r["run"] for r in day["steps"][0]["runs"]] == ["a", "b", "d"]
    assert day["agents"] == [{"name": "wajo", "channel": "wajo", "from": "2026-11-08T03:23:06.000Z"},  # its latest run's channel
                             {"name": "hark", "channel": "hark", "from": "2026-11-08T03:24:00.000Z"}]
    assert day["start"] == "2026-11-08T03:23:06.000Z" and day["end"] == "2026-11-08T08:00:00.000Z"  # midnight Pacific
    with pytest.raises(TaskViewError, match="no runs of 'Housewarming draft' on 2026-11-09"):
        taskview.find_session(runs, "Housewarming draft", "2026-11-09")


def test_each_steps_rubric_comes_from_the_task_sheet(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    write_xlsx(tasks / "Other tasks.xlsx", "Shopping", {1: HEADER})
    write_xlsx(tasks / "Housewarming task.xlsx", "Housewarming draft", ROWS, other_tabs=["Notes"])
    (tasks / "~$Housewarming task.xlsx").write_bytes(b"Excel's lock file, not a workbook")
    steps = [{"step": "DAY 1 CHECKPOINT 1", "cell": "D3"}, {"step": "DAY 1 LEARNING 1", "cell": "D4"},
             {"step": "DAY 5 CHECKPOINT 4", "cell": "D9"}, {"step": "DAY 9 NOWHERE", "cell": ""}]
    out = taskview.rubrics("Housewarming draft", steps, tasks)
    assert out["sheet"].endswith("Housewarming task.xlsx")
    assert out["steps"]["DAY 1 CHECKPOINT 1"] == {
        "row": 3, "phase": "DAY 1 CHECKPOINT 1\n(5 min after the last plant)", "prompt": "Plan my party",
        "success": "Picks the 21st", "functional": "10 = all of it", "subjective": "(0–10) Does it know who you'd invite?\n\n- Not verbose"}
    assert out["steps"]["DAY 1 LEARNING 1"]["subjective"] == ""  # nothing to grade at a learning step
    assert out["steps"]["DAY 5 CHECKPOINT 4"]["subjective"] == "(0–10) Does it feel like you?"  # by the run's cell
    assert "DAY 9 NOWHERE" not in out["steps"]
    assert taskview.rubrics("Travel", steps, tasks) == {"sheet": None, "steps": {}}


def test_calendar_is_claudes_reading_or_else_what_was_planted(tmp_path):
    tasks, cal = tmp_path / "tasks", tmp_path / "calendar"
    (tasks / "housewarming").mkdir(parents=True)
    (tasks / "housewarming" / "run.json").write_text(json.dumps({"task": "Housewarming draft", "start": "2026-11-07"}))
    (tasks / "housewarming" / "calendar.json").write_text(json.dumps({"events": [{"id": "p1", "summary": "Lab dinner"}]}))
    assert taskview.task_folder("Housewarming draft", tasks) == tasks / "housewarming"
    only = taskview.calendar("Housewarming draft", "2026-11-07", tasks_dir=tasks, calendar_dir=cal)
    assert only["source"] == "planted" and only["events"] == [{"id": "p1", "summary": "Lab dinner", "planted": True}]

    cal.mkdir()
    (cal / "2026-11-07.json").write_text(json.dumps({"read_at": "2026-11-08T05:58:00Z", "read_by": "Claude", "events": [
        {"id": "p1", "summary": "Lab dinner"}, {"id": "x9", "summary": "Housewarming at home", "by": "hark"}]}))
    read = taskview.calendar("Housewarming draft", "2026-11-07", tasks_dir=tasks, calendar_dir=cal)
    assert read["source"] == "read" and read["read_at"] == "2026-11-08T05:58:00Z"
    assert [(e["id"], e["planted"]) for e in read["events"]] == [("p1", True), ("x9", False)]


def test_a_task_folder_can_be_found_by_the_tasks_first_word(tmp_path):
    (tmp_path / "shopping").mkdir()
    assert taskview.task_folder("Shopping vF", tmp_path) == tmp_path / "shopping"
    assert taskview.task_folder("Travel", tmp_path) is None
    assert taskview.plants("Travel", tmp_path) == {}


def test_rankings_are_saved_per_step_and_checked(tmp_path):
    runs, grades = tmp_path / "runs", tmp_path / "grades"
    for i, agent in enumerate(["wajo", "muse", "dots", "hark"]):
        write_run(runs, f"r{i}", agent=agent, channel=agent)
    body = {"task": "Housewarming draft", "date": "2026-11-07", "step": "DAY 1 CHECKPOINT 1"}
    saved = taskview.save_grade(body | {"ranks": {"hark": 1, "wajo": 2, "muse": None}, "marked": {"hark": "hark:abc"}, "blind": True},
                                runs, grades)
    assert saved["ranks"] == {"wajo": 2, "hark": 1} and saved["marked"] == {"hark": "hark:abc"} and saved["blind"] is True
    path = grades / "housewarming-draft" / "2026-11-07" / "day-1-checkpoint-1.json"
    assert json.loads(path.read_text()) == saved
    assert taskview.grades("Housewarming draft", "2026-11-07", [{"step": "DAY 1 CHECKPOINT 1"}, {"step": "DAY 3"}], grades) == \
        {"DAY 1 CHECKPOINT 1": saved}

    for bad, error in [
        ({"ranks": {"wajo": 1, "hark": 1}}, "same rank"),
        ({"ranks": {"wajo": 5}}, "from 1 to 4"),
        ({"ranks": {"wajo": True}}, "from 1 to 4"),
        ({"ranks": {"wajo": "1"}}, "from 1 to 4"),
        ({"ranks": {"boba": 1}}, "boba didn't run"),
        ({"ranks": [1, 2]}, "must be objects"),
        ({"marked": {"wajo": 3}}, "message ids"),
        ({"marked": {"wajo": ["wajo:a", 3]}}, "message ids"),
        ({"marked": {"wajo": [""]}}, "message ids"),
        ({"step": "DAY 9"}, "no step 'DAY 9'"),
        ({"date": "../../etc"}, "not a date"),
        ({"date": "2026-11-08"}, "no runs"),
    ]:
        with pytest.raises(TaskViewError, match=error):
            taskview.save_grade(body | bad, runs, grades)
    assert json.loads(path.read_text()) == saved  # nothing bad was written over it

    # A list marks exactly those messages; an empty list says there's nothing to grade, and is kept
    lists = taskview.save_grade(body | {"marked": {"wajo": ["wajo:a", "wajo:b"], "dots": [], "muse": ""}}, runs, grades)
    assert lists["marked"] == {"wajo": ["wajo:a", "wajo:b"], "dots": []}

    # Excerpts: the exact passages to grade, kept with the ranks
    ex = {"hark": [{"guid": "hark:abc", "quote": "Hey Bob,", "label": "Reply to Bob"}]}
    saved_ex = taskview.save_grade(body | {"ranks": {"hark": 1}, "excerpts": ex}, runs, grades)
    assert saved_ex["excerpts"] == ex and saved_ex["ranks"] == {"hark": 1}
    # Separate rankings per aspect (CP1: the guest list, and the invite's voice)
    asp = taskview.save_grade(body | {"aspect_ranks": {"Guest list": {"hark": 1, "wajo": 2}, "Invite": {"wajo": 1, "muse": None}}}, runs, grades)
    assert asp["aspect_ranks"] == {"Guest list": {"wajo": 2, "hark": 1}, "Invite": {"wajo": 1}}
    for bad, error in [({"Invite": {"wajo": 1, "hark": 1}}, "same rank"), ({"Invite": {"boba": 1}}, "boba didn't run"),
                       ({"Invite": {"wajo": 9}}, "from 1 to 4"), ({"": {}}, "aspect_ranks"), ({"Invite": [1]}, "aspect_ranks")]:
        with pytest.raises(TaskViewError, match=error):
            taskview.save_grade(body | {"aspect_ranks": bad}, runs, grades)
    for bad in [{"boba": []}, {"hark": [{"guid": "hark:abc"}]}, {"hark": [{"guid": "hark:abc", "quote": "x", "extra": 1}]}, {"hark": "x"}]:
        with pytest.raises(TaskViewError, match="excerpt"):
            taskview.save_grade(body | {"excerpts": bad}, runs, grades)


def test_the_view_has_steps_rubric_grades_and_calendar(tmp_path):
    runs, tasks = tmp_path / "runs", tmp_path / "tasks"
    tasks.mkdir()
    write_run(runs, "a")
    write_xlsx(tasks / "Housewarming task.xlsx", "Housewarming draft", ROWS)
    v = taskview.view("Housewarming draft", "2026-11-07", runs_dir=runs, tasks_dir=tasks, grades_dir=tmp_path / "g",
                      calendar_dir=tmp_path / "c")
    assert v["session"]["key"] == "Housewarming draft|2026-11-07"
    assert list(v["rubrics"]["steps"]) == ["DAY 1 CHECKPOINT 1"]
    assert v["grades"] == {} and v["calendar"]["events"] == []
    assert v["grades_dir"] == "data/grades/housewarming-draft/2026-11-07/"
    with pytest.raises(TaskViewError, match="not a date"):
        taskview.view("Housewarming draft", "Nov 7", runs_dir=runs)
