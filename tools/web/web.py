"""
Module: web.py

Description:
    Fetches a web page and returns its main text as Markdown, for the model to read: trafilatura
    strips the menus, ads, scripts and styling, and keeps the headings, paragraphs, lists, tables
    and code. Plain text, JSON and XML are returned as they are. A front page or an index, a list of
    headlines rather than one article, is read with mode=links: its links, with their text.

    Key design points:
      - Only http and https; the page's size and the time to fetch it are limited (WEB_MAX_BYTES,
        WEB_TIMEOUT), and so is the text returned at once (WEB_MAX_CHARS): a longer text says where
        it stopped, and start= reads on from there.
      - A page that is not text (an image, a PDF, an archive) is refused, not downloaded.
      - A page whose text only JavaScript builds has none to extract; it says so. A page with little
        article text but many links says to read it with mode=links.
      - Started by the system's python3 like the other tools, it carries on under the agents' .venv
        python, which has trafilatura (tools/web/requirements.txt).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

# Import the tools' shared command line from the repository root (the nearest folder above
# holding pyproject.toml).
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "pyproject.toml").is_file())))
from tools.common.cli import ToolArgumentParser

VENV = Path(sys.path[0]) / ".venv"  # The agents' environment, in the repository root


def use_venv() -> None:
    """Carry on under the agents' .venv python, which has trafilatura, when started by another one."""
    python = VENV / "bin" / "python"
    if Path(sys.prefix) != VENV and python.exists():
        os.execv(python, [str(python), __file__, *sys.argv[1:]])


