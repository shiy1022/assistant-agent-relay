"""Snapshots: what each page an agent linked to showed, saved for grading.

    bench snapshot [AGENT] [--hours H] [--retry]   saves the pages behind links agents sent, once each
    bench snapshot --mhtml                         adds the .mhtml to pages saved before it was made
    bench console                                  does the same by itself whenever a reply with a link is recorded

For every link in an agent's message, a fresh headless Chrome (a throwaway
profile, signed in to nothing) loads the page and waits. It never clicks,
types or submits anything. Five files are saved, named after the site and the
link:

    <site>-<hash>.mhtml  open this one, in Chrome: the page as it looked, with its styles, images and
                         fonts inside, in one file. Its scripts don't run when it's opened.
    <site>-<hash>.png    a full-page screenshot
    <site>-<hash>.txt    its visible text
    <site>-<hash>.html   its source as rendered, for searching. Don't open it: the site's scripts
                         re-run from your disk and often show an error instead (Expedia, Europcar),
                         and its styles are missing.
    <site>-<hash>.json   the link, the message it came in, the HTTP status, the title and when it was saved

A page that shows its content in a big embedded frame (Wajo's plans do) gets the
frame saved too, the same five ways, as <site>-<hash>-frame1.*, since a
screenshot of the outer page cuts a scrolling frame off at one screen. The
frame's text is in the main .txt as well.

They go in the deliverables/ of the agent's run the message belongs to: the
latest one that started before it, if the message came while it was recording
or within RUN_GRACE after (agents often finish after a run stops listening).
They're listed in that run.json's deliverables with the link as their source.
A link sent outside any run goes to data/snapshots/<agent>/.
data/snapshots/index.jsonl records every link handled, saved or skipped, so each
is handled once. Pages bench.deliverables saved count as handled too.

Skipped, and why:
- Sites that need your sign-in, or are the agents' own apps (SIGN_IN_HOSTS), like
  the dot's chatgpt.com/browser/handoff links into its own browser.
  Signed in to nothing, they'd show a sign-in page, and Slack holds the team's
  own messages.
- Links that look like actions rather than pages (ACTION): approve, confirm,
  unsubscribe, sign-in, invite, join, referral and so on. Opening one can be
  enough to do it.
- Anything that isn't http(s).

A bot check, like Expedia's "Bot or Not?", is saved as it shows. It's never got around.
"""

import hashlib
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bench import conversation, runs
from bench.config import ROOT

OUT_DIR = ROOT / "data" / "snapshots"  # data/ is never committed
INDEX = OUT_DIR / "index.jsonl"
HOURS = 24             # how far back a pass looks for links
RUN_GRACE = timedelta(hours=6)  # how long after a run stops a reply still counts as that run's
LOAD_SECONDS = 45      # for the page to load
SETTLE_SECONDS = 20    # for it to stop fetching, at most
WAIT_SECONDS = 3       # then a little longer, for pages that keep drawing
FRAME_MIN = 300        # px: an embedded frame at least this wide and tall is content, and is saved too
MAX_FRAMES = 3
SIGN_IN_HOSTS = ("slack.com", "hark.com", "meta.ai", "chatgpt.com", "accounts.google.com", "myaccount.google.com",
                 "mail.google.com", "calendar.google.com")  # chatgpt.com: the dot's browser handoffs, into its own session
SIGN_IN_PAGES = ("wajo.ai/user", "wajo.ai/settings", "wajo.ai/chat")  # host and path: the agents' account pages
ACTION = re.compile(r"(?:^|[/_.-])(approve|confirm|accept|decline|unsubscribe|verify|authori[sz]e|oauth2?|magic|"
                    r"logout|log-?in|sign-?in|sign-?up|rsvp|invite|join|referral|refer)(?:[/_.-]|$)", re.I)

Load = Callable[[str, Path, str], dict]  # (link, folder, name) -> what was saved, as in the .json file


def skip_reason(link: str) -> str | None:
    """Why `link` isn't loaded, or None if it is."""
    parts = urllib.parse.urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "not a web page"
    host = parts.hostname.lower()
    if any(host == h or host.endswith("." + h) for h in SIGN_IN_HOSTS) \
            or any(f"{host.removeprefix('www.')}{parts.path}".startswith(p) for p in SIGN_IN_PAGES):
        return "needs your sign-in"
    if ACTION.search(parts.path):
        return "looks like an action, not a page"
    return None


