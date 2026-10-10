"""
Module: web.py

Description:
    Fetches a web page and returns its main text as Markdown, for the model to read: trafilatura
    strips the menus, ads, scripts and styling, and keeps the headings, paragraphs, lists, tables
    and code. Plain text, JSON and XML are returned as they are.

    Key design points:
      - Only http and https; the page's size and the time to fetch it are limited (WEB_MAX_BYTES,
        WEB_TIMEOUT), and so is the text returned at once (WEB_MAX_CHARS): a longer text says where
        it stopped, and start= reads on from there.
      - A page that is not text (an image, a PDF, an archive) is refused, not downloaded.
      - A page whose text only JavaScript builds has none to extract; it says so.
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
from urllib.parse import urlparse

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

    def read(self, url: str, start: int = 0, links: bool = False) -> str:
        """
        Fetch a page and return its text, max_chars at a time.
        Args:
            url: The address.
            start: Where in the text to begin, for a page longer than max_chars.
            links: Keep the links (HTML pages only).
        Returns:
            str: The heading, the address read and the text; at the end, where it stopped when there
                is more.
        Raises:
            ValueError: If the page cannot be read (see `fetch`), or has no text.
        """
        final, kind, body = self.fetch(url)
        if kind in self.HTML_TYPES:
            heading, text = self.extract(body, final, links)
            if not text:
                raise ValueError(f"{final} has no text to extract: the page may be built by JavaScript, or be "
                                 f"only a form or media.")
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
        lines = [heading, f"url: {final}", "", part]
        if start or end < len(text):
            more = f" Call again with start={end} for more." if end < len(text) else ""
            lines += ["", f"[Characters {start:,} to {end:,} of {len(text):,}.{more}]"]
        return "\n".join(lines)

    @classmethod
    def build_parser(cls) -> ToolArgumentParser:
        """
        The command line: "--url=<address> [--start=<n>] [--links=true]".
        Returns:
            ToolArgumentParser: The parser.
        """
        parser = ToolArgumentParser("web", cls.VERSION, "Read a web page as Markdown text.")
        parser.add_argument("--url", required=True, help="The page's http(s) address")
        parser.add_argument("--start", type=int, default=0, help="Where in the text to begin (a long page)")
        parser.add_argument("--links", type=ToolArgumentParser.boolean, default=False, help="Keep the links")
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
        print(WebPage().read(args.url, args.start, args.links))
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    use_venv()
    raise SystemExit(main())
