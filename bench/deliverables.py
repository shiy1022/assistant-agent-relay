"""Deliverables: the pages an agent links to as its answer, saved into the run's folder.

    bench run AGENT …                  saves them once the run's folder is written
    bench run-save [RUN_DIR ...]       saves them for runs already written (default: every run)
    bench run-save RUN_DIR --again     saves them again, say after the agent updated the page

Only links that match one of the agent's PAGES are ever fetched. Anything else
an agent sends, like a referral or invite link, is left alone. A page the
channel already saved a copy of, like a Hark Page (behind Hark's sign-in, so
the Hark channel saves it as the message's attachment), is copied from there
instead of being fetched.

Each page is saved twice in deliverables/: <name>.html, the page as served, and
<name>.txt, its text with each link's address after it. Both are listed in
run.json's deliverables with the link the agent sent (source) and when they
were saved (saved_at). A page that couldn't be saved is listed in run.json's
unsaved_deliverables, with why, until a later run-save saves it.
"""

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path

from bench import runs

TIMEOUT = 30
MAX_BYTES = 20_000_000
USER_AGENT = "agent-runner (Assistant Bench; saves pages agents link to as their answers)"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class DeliverableError(Exception):
    pass


@dataclass(frozen=True)
class Page:
    """A kind of page an agent links to as its answer."""

    link: re.Pattern   # the whole link as the agent sends it; its groups name the page
    fetch: str | None  # the address to fetch, from the link's groups; None if only the channel's saved copy will do
    name: str          # the files it's saved as in deliverables/, from the link's groups

    def match(self, link: str) -> tuple[str | None, str] | None:
        """(the address to fetch, the files' name) if `link` is one of these pages."""
        m = self.link.fullmatch(link)
        if not m:
            return None
        return (self.fetch.format(**m.groupdict()) if self.fetch else None, self.name.format(**m.groupdict()))


PAGES: dict[str, list[Page]] = {
    # Wajo's plans, open to anyone with the link. wajo.ai/d/<id> is Wajo's app,
    # which shows the plan in a frame; the frame, wajo.ai/api/d/<id>, is the plan
    # itself, one HTML file with its fonts and images inside.
    "wajo": [Page(re.compile(rf"https://wajo\.ai/d/(?P<id>{UUID})/?(?:[?#].*)?"),
                  "https://wajo.ai/api/d/{id}", "wajo-{id:.8}")],
    # Hark's Pages live on hark.com behind its sign-in, so they're never fetched
    # from here: the Hark channel saves a copy of each as the message's attachment
    # (bench.channels.hark), and that copy is what goes into deliverables/.
    "hark": [Page(re.compile(rf"https://hark\.com/site/(?P<id>{UUID})/?(?:[?#].*)?"), None, "hark-{id:.8}")],
}

Fetch = Callable[[str], str]  # an address → the page's HTML; raises DeliverableError
FetchBytes = Callable[[str], tuple[bytes, str]]  # an address → (the file, its media type); raises DeliverableError
MAX_ASSET = 5_000_000


