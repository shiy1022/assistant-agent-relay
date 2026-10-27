import argparse
import json
import urllib.request
from datetime import UTC, datetime

import pytest

from bench import cli, conversation, deliverables, runs
from bench.config import Agent
from tests.test_dashboard import FO, tg, write_jsonl
from tests.test_runs import PROMPT, START, Clock, convos

PLAN = "https://wajo.ai/d/b6ffe336-89df-4375-b32d-dae17b96cdfc"
PLAN_FRAME = "https://wajo.ai/api/d/b6ffe336-89df-4375-b32d-dae17b96cdfc"
REFERRAL = "https://wajo.ai/?ref=yueheng"
SITE = "https://hark.com/site/dd7d20a1-bca3-42b5-a0d0-eda7c0f80714"
HARK = Agent("hark", "hark.com", 180, 1800, channel="hark")
PAGE = """<!doctype html><html><head><title>Sedona, MLK Weekend 2027 · Wajo</title>
<style>body { color: red }</style><script>alert("hi")</script></head>
<body><main><h1>Sedona,
  MLK Weekend 2027</h1><p>Fri: <strong>Airport Mesa</strong> at sunset.</p>
<div><div><a href="https://amararesort.com/">Check rates at Amara</a></div></div>
<ul><li>Elote &amp; Mariposa</li><li><a href="https://sedonashuttle.com/">sedonashuttle.com</a></li></ul>
<a href="#days">Day by day</a></main></body></html>"""


class FakeFetch:
    """Answers with PAGE, or raises for the addresses in `fail`. Records what was asked for."""

    def __init__(self, fail=()):
        self.asked, self.fail = [], set(fail)

    def __call__(self, url):
        self.asked.append(url)
        if url in self.fail:
            raise deliverables.DeliverableError(f"{url} answered HTTP 503 Service Unavailable")
        return PAGE


@pytest.fixture
def agents(monkeypatch):
    monkeypatch.setattr(runs, "load_agents", lambda: {"wajo": FO})


def a_run(tmp_path, links, agent=FO, **info):
    run_dir = runs.start(agent, PROMPT, task="Travel", runs_dir=tmp_path / "runs", now=START)
    runs.write_info(run_dir, runs.read_info(run_dir) | {"links": links} | info)
    return run_dir


# MARK: Which links are deliverables

def test_only_the_agents_own_pages_are_deliverables():
    links = [REFERRAL, PLAN, "https://wajo.ai/invite/abc123", "https://wajo.ai/d/not-a-plan",
             "http://wajo.ai/d/b6ffe336-89df-4375-b32d-dae17b96cdfc",           # not HTTPS
             "https://wajo.ai.example.com/d/b6ffe336-89df-4375-b32d-dae17b96cdfc",  # someone else's site
             "https://example.com/https://wajo.ai/d/b6ffe336-89df-4375-b32d-dae17b96cdfc",
             "https://wajo.ai/d/0a1b2c3d-0000-4000-8000-000000000000?utm_source=telegram"]
    assert deliverables.pages("wajo", links) == [
        (PLAN, PLAN_FRAME, "wajo-b6ffe336"),
        ("https://wajo.ai/d/0a1b2c3d-0000-4000-8000-000000000000?utm_source=telegram",
         "https://wajo.ai/api/d/0a1b2c3d-0000-4000-8000-000000000000", "wajo-0a1b2c3d")]
    assert deliverables.pages("muse", [PLAN]) == []  # only the agent that makes them
    assert deliverables.pages(None, [PLAN]) == []


def test_the_same_page_linked_twice_is_one_deliverable():
    assert deliverables.pages("wajo", [PLAN, PLAN + "/", PLAN + "#days"]) == [(PLAN, PLAN_FRAME, "wajo-b6ffe336")]


def test_hark_pages_are_recognised_but_never_fetched():
    assert deliverables.pages("hark", [SITE, SITE + "#days", "https://hark.com/chat", PLAN]) == [(SITE, None, "hark-dd7d20a1")]
    assert deliverables.pages("wajo", [SITE]) == []


def a_transcript(run_dir, *messages):
    conversation.write_jsonl(run_dir / "transcript.jsonl",
                             [{"id": f"hark:{i}", "agent": "hark", "role": "agent", "text": "", "links": [], "attachments": []} | m
                              for i, m in enumerate(messages)])


