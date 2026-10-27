import json
from datetime import UTC, datetime

import pytest

from bench import snapshots
from bench.snapshots import name_for, save_new, skip_reason

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)
CHECKOUT = "https://app.gojinko.com/checkout?t=abc.def"
HOTEL = "https://www.expedia.com/checkout/session/dbcd9922?productLine=LODGING"


@pytest.mark.parametrize("link", [
    CHECKOUT, HOTEL,
    "https://booking.flyfrontier.com/Payment/New",
    "https://www.europcar.com/en-us/reservation/reviewandbook?pickupDay=16",
    "https://www.google.com/travel/flights/booking?tfs=CBwQ",
    "https://visitsedona.com/trip-planning/frequently-asked-questions/",
    "https://wajo.ai/d/0b3c4d5e-1111-2222-3333-444455556666",
])
def test_pages_are_loaded(link):
    assert skip_reason(link) is None


@pytest.mark.parametrize("link, reason", [
    ("https://pjtty.slack.com/archives/C0CB93708GH/p1793070663278989", "sign-in"),
    ("https://hark.com/chat", "sign-in"),
    ("https://agent.meta.ai/connectors/connect/gmail", "sign-in"),
    ("https://mail.google.com/mail/u/0/#inbox", "sign-in"),
    ("https://chatgpt.com/browser/handoff?thread_id=01a17bcb&tab_id=21", "sign-in"),
    ("https://example.com/booking/confirm?token=x", "action"),
    ("https://example.com/approve/123", "action"),
    ("https://news.example.com/unsubscribe?u=1", "action"),
    ("https://auth.example.com/oauth/authorize?client_id=1", "action"),
    ("https://example.com/sign-in?next=/", "action"),
    ("https://example.com/invite/abc", "action"),
    ("https://wajo.ai/join?code=N25YT658&utm_source=referral", "action"),
    ("https://wajo.ai/user", "sign-in"),
    ("mailto:someone@example.com", "not a web page"),
    ("#hark:home", "not a web page"),
])
def test_links_that_are_never_loaded(link, reason):
    assert reason in skip_reason(link)


def message(id, sent_at, links, role="agent"):
    return {"id": id, "role": role, "sent_at": sent_at, "links": links, "text": " ".join(links)}


def write_run(runs_dir, name, agent, started_at, deliverables=(), ended_at=None):
    run = runs_dir / name
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"schema": 1, "run": name, "agent": agent, "started_at": started_at,
                                              "ended_at": ended_at, "deliverables": list(deliverables)}))
    return run


class FakeChrome:
    def __init__(self, fails=()):
        self.loaded, self.fails = [], set(fails)

    def __call__(self, link, folder, name):
        self.loaded.append(link)
        if link in self.fails:
            raise TimeoutError("page didn't load\nmore detail")
        folder.mkdir(parents=True, exist_ok=True)
        for ext in ("html", "txt", "png"):
            (folder / f"{name}.{ext}").write_text(link)
        return {"status": 200, "title": "Review and pay", "files": [f"{name}.{ext}" for ext in ("html", "txt", "png")]}


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "runs", tmp_path / "snapshots"


def run_pass(dirs, convos, chrome, **kw):
    runs_dir, out_dir = dirs
    return save_new(convos=convos, runs_dir=runs_dir, out_dir=out_dir, load=chrome, now=NOW, **kw)


def test_each_link_is_saved_once_into_the_agents_latest_run(dirs):
    runs_dir, out_dir = dirs
    early = write_run(runs_dir, "20261026-201339-travel-hark", "hark", "2026-10-27T03:13:39Z",
                      ended_at="2026-10-27T03:16:00Z")
    later = write_run(runs_dir, "20261026-205439-travel-wajo", "wajo", "2026-10-27T03:54:39Z")
    convos = {
        "wajo": [message("w1", "2026-10-27T03:56:15Z", [CHECKOUT]),
                 message("w2", "2026-10-27T03:57:00Z", [CHECKOUT]),  # sent again: saved once
                 message("u1", "2026-10-27T03:58:00Z", ["https://example.com/mine"], role="user")],  # yours: never
        "hark": [message("h1", "2026-10-27T04:30:00Z", [HOTEL])],  # an hour after its run ended: still that run's
        "dots": [message("d1", "2026-10-27T03:30:00Z", ["https://visitsedona.com/faq/"])],  # no run yet
    }
    chrome = FakeChrome()
    done = run_pass(dirs, convos, chrome)
    assert sorted(chrome.loaded) == sorted([CHECKOUT, HOTEL, "https://visitsedona.com/faq/"])
    assert {e["outcome"] for e in done} == {"saved"}
    meta = json.loads((later / "deliverables" / f"{name_for(CHECKOUT)}.json").read_text())
    assert meta["link"] == CHECKOUT and meta["message"] == "w1" and meta["status"] == 200
    assert (early / "deliverables" / f"{name_for(HOTEL)}.png").exists()
    assert (out_dir / "dots" / f"{name_for('https://visitsedona.com/faq/')}.txt").exists()
    listed = json.loads((later / "run.json").read_text())["deliverables"]
    assert {d["source"] for d in listed} == {CHECKOUT} and len(listed) == 4  # html, txt, png and json
    assert run_pass(dirs, convos, FakeChrome()) == []  # nothing new the next time