def name_for(link: str) -> str:
    host = re.sub(r"[^a-z0-9]+", "-", (urllib.parse.urlsplit(link).hostname or "link").lower()).strip("-")
    return f"{host}-{hashlib.sha1(link.encode()).hexdigest()[:8]}"


def read_index(index: Path = INDEX) -> list[dict]:
    return [json.loads(line) for line in index.read_text().splitlines() if line.strip()] if index.exists() else []


def handled(index_lines: list[dict], runs_dir: Path, *, retry: bool = False) -> set[str]:
    """Links already done: in the index (failures only unless `retry`), or saved in a run's deliverables."""
    done = {r["link"] for r in index_lines if not (retry and r.get("outcome") == "error")}
    for info_path in runs_dir.glob("*/run.json"):
        try:
            info = json.loads(info_path.read_text())
        except (OSError, ValueError):
            continue
        done |= {d["source"] for d in info.get("deliverables") or [] if d.get("source")}
    for meta in runs_dir.glob("*/deliverables/*.json"):
        try:
            done.add(json.loads(meta.read_text())["link"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return done


def run_starts(runs_dir: Path) -> list[tuple[str, str, Path, str | None]]:
    """(agent, started_at, run folder, ended_at) for every run, oldest first."""
    out = []
    for info_path in runs_dir.glob("*/run.json"):
        try:
            info = json.loads(info_path.read_text())
        except (OSError, ValueError):
            continue
        if info.get("agent") and info.get("started_at"):
            out.append((info["agent"], info["started_at"], info_path.parent, info.get("ended_at")))
    return sorted(out, key=lambda r: conversation.parse(r[1]))


def target(agent: str, sent_at: str, starts: list[tuple[str, str, Path, str | None]],
           out_dir: Path = OUT_DIR) -> tuple[Path, Path | None]:
    """(folder to save in, run folder or None): the agent's latest run that started before the message,
    if the message came while it recorded or within RUN_GRACE after; else the agent's own folder."""
    sent = conversation.parse(sent_at)
    mine = [r for r in starts if r[0] == agent and conversation.parse(r[1]) <= sent]
    if mine and (mine[-1][3] is None or sent <= conversation.parse(mine[-1][3]) + RUN_GRACE):
        return mine[-1][2] / "deliverables", mine[-1][2]
    return out_dir / agent, None


def pending(convos: dict[str, list[dict]], done: set[str], since: datetime,
            agents: list[str] | None = None) -> list[tuple[str, dict, str]]:
    """(agent, message, link) for each link in agents' messages since `since` that isn't done, once each."""
    out, seen = [], set(done)
    for agent, messages in convos.items():
        if agents and agent not in agents:
            continue
        for m in messages:
            if m.get("role") != "agent" or not m.get("sent_at") or conversation.parse(m["sent_at"]) < since:
                continue
            for link in m.get("links") or []:
                if link not in seen:
                    seen.add(link)
                    out.append((agent, m, link))
    return out


def register(run_dir: Path, meta: dict) -> None:
    """Lists the saved files in run.json's deliverables, with the link as their source, so a rebuild keeps it."""
    info = runs.read_info(run_dir)
    known = {d["path"]: d for d in info.get("deliverables") or []}
    for f in [*meta.get("files", []), f"{meta['name']}.json"]:
        path = f"deliverables/{f}"
        known[path] = {"path": path, "source": meta["link"], "saved_at": meta["saved_at"],
                       **({"status": meta["status"]} if meta.get("status") else {})}
    info["deliverables"] = sorted(known.values(), key=lambda d: d["path"])
    runs.write_info(run_dir, info)


@contextmanager
def chrome() -> Iterator[Load]:
    """A fresh headless Chrome with a throwaway profile. Yields a function that saves one page."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 1600}, accept_downloads=False, locale="en-US")

        def save(link: str, folder: Path, name: str, *, frames: bool) -> dict:
            page = context.new_page()
            page.on("dialog", lambda d: d.dismiss())
            try:
                response = page.goto(link, wait_until="domcontentloaded", timeout=LOAD_SECONDS * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=SETTLE_SECONDS * 1000)
                except Exception:
                    pass  # some pages never stop fetching
                time.sleep(WAIT_SECONDS)
                text = page.inner_text("body")
                content = content_frames(page) if frames else []
                (folder / f"{name}.html").write_text(page.content())
                page.screenshot(path=str(folder / f"{name}.png"), full_page=True)
                (folder / f"{name}.mhtml").write_text(mhtml(context, page))
                meta = {"status": response.status if response else None, "final_url": page.url, "title": page.title(),
                        "files": [f"{name}.mhtml", f"{name}.png", f"{name}.txt", f"{name}.html"]}
            finally:
                page.close()
            for i, frame_url in enumerate(content, 1):  # saved as pages of their own, so nothing is cut off
                try:
                    framed = save(frame_url, folder, f"{name}-frame{i}", frames=False)
                except Exception as e:
                    meta.setdefault("frames", []).append({"url": frame_url, "error": str(e).splitlines()[0][:200]})
                    continue
                text += f"\n\n--- embedded frame {i}: {frame_url}\n\n" + (folder / f"{name}-frame{i}.txt").read_text()
                meta["files"] += framed["files"]
                meta.setdefault("frames", []).append({"url": frame_url, "status": framed["status"], "title": framed["title"]})
            (folder / f"{name}.txt").write_text(text)
            return meta

        def load(link: str, folder: Path, name: str) -> dict:
            folder.mkdir(parents=True, exist_ok=True)
            return save(link, folder, name, frames=True)

        try:
            yield load
        finally:
            browser.close()


def content_frames(page) -> list[str]:
    """The web addresses of the page's big embedded frames: ones showing content, not ads or trackers."""
    urls = []
    for frame in page.main_frame.child_frames:
        if not frame.url.startswith(("http://", "https://")) or skip_reason(frame.url):
            continue
        try:
            box = frame.frame_element().bounding_box()
        except Exception:
            continue
        if box and box["width"] >= FRAME_MIN and box["height"] >= FRAME_MIN and frame.url not in urls:
            urls.append(frame.url)
    return urls[:MAX_FRAMES]


def mhtml(context, page) -> str:
    """The page as Chrome's "Save as single file" would keep it: the rendered page with everything it shows inside."""
    cdp = context.new_cdp_session(page)
    try:
        return cdp.send("Page.captureSnapshot", {"format": "mhtml"})["data"]
    finally:
        cdp.detach()


SCRIPT = re.compile(r"<script\b[^>]*>.*?</script\s*>|<script\b[^>]*/>", re.I | re.S)
NOSCRIPT = re.compile(r"<noscript\b[^>]*>.*?</noscript\s*>", re.I | re.S)
HEAD = re.compile(r"<head\b[^>]*>", re.I)


def offline_page(html: str, base_url: str) -> str:
    """A saved .html made viewable again: its scripts and <noscript> parts taken out (the page ran with scripts on),
    and a <base> so its styles and images come from the site."""
    html = NOSCRIPT.sub("", SCRIPT.sub("", html))
    base = f'<base href="{base_url.replace(chr(34), "%22")}">'
    return HEAD.sub(lambda m: m.group(0) + base, html, count=1) if HEAD.search(html) else base + html


def add_mhtml(*, runs_dir: Path = runs.RUNS_DIR, out_dir: Path = OUT_DIR,
              on_event: Callable[[str], None] | None = None) -> int:
    """Adds the .mhtml to pages saved before it was made, from their saved .html, with the site's styles and images as
    the site serves them now. A page that was a bot check or an error stays one. Returns how many were added."""
    from playwright.sync_api import sync_playwright
    todo = []
    for meta_path in [*runs_dir.glob("*/deliverables/*.json"), *out_dir.glob("*/*.json")]:
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        html = meta_path.with_suffix(".html")
        if "link" in meta and html.exists() and not meta_path.with_suffix(".mhtml").exists():
            todo.append((meta_path, meta, html))
    if not todo:
        return 0
    added = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 1600}, java_script_enabled=False,
                                      accept_downloads=False, locale="en-US")
        try:
            for meta_path, meta, html in todo:
                page_file = html.with_name(f".{html.stem}.offline.html")
                page_file.write_text(offline_page(html.read_text(), meta.get("final_url") or meta["link"]))
                page = context.new_page()
                try:
                    page.goto(page_file.resolve().as_uri(), wait_until="load", timeout=LOAD_SECONDS * 1000)
                    page.wait_for_timeout(WAIT_SECONDS * 1000)
                    meta_path.with_suffix(".mhtml").write_text(mhtml(context, page))
                except Exception as e:
                    if on_event:
                        on_event(f"snapshot: couldn't make {meta_path.stem}.mhtml: {str(e).splitlines()[0][:200]}")
                    continue
                finally:
                    page.close()
                    page_file.unlink(missing_ok=True)
                meta["files"] = [f"{meta_path.stem}.mhtml"] + [f for f in meta.get("files", []) if not f.endswith(".mhtml")]
                meta["mhtml_made"] = {"at": now_iso(), "from": "the saved .html, with the site's styles and images as served then"}
                meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
                if meta_path.parent.name == "deliverables" and (meta_path.parent.parent / "run.json").exists():
                    register(meta_path.parent.parent, meta | {"name": meta_path.stem})
                added += 1
                if on_event:
                    on_event(f"snapshot: made {meta_path.relative_to(ROOT) if meta_path.is_relative_to(ROOT) else meta_path}"
                             .removesuffix(".json") + ".mhtml")
        finally:
            browser.close()
    return added


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def save_new(*, agents: list[str] | None = None, hours: float = HOURS, retry: bool = False, again: tuple[str, ...] = (),
             convos: dict[str, list[dict]] | None = None, runs_dir: Path = runs.RUNS_DIR, out_dir: Path = OUT_DIR,
             index: Path | None = None, load: Load | None = None, now: datetime | None = None,
             on_event: Callable[[dict], None] | None = None) -> list[dict]:
    """Saves every link agents sent in the last `hours` that isn't saved yet, and the links in `again` even if they
    are. Returns what it did, one entry per link."""
    index = index or out_dir / "index.jsonl"
    convos = conversation.load() if convos is None else convos
    since = (now or datetime.now(UTC)) - timedelta(hours=hours)
    todo = pending(convos, handled(read_index(index), runs_dir, retry=retry) - set(again), since, agents)
    if not todo:
        return []
    starts = run_starts(runs_dir)
    results: list[dict] = []

    def record(entry: dict) -> None:
        index.parent.mkdir(parents=True, exist_ok=True)
        with index.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        results.append(entry)
        if on_event:
            on_event(entry)

    to_load = []
    for agent, m, link in todo:
        base = {"link": link, "agent": agent, "message": m["id"], "sent_at": m["sent_at"]}
        if reason := skip_reason(link):
            record(base | {"outcome": "skipped", "reason": reason, "at": now_iso()})
        else:
            to_load.append(base)
    if not to_load:
        return results

    def work(load: Load) -> None:
        for base in to_load:
            folder, run_dir = target(base["agent"], base["sent_at"], starts, out_dir)
            name = name_for(base["link"])
            meta = {"link": base["link"], "agent": base["agent"], "message": base["message"], "sent_at": base["sent_at"],
                    "name": name, "saved_at": now_iso()}
            try:
                meta |= load(base["link"], folder, name)
            except Exception as e:  # a page that won't load is noted, not retried, until a pass with retry
                record(base | {"outcome": "error", "error": f"{type(e).__name__}: {str(e).splitlines()[0][:300]}",
                               "at": now_iso()})
                continue
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{name}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
            if run_dir is not None:
                register(run_dir, meta)
            saved_to = folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder
            record(base | {"outcome": "saved", "status": meta.get("status"), "title": meta.get("title"),
                           "saved_to": f"{saved_to}/{name}.*", "at": meta["saved_at"]})

    if load is not None:
        work(load)
    else:
        with chrome() as load:
            work(load)
    return results


def describe(entry: dict) -> str:
    """One line about a link handled, for the console's log and bench snapshot."""
    link = entry["link"] if len(entry["link"]) <= 90 else entry["link"][:87] + "…"
    if entry["outcome"] == "saved":
        return f"snapshot: {entry['agent']}: saved {link} (HTTP {entry.get('status')}, {entry.get('title')!r}) → {entry['saved_to']}"
    if entry["outcome"] == "skipped":
        return f"snapshot: {entry['agent']}: skipped {link}: {entry['reason']}"
    return f"snapshot: {entry['agent']}: couldn't save {link}: {entry['error']}"