def test_a_hark_page_is_copied_from_what_the_channel_saved(tmp_path):
    copy = tmp_path / "inbox" / "hark" / "attachments" / "m1" / "Sedona MLK Weekend 2027.html"
    copy.parent.mkdir(parents=True)
    copy.write_text(PAGE)
    run_dir = a_run(tmp_path, [SITE], agent=HARK)
    a_transcript(run_dir, {"text": f"Page: Sedona MLK Weekend 2027\n{SITE}", "links": [SITE],
                           "attachments": [{"name": copy.name, "mime_type": "text/html", "path": str(copy)}]})
    fetch = FakeFetch()
    saved, unsaved = deliverables.save(run_dir, fetch=fetch, now=datetime(2026, 10, 25, 22, 20, tzinfo=UTC))
    assert fetch.asked == [] and unsaved == []
    assert [d["path"] for d in saved] == ["deliverables/hark-dd7d20a1.html", "deliverables/hark-dd7d20a1.txt"]
    assert (run_dir / "deliverables" / "hark-dd7d20a1.html").read_text() == PAGE
    assert (run_dir / "deliverables" / "hark-dd7d20a1.txt").read_text().startswith("Sedona, MLK Weekend 2027 · Wajo\n")
    assert all(d["source"] == SITE for d in saved)


def test_a_hark_page_without_a_saved_copy_is_listed_as_unsaved(tmp_path):
    run_dir = a_run(tmp_path, [SITE], agent=HARK)
    a_transcript(run_dir, {"text": SITE, "links": [SITE], "attachments": [{"name": "x.html", "mime_type": "text/html"}]})
    fetch = FakeFetch()
    saved, unsaved = deliverables.save(run_dir, fetch=fetch)
    assert (saved, fetch.asked) == ([], [])
    assert [u["source"] for u in unsaved] == [SITE] and "bench sync hark" in unsaved[0]["error"]
    assert runs.read_info(run_dir)["unsaved_deliverables"] == unsaved


# MARK: Saving them

def test_save_writes_the_page_and_its_text_and_lists_them(tmp_path):
    run_dir = a_run(tmp_path, [REFERRAL, PLAN])
    fetch = FakeFetch()
    saved, unsaved = deliverables.save(run_dir, fetch=fetch, now=datetime(2026, 10, 25, 3, 40, tzinfo=UTC))

    assert fetch.asked == [PLAN_FRAME]  # never the referral link
    assert (run_dir / "deliverables" / "wajo-b6ffe336.html").read_text() == PAGE
    assert (run_dir / "deliverables" / "wajo-b6ffe336.txt").read_text().startswith("Sedona, MLK Weekend 2027 · Wajo\n")
    entries = [{"path": "deliverables/wajo-b6ffe336.html", "source": PLAN, "saved_at": "2026-10-25T03:40:00.000Z"},
               {"path": "deliverables/wajo-b6ffe336.txt", "source": PLAN, "saved_at": "2026-10-25T03:40:00.000Z"}]
    assert (saved, unsaved) == (entries, [])
    info = runs.read_info(run_dir)
    assert info["deliverables"] == entries and "unsaved_deliverables" not in info
    assert list(info)[:len(runs.STANDARD)] == list(runs.STANDARD)


def test_save_skips_pages_already_saved_unless_again(tmp_path):
    hand_saved = {"path": "deliverables/plan.html", "source": PLAN}
    run_dir = a_run(tmp_path, [PLAN], deliverables=[hand_saved])
    fetch = FakeFetch()
    assert deliverables.save(run_dir, fetch=fetch) == ([], [])
    assert fetch.asked == [] and runs.read_info(run_dir)["deliverables"] == [hand_saved]

    saved, _ = deliverables.save(run_dir, fetch=fetch, again=True)
    assert fetch.asked == [PLAN_FRAME]
    assert [d["path"] for d in runs.read_info(run_dir)["deliverables"]] == [
        "deliverables/plan.html", "deliverables/wajo-b6ffe336.html", "deliverables/wajo-b6ffe336.txt"]


def test_a_run_with_no_deliverables_fetches_nothing(tmp_path):
    run_dir = a_run(tmp_path, [REFERRAL, "https://maps.google.com/?cid=1"])
    fetch = FakeFetch()
    assert deliverables.save(run_dir, fetch=fetch) == ([], [])
    assert fetch.asked == [] and not (run_dir / "deliverables").exists()