class WebPage:
    """
    Fetch a page and turn it into text the model can read.
    """

    VERSION = "1.0.0"
    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) agents-web/1.0"
    TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/json", "application/xml", "text/xml")
    HTML_TYPES = ("text/html", "application/xhtml+xml")
    MODES = ("text", "links")
    LINK_WORDS = 4  # A link with fewer words is a menu item or a button, not a headline
    LINK_CHARS = 200  # A link's text is cut there (a teaser around its headline)
    FEW_CHARS = 1500  # Article text this short, on a page with LINK_HINT links or more: suggest mode=links
    LINK_HINT = 20

    def __init__(self, timeout: Optional[float] = None, max_bytes: Optional[int] = None,
                 max_chars: Optional[int] = None) -> None:
        """
        Args:
            timeout: Seconds to wait for the server; None reads WEB_TIMEOUT (default 20).
            max_bytes: The largest page fetched; None reads WEB_MAX_BYTES (default 5,000,000).
            max_chars: The most text returned at once; None reads WEB_MAX_CHARS (default 12,000).
        """
        self.timeout = timeout or float(os.environ.get("WEB_TIMEOUT") or 20)
        self.max_bytes = max_bytes or int(os.environ.get("WEB_MAX_BYTES") or 5_000_000)
        self.max_chars = max_chars or int(os.environ.get("WEB_MAX_CHARS") or 12_000)

    def fetch(self, url: str) -> tuple[str, str, str]:
        """
        Fetch a page, following redirects.
        Args:
            url: An http or https address.
        Returns:
            tuple[str, str, str]: The final address, the content type (without parameters) and the
                page decoded as text.
        Raises:
            ValueError: If the address is not http(s), the server refuses or cannot be reached, the
                page is not text, or it is larger than max_bytes.
        """
        if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
            raise ValueError(f"Not a web address: {url!r}. Give a full http:// or https:// address.")
        request = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT,
                                                       "Accept": "text/html,text/plain,application/json,*/*;q=0.5"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                kind = response.headers.get_content_type()
                if kind not in self.HTML_TYPES + self.TEXT_TYPES and not kind.startswith("text/"):
                    raise ValueError(f"{response.url} is not a text page ({kind}); only web pages and text are read.")
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise ValueError(f"{response.url} is larger than {self.max_bytes:,} bytes (WEB_MAX_BYTES).")
                charset = response.headers.get_content_charset() or "utf-8"
                return response.url, kind, body.decode(charset, errors="replace")
        except urllib.error.HTTPError as error:
            raise ValueError(f"{url}: the server answered HTTP {error.code} ({error.reason}).") from None
        except urllib.error.URLError as error:
            raise ValueError(f"{url} could not be reached: {error.reason}.") from None
        except TimeoutError:
            raise ValueError(f"{url} did not answer within {self.timeout:g} seconds (WEB_TIMEOUT).") from None

    @staticmethod
    def extract(html: str, url: str, links: bool) -> tuple[str, str]:
        """
        Extract a page's main text as Markdown, and a heading for it (its title, and its site).
        Args:
            html: The page.
            url: Its address, for relative links and the metadata.
            links: Keep the links, as Markdown links.
        Returns:
            tuple[str, str]: The heading and the text; the text is "" when the page has none to extract.
        """
        import trafilatura  # Only here: the rest of the tool, and its tests, run without it

        text = trafilatura.extract(html, url=url, output_format="markdown", include_links=links,
                                   include_tables=True, include_comments=False, favor_recall=True) or ""
        meta = trafilatura.extract_metadata(html, default_url=url)
        title = (meta.title if meta and meta.title else "") or url
        site = meta.sitename if meta and meta.sitename else ""  # Not its date: trafilatura guesses one when there is none
        return f"# {title}" + (f"\n({site})" if site else ""), text.strip()

    @classmethod
    def link_list(cls, html: str, url: str) -> str:
        """
        List a page's links that read as headlines, as Markdown: for a front page or an index, whose
        main text is a list of articles. A link that holds a heading is named by it; menu items and
        buttons (fewer than LINK_WORDS words) are left out, and each address is listed once.
        Args:
            html: The page.
            url: Its address, for relative links.
        Returns:
            str: One "- [text](address)" line per link, in page order; "" when there are none.
        """
        import lxml.html  # trafilatura's own parser

        root = lxml.html.fromstring(html)
        for junk in root.xpath("//script | //style | //noscript"):
            junk.drop_tree()
        seen: dict[str, int] = {}
        entries: list[list[str]] = []
        for anchor in root.iter("a"):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            target = urljoin(url, href)
            headings = [" ".join(h.text_content().split()) for h in anchor.iter("h1", "h2", "h3", "h4", "h5", "h6")]
            text = next((h for h in headings if h), "") or " ".join(anchor.text_content().split())
            if len(text) > cls.LINK_CHARS:
                text = text[:cls.LINK_CHARS].rsplit(" ", 1)[0] + " …"
            if target in seen:  # The same article linked again (its picture, its teaser): keep the best text
                entry = entries[seen[target]]
                if len(text.split()) >= cls.LINK_WORDS and (headings or len(entry[0].split()) < cls.LINK_WORDS):
                    entry[0] = text
                continue
            seen[target] = len(entries)
            entries.append([text, target])
        names = set()
        lines = []
        for text, target in entries:
            if len(text.split()) >= cls.LINK_WORDS and text not in names:
                names.add(text)
                lines.append(f"- [{text.replace('[', '(').replace(']', ')')}]({target})")
        return "\n".join(lines)

    def read(self, url: str, start: int = 0, links: bool = False, mode: str = "text") -> str:
        """
        Fetch a page and return its text, max_chars at a time.
        Args:
            url: The address.
            start: Where in the text to begin, for a page longer than max_chars.
            links: Keep the links (HTML pages only).
            mode: "text", the page's main text; or "links", its links with their text, for a front page
                or an index of headlines (HTML pages only).
        Returns:
            str: The heading, the address read and the text; at the end, where it stopped when there
                is more.
        Raises:
            ValueError: If the page cannot be read (see `fetch`), or has no text.
        """
        if mode not in self.MODES:
            raise ValueError(f"Unknown mode '{mode}'. Use text or links.")
        final, kind, body = self.fetch(url)
        hint = ""
        if kind in self.HTML_TYPES and mode == "links":
            heading = self.extract(body, final, False)[0]
            text = self.link_list(body, final)
            if not text:
                raise ValueError(f"{final} has no links with text: the page may be built by JavaScript.")
        elif kind in self.HTML_TYPES:
            heading, text = self.extract(body, final, links)
            listed = self.link_list(body, final).count("\n") + 1 if len(text) < self.FEW_CHARS else 0
            if not text and listed < self.LINK_HINT:
                raise ValueError(f"{final} has no text to extract: the page may be built by JavaScript, or be "
                                 f"only a form or media.")
            if listed >= self.LINK_HINT:
                hint = (f"[Little article text, but {listed} links with text: a front page or an index. "
                        f"Call again with mode=links for its headlines.]")
        else:
            heading, text = f"# {final}", body.strip()
            if kind == "application/json":
                try:
                    text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
                except ValueError:
                    pass
        if start < 0 or (start and start >= len(text)):
            raise ValueError(f"start={start} is past the end of the text ({len(text):,} characters).")
        end = min(len(text), start + self.max_chars)
        if end < len(text):  # Stop at a line break when there is one near the limit
            cut = text.rfind("\n", start + self.max_chars // 2, end)
            end = cut + 1 if cut > 0 else end
        part = text[start:end].strip()
        lines = [heading, f"url: {final}", "", part] if part else [heading, f"url: {final}"]
        if start or end < len(text):
            more = f" Call again with start={end} for more." if end < len(text) else ""
            lines += ["", f"[Characters {start:,} to {end:,} of {len(text):,}.{more}]"]
        if hint:
            lines += ["", hint]
        return "\n".join(lines)

    @classmethod
    def build_parser(cls) -> ToolArgumentParser:
        """
        The command line: "--url=<address> [--start=<n>] [--links=true] [--mode=text|links]".
        Returns:
            ToolArgumentParser: The parser.
        """
        parser = ToolArgumentParser("web", cls.VERSION, "Read a web page as Markdown text.")
        parser.add_argument("--url", required=True, help="The page's http(s) address")
        parser.add_argument("--start", type=int, default=0, help="Where in the text to begin (a long page)")
        parser.add_argument("--links", type=ToolArgumentParser.boolean, default=False, help="Keep the links")
        parser.add_argument("--mode", default="text", help="text (default) or links: the links with their text")
        return parser


def main(argv: Optional[list[str]] = None) -> int:
    """
    Parse the command line and print the page's text, or "Error: <reason>".
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        int: 0 on success, 1 when the page cannot be read or an argument is wrong.
    """
    try:
        args = WebPage.build_parser().parse_args(argv)
        print(WebPage().read(args.url, args.start, args.links, args.mode))
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    use_venv()
    raise SystemExit(main())