def test_a_reply_long_after_a_run_is_not_that_runs(dirs):
    runs_dir, out_dir = dirs
    write_run(runs_dir, "20261024-205729-travel-day1-muse", "muse", "2026-10-25T03:57:29Z", ended_at="2026-10-25T04:10:00Z")
    link = "https://support.link.com/questions/what-s-covered-with-protections"
    run_pass(dirs, {"muse": [message("m1", "2026-10-27T01:35:00Z", [link])]}, FakeChrome())
    assert (out_dir / "muse" / f"{name_for(link)}.json").exists()


def test_skipped_links_are_noted_once_and_never_loaded(dirs):
    slack = "https://pjtty.slack.com/archives/C0CB93708GH/p1793070663278989"
    convos = {"dots": [message("d1", "2026-10-27T03:59:54Z", [slack])]}
    chrome = FakeChrome()
    [entry] = run_pass(dirs, convos, chrome)
    assert entry["outcome"] == "skipped" and "sign-in" in entry["reason"] and chrome.loaded == []
    assert run_pass(dirs, convos, chrome) == []


def test_pages_saved_before_count_as_done(dirs):
    runs_dir, _ = dirs
    write_run(runs_dir, "20261026-205439-travel-wajo", "wajo", "2026-10-27T03:54:39Z",
              deliverables=[{"path": "deliverables/x.html", "source": CHECKOUT}])
    chrome = FakeChrome()
    assert run_pass(dirs, {"wajo": [message("w1", "2026-10-27T03:56:15Z", [CHECKOUT])]}, chrome) == []
    assert chrome.loaded == []


def test_old_messages_are_left_alone(dirs):
    chrome = FakeChrome()
    assert run_pass(dirs, {"wajo": [message("w1", "2026-10-20T00:00:00Z", [CHECKOUT])]}, chrome) == []
    assert chrome.loaded == []


def test_a_page_that_fails_is_noted_and_tried_again_only_when_asked(dirs):
    convos = {"wajo": [message("w1", "2026-10-27T03:56:15Z", [CHECKOUT])]}
    [entry] = run_pass(dirs, convos, FakeChrome(fails=[CHECKOUT]))
    assert entry["outcome"] == "error" and entry["error"] == "TimeoutError: page didn't load"
    assert run_pass(dirs, convos, FakeChrome()) == []
    [again] = run_pass(dirs, convos, FakeChrome(), retry=True)
    assert again["outcome"] == "saved"


def test_only_the_agents_asked_for(dirs):
    convos = {"wajo": [message("w1", "2026-10-27T03:56:15Z", [CHECKOUT])],
              "dots": [message("d1", "2026-10-27T03:56:15Z", [HOTEL])]}
    chrome = FakeChrome()
    run_pass(dirs, convos, chrome, agents=["dots"])
    assert chrome.loaded == [HOTEL]


def test_describe_says_what_happened():
    saved = {"outcome": "saved", "agent": "wajo", "link": CHECKOUT, "status": 200, "title": "Review and pay",
             "saved_to": "runs/r/deliverables/app-gojinko-com-1.*"}
    assert snapshots.describe(saved).startswith("snapshot: wajo: saved https://app.gojinko.com/checkout")
    assert "needs your sign-in" in snapshots.describe({"outcome": "skipped", "agent": "dots", "link": "https://x.slack.com/a",
                                                       "reason": "needs your sign-in"})


def test_a_saved_page_is_made_viewable_without_its_scripts():
    saved = ('<html><head><meta charset="utf-8"><script src="/app.js"></script><link rel="stylesheet" href="/a.css">'
             '</head><body><noscript>Please turn on JavaScript</noscript><script>render()</script>'
             '<h1>Review and pay</h1><img src="/car.png"></body></html>')
    page = snapshots.offline_page(saved, "https://app.gojinko.com/checkout?t=abc")
    assert "<script" not in page and "noscript" not in page and "Please turn on JavaScript" not in page
    assert page.startswith('<html><head><base href="https://app.gojinko.com/checkout?t=abc"><meta charset="utf-8">')
    assert '<link rel="stylesheet" href="/a.css">' in page and "<h1>Review and pay</h1>" in page


def test_a_link_can_be_saved_again(dirs):
    convos = {"wajo": [message("w1", "2026-10-27T03:56:15Z", [CHECKOUT])]}
    run_pass(dirs, convos, FakeChrome())
    chrome = FakeChrome()
    assert run_pass(dirs, convos, chrome) == [] and chrome.loaded == []
    [entry] = run_pass(dirs, convos, chrome, again=(CHECKOUT,))
    assert entry["outcome"] == "saved" and chrome.loaded == [CHECKOUT]