def test_a_page_that_fails_is_listed_until_it_is_saved(tmp_path):
    other = "https://wajo.ai/d/0a1b2c3d-0000-4000-8000-000000000000"
    run_dir = a_run(tmp_path, [PLAN, other])
    saved, unsaved = deliverables.save(run_dir, fetch=FakeFetch(fail={PLAN_FRAME}))
    assert [d["source"] for d in saved] == [other, other]
    assert unsaved == [{"source": PLAN, "error": f"{PLAN_FRAME} answered HTTP 503 Service Unavailable"}]
    assert runs.read_info(run_dir)["unsaved_deliverables"] == unsaved

    fetch = FakeFetch()
    deliverables.save(run_dir, fetch=fetch)
    assert fetch.asked == [PLAN_FRAME]  # the one already saved isn't fetched again
    info = runs.read_info(run_dir)
    assert "unsaved_deliverables" not in info
    assert len(info["deliverables"]) == 4


def test_rebuild_keeps_where_each_page_came_from_and_when(tmp_path, agents):
    events = [tg("new", 9, PROMPT, from_me=True, date="2026-10-25T03:32:53.000Z"),
              tg("new", 10, f"here's your plan: {PLAN}\nand {REFERRAL}", date="2026-10-25T03:37:05.000Z")]
    run_dir = runs.start(FO, PROMPT, task="Travel", runs_dir=tmp_path / "runs", now=START)
    runs.finish(run_dir, "quiet for 90s", now=datetime(2026, 10, 25, 3, 40, tzinfo=UTC),
                conversations=convos(tmp_path, events))
    saved, _ = deliverables.save(run_dir, fetch=FakeFetch(), now=datetime(2026, 10, 25, 3, 40, tzinfo=UTC))
    assert runs.rebuild(run_dir, convos(tmp_path, events))["deliverables"] == saved


# MARK: A page with its files inside

FONT_CSS = "@font-face{font-family:'Geist';src:url(https://fonts.gstatic.com/s/geist/v1/a.woff2) format('woff2')}"
LIVE = ('<html><head><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist&amp;display=swap"/></head>'
        '<body><img class="hero" src="data:image/jpeg;base64,/9j/4AAQ" alt="hero"/>'
        '<img src="https://cf.bstatic.com/hotel/1.webp?k=abc&amp;o=" alt="El Portal"/>'
        '<a href="https://www.booking.com/hotel/us/el-portal-sedona.html">Book</a></body></html>')


class FakeFiles:
    def __init__(self, fail=()):
        self.asked, self.fail = [], set(fail)

    def __call__(self, url):
        self.asked.append(url)
        if url in self.fail:
            raise deliverables.DeliverableError(f"{url} answered HTTP 404 Not Found")
        if url.startswith("https://fonts.googleapis.com/"):
            return FONT_CSS.encode(), "text/css"
        if url.endswith(".woff2"):
            return b"WOFF2", "font/woff2"
        return b"RIFF", "image/webp"


def test_freeze_embeds_the_pages_images_and_fonts_and_leaves_links_alone():
    files = FakeFiles()
    frozen, missing = deliverables.freeze(LIVE, fetch=files)
    assert missing == []
    assert files.asked == ["https://cf.bstatic.com/hotel/1.webp?k=abc&o=",  # as the browser would ask, unescaped
                           "https://fonts.googleapis.com/css2?family=Geist&display=swap",
                           "https://fonts.gstatic.com/s/geist/v1/a.woff2"]
    assert '<img src="data:image/webp;base64,UklGRg==" alt="El Portal"/>' in frozen
    assert '<img class="hero" src="data:image/jpeg;base64,/9j/4AAQ" alt="hero"/>' in frozen  # already inside
    assert "<style>@font-face{font-family:'Geist';src:url(data:font/woff2;base64,V09GRjI=) format('woff2')}</style>" in frozen
    assert "fonts.googleapis.com" not in frozen
    assert 'href="https://www.booking.com/hotel/us/el-portal-sedona.html"' in frozen