class SameSite(urllib.request.HTTPRedirectHandler):
    """Follows redirects only within the site asked for, so nothing else is ever fetched."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc):
            raise DeliverableError(f"{req.full_url} redirects to another site, {new.scheme}://{new.netloc}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str) -> str:
    """The HTML at `url`, over HTTPS, without cookies or anything signed in."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.build_opener(SameSite).open(request, timeout=TIMEOUT) as r:
            if r.headers.get_content_type() != "text/html":
                raise DeliverableError(f"{url} is {r.headers.get_content_type()}, not a web page")
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise DeliverableError(f"{url} is over {MAX_BYTES // 1_000_000} MB")
            return body.decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise DeliverableError(f"{url} answered HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DeliverableError(f"couldn't fetch {url}: {getattr(e, 'reason', e)}") from None


def read_copy(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError as e:
        raise DeliverableError(f"couldn't read the saved copy {path}: {e.strerror or e}") from None


def http_get_bytes(url: str) -> tuple[bytes, str]:
    """A page's image, stylesheet or font at `url`, as (bytes, media type), without cookies."""
    # Google Fonts hands out woff2 only to a browser-like client; anything else gets bigger, older formats.
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"})
    try:
        with urllib.request.build_opener(SameSite).open(request, timeout=TIMEOUT) as r:
            body = r.read(MAX_ASSET + 1)
            if len(body) > MAX_ASSET:
                raise DeliverableError(f"{url} is over {MAX_ASSET // 1_000_000} MB")
            return body, r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        raise DeliverableError(f"{url} answered HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DeliverableError(f"couldn't fetch {url}: {getattr(e, 'reason', e)}") from None


def data_uri(body: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(body).decode()}"


def freeze(html: str, fetch: FetchBytes | None = None) -> tuple[str, list[str]]:
    """The page with its remote images and web fonts embedded, so it opens anywhere, offline, as it was.

    Images (<img src=https://…>) become data: URIs. A stylesheet from fonts.googleapis.com is
    inlined with its font files embedded. Links to other sites are left as links. Only the
    files the page itself shows are fetched. Returns the page and what couldn't be fetched.
    """
    fetch = fetch or http_get_bytes
    missing: list[str] = []

    def embedded(url: str) -> str | None:
        try:
            return data_uri(*fetch(html_unescape(url)))
        except DeliverableError as e:
            missing.append(str(e))
            return None

    def image(m: re.Match) -> str:
        data = embedded(m.group(2))
        return f"{m.group(1)}{data}{m.group(3)}" if data else m.group(0)

    def stylesheet(m: re.Match) -> str:
        url = html_unescape(m.group(1))
        try:
            css = fetch(url)[0].decode("utf-8", errors="replace")
        except DeliverableError as e:
            missing.append(str(e))
            return m.group(0)
        css = re.sub(r"url\((https://fonts\.gstatic\.com/[^)\s]+)\)",
                     lambda f: f"url({embedded(f.group(1)) or f.group(1)})", css)
        return f"<style>{css}</style>"

    html = re.sub(r'(<img\b[^>]*\bsrc=")(https?://[^"]+)(")', image, html)
    html = re.sub(r"(<img\b[^>]*\bsrc=')(https?://[^']+)(')", image, html)
    html = re.sub(r'<link\b[^>]*\brel="stylesheet"[^>]*\bhref="(https://fonts\.googleapis\.com/[^"]+)"[^>]*>', stylesheet, html)
    return html, missing


def pages(agent: str, links: list[str]) -> list[tuple[str, str | None, str]]:
    """(link, address to fetch or None, name) for each of `links` that's one of the agent's PAGES, once per page."""
    found: dict[str, tuple[str, str | None, str]] = {}
    for link in links:
        for page in PAGES.get(agent, []):
            if hit := page.match(link):
                found.setdefault(hit[1], (link, *hit))
                break
    return list(found.values())


def saved_copies(run_dir: Path) -> dict[str, Path]:
    """The pages the channel already saved, by the link they were sent with, from the run's transcript:
    an HTML attachment of a message stands for the page link in that same message, in order."""
    transcript = run_dir / "transcript.jsonl"
    if not transcript.exists():
        return {}
    copies: dict[str, Path] = {}
    agent = None
    for line in transcript.read_text().splitlines():
        if not line.strip():
            continue
        m = json.loads(line)
        agent = agent or m.get("agent")
        html = [Path(a["path"]) for a in m.get("attachments") or [] if a.get("path") and a.get("mime_type") == "text/html"]
        for (link, _, _), path in zip(pages(agent, m.get("links") or []), html):
            copies.setdefault(link, path)
    return copies


def save(run_dir: Path, *, fetch: Fetch | None = None, again: bool = False,
         now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Saves the pages the run's agent linked to into deliverables/, and lists them in run.json.
    A page already in deliverables (by the link it came from) is saved again only with `again`.
    Returns the files saved and the pages that couldn't be, as [{source, error}]."""
    fetch = fetch or http_get
    info = runs.read_info(run_dir)
    listed = {d.get("source") for d in info.get("deliverables") or []}
    found = pages(info.get("agent"), info.get("links") or [])
    if not found:
        return [], []
    saved, unsaved = [], []
    copies = saved_copies(run_dir)
    for link, url, name in found:
        if link in listed and not again:
            continue
        try:
            if link in copies:
                html = read_copy(copies[link])
            elif url is None:
                raise DeliverableError(f"{link} needs Hark's sign-in, and the Hark channel hasn't saved a copy of it yet. "
                                       "Record it first: uv run bench sync hark")
            else:
                html = fetch(url)
        except DeliverableError as e:
            unsaved.append({"source": link, "error": str(e)})
            continue
        folder = run_dir / "deliverables"
        folder.mkdir(exist_ok=True)
        saved_at = runs.iso(now or datetime.now(UTC))
        for suffix, content in ((".html", html), (".txt", text(html))):
            (folder / f"{name}{suffix}").write_text(content)
            saved.append({"path": f"deliverables/{name}{suffix}", "source": link, "saved_at": saved_at})

    files = {d.get("path"): d for d in info.get("deliverables") or []} | {d["path"]: d for d in saved}
    info["deliverables"] = [files[p] for p in sorted(files)]
    # Pages with no copy at all, with why the last try failed
    errors = {d.get("source"): d for d in info.pop("unsaved_deliverables", None) or []} | {d["source"]: d for d in unsaved}
    have = {d.get("source") for d in info["deliverables"]}
    still = [errors[link] for link, _, _ in found if link in errors and link not in have]
    runs.write_info(run_dir, info | ({"unsaved_deliverables": still} if still else {}))
    return saved, unsaved


BREAK, GAP = "\x00", "\x01"  # a line break and a blank line between blocks; side by side they count once
BLOCKS = {"address", "article", "aside", "blockquote", "dd", "details", "dialog", "div", "dl", "dt",
          "fieldset", "figcaption", "figure", "footer", "form", "header", "hr", "li", "main", "nav",
          "ol", "pre", "summary", "table", "tr", "ul"}
GAPS = {"p", "section", "h1", "h2", "h3", "h4", "h5", "h6"}
UNSEEN = {"script", "style", "noscript", "template", "svg"}


class Text(HTMLParser):
    """A page's text, roughly as a browser shows it, with each link's address after it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title: list[str] = []
        self.where = ""         # "head", "title" or "" (the body)
        self.unseen = 0         # inside a script, style or the like
        self.href: tuple[str, int] | None = None  # the link we're inside, and where its text starts

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "head":
            self.where = "head"
        elif tag == "title" and self.where == "head":
            self.where = "title"
        elif tag == "body":
            self.where = ""
        elif tag in UNSEEN:
            self.unseen += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag in BLOCKS or tag in GAPS:
            self.out.append(GAP if tag in GAPS else BREAK)
        elif tag in ("td", "th"):
            self.out.append("\t")
        elif tag == "a" and (attrs.get("href") or "").startswith(("http://", "https://")):
            self.href = (attrs["href"], len(self.out))

    def handle_endtag(self, tag):
        if tag == "title" and self.where == "title":
            self.where = "head"
        elif tag == "head":
            self.where = ""
        elif tag in UNSEEN:
            self.unseen = max(0, self.unseen - 1)
        elif tag in BLOCKS or tag in GAPS:
            self.out.append(GAP if tag in GAPS else BREAK)
        elif tag == "a" and self.href:
            href, start = self.href
            label = re.sub(r"[\s\x00\x01]+", " ", "".join(self.out[start:])).strip()
            if label != href and label.rstrip("/") not in (href.rstrip("/"), href.split("://", 1)[1].rstrip("/")):
                self.out.append(f" ({href})")
            self.href = None

    def handle_data(self, data):
        if self.where == "title":
            self.title.append(data)
        elif not self.where and not self.unseen:
            self.out.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        joined = re.sub(r"[ \n\x00\x01]*[\x00\x01][ \n\x00\x01]*",
                        lambda m: "\n\n" if GAP in m[0] else "\n", "".join(self.out))
        body = "\n".join(line.strip() for line in joined.split("\n")).strip()
        title = re.sub(r"\s+", " ", "".join(self.title)).strip()
        return "\n\n".join(filter(None, [title, body])) + "\n"


def text(html: str) -> str:
    parser = Text()
    parser.feed(html)
    parser.close()
    return parser.text()