def test_freeze_keeps_what_it_could_not_fetch_and_says_so():
    files = FakeFiles(fail={"https://cf.bstatic.com/hotel/1.webp?k=abc&o=", "https://fonts.gstatic.com/s/geist/v1/a.woff2"})
    frozen, missing = deliverables.freeze(LIVE, fetch=files)
    assert 'src="https://cf.bstatic.com/hotel/1.webp?k=abc&amp;o="' in frozen
    assert "url(https://fonts.gstatic.com/s/geist/v1/a.woff2)" in frozen
    assert missing == ["https://cf.bstatic.com/hotel/1.webp?k=abc&o= answered HTTP 404 Not Found",
                       "https://fonts.gstatic.com/s/geist/v1/a.woff2 answered HTTP 404 Not Found"]


# MARK: The text version

def test_text_reads_like_the_page_with_link_addresses():
    assert deliverables.text(PAGE) == (
        "Sedona, MLK Weekend 2027 · Wajo\n\n"
        "Sedona, MLK Weekend 2027\n\n"
        "Fri: Airport Mesa at sunset.\n\n"
        "Check rates at Amara (https://amararesort.com/)\n"
        "Elote & Mariposa\n"
        "sedonashuttle.com\n"
        "Day by day\n")


def test_text_of_a_bare_fragment():
    assert deliverables.text("plain <b>words</b><br>next line") == "plain words\nnext line\n"


# MARK: Fetching

def test_redirects_to_another_site_are_refused():
    handler = deliverables.SameSite()
    request = urllib.request.Request(PLAN_FRAME)
    assert handler.redirect_request(request, None, 302, "Found", {}, PLAN_FRAME + "/").full_url == PLAN_FRAME + "/"
    for elsewhere in ("https://accounts.example.com/login", "http://wajo.ai/api/d/x"):
        with pytest.raises(deliverables.DeliverableError, match="redirects to another site"):
            handler.redirect_request(request, None, 302, "Found", {}, elsewhere)


# MARK: bench run and bench run-save

def test_bench_run_saves_the_page_the_agent_linked_to(tmp_path, monkeypatch, agents, capsys):
    inbox = tmp_path / "inbox" / "messages.jsonl"
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(conversation, "inbox_path", lambda channel: tmp_path / ("inbox" if channel == "telegram" else channel) / "messages.jsonl")
    monkeypatch.setattr(cli, "get_agent", lambda name: FO)
    monkeypatch.setattr(runs.time, "monotonic", Clock())
    fetch = FakeFetch()
    monkeypatch.setattr(deliverables, "http_get", fetch)
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(PROMPT)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def converse(agent, text, plan, on_event):
        script = [tg("new", 9, text, from_me=True, date=now),
                  tg("new", 10, f"here's your plan: {PLAN}\ninvite a friend: {REFERRAL}", date=now)]
        write_jsonl(inbox, script)
        for event in script:
            on_event(event)
        while seconds := plan.next():
            runs.time.monotonic.now += seconds

    monkeypatch.setattr(cli, "converse", converse)
    cli.cmd_run(argparse.Namespace(agent="wajo", file=str(prompt_file), task="Travel", step=None, cell=None,
                                   quiet=None, max_wait=None))
    [run_dir] = (tmp_path / "runs").iterdir()
    info = runs.read_info(run_dir)
    assert fetch.asked == [PLAN_FRAME]
    assert info["links"] == [PLAN, REFERRAL]
    assert [(d["path"], d["source"]) for d in info["deliverables"]] == [
        ("deliverables/wajo-b6ffe336.html", PLAN), ("deliverables/wajo-b6ffe336.txt", PLAN)]
    assert (run_dir / "deliverables" / "wajo-b6ffe336.html").read_text() == PAGE
    assert f"Saved deliverables/wajo-b6ffe336.html from {PLAN}" in capsys.readouterr().out


def test_bench_run_save_saves_an_existing_run_and_fails_if_it_cant(tmp_path, monkeypatch, capsys):
    run_dir = a_run(tmp_path, [PLAN])
    monkeypatch.setattr(deliverables, "http_get", FakeFetch(fail={PLAN_FRAME}))
    assert cli.main(["run-save", str(run_dir)]) == 1
    assert f"couldn't save {PLAN}" in capsys.readouterr().err
    assert runs.read_info(run_dir)["deliverables"] == []

    monkeypatch.setattr(deliverables, "http_get", FakeFetch())
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    assert cli.main(["run-save"]) == 0  # every run
    assert len(runs.read_info(run_dir)["deliverables"]) == 2
    assert json.loads((run_dir / "run.json").read_text()).get("unsaved_deliverables") is None
