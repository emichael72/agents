"""
Offline tests for the web tool: a local HTTP server stands in for the web. A page's main text comes
back as Markdown without its menus and scripts, a long text in parts, and what is not text is refused.
Run from the repository root:
    .venv/bin/python -m unittest discover -s tools/web/tests
"""

import io
import json
import re
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.web.web import WebPage, main

ARTICLE = "".join(f"<p>Paragraph {n} explains how a recursive tree flip uses one stack frame per level.</p>"
                  for n in range(40))
PAGES = {  # path: (content type, body)
    "/article": ("text/html; charset=utf-8", f"""<html><head><title>Tree flips</title><script>var tracker = 1;</script>
        </head><body><nav><a href="/">Home</a> | <a href="/shop">Shop</a></nav><article><h1>Tree flips</h1>
        <p>A <a href="/recursion">recursive</a> flip is short.</p>{ARTICLE}<pre><code>flip(node->left);</code></pre>
        </article><footer>Copyright and cookie notice</footer></body></html>"""),
    "/notes.txt": ("text/plain", "line one\nline two\n"),
    "/data.json": ("application/json", '{"name": "agents", "stars": 3}'),
    "/logo.png": ("image/png", "\x89PNG not really"),
    "/app": ("text/html", "<html><body><div id=root></div><script>render()</script></body></html>"),
}


class Handler(BaseHTTPRequestHandler):
    """Serves PAGES; any other path is a 404."""

    def do_GET(self):
        kind, body = PAGES.get(self.path, (None, None))
        if kind is None:
            self.send_error(404, "Not Found")
            return
        data = body.encode("latin-1" if kind == "image/png" else "utf-8")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


class WebPageTests(unittest.TestCase):
    """The tool against the local server."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_page_is_its_main_text_as_markdown(self):
        text = WebPage(max_chars=100_000).read(self.base + "/article")
        self.assertTrue(text.startswith("# Tree flips\n"))
        self.assertIn(f"url: {self.base}/article", text)
        self.assertIn("Paragraph 39 explains", text)
        self.assertIn("flip(node->left);", text)
        for noise in ("tracker", "Shop", "cookie notice"):  # Scripts, menus and footers are left out
            self.assertNotIn(noise, text)
        self.assertNotIn("](", text)  # No links unless asked for
        self.assertIn("/recursion)", WebPage(max_chars=100_000).read(self.base + "/article", links=True))

    def test_a_long_text_comes_in_parts(self):
        page = WebPage(max_chars=1000)
        first = page.read(self.base + "/article")
        start = int(first.rsplit("start=", 1)[1].split(" ")[0])
        self.assertLessEqual(start, 1000)
        self.assertIn("[Characters 0 to", first)
        second = page.read(self.base + "/article", start=start)
        self.assertIn(f"[Characters {start:,} to", second)
        self.assertNotEqual(first.split("\n\n")[1][:50], second.split("\n\n")[1][:50])

    def test_text_and_json_come_back_as_they_are(self):
        self.assertTrue(WebPage().read(self.base + "/notes.txt").endswith("line one\nline two"))
        data = WebPage().read(self.base + "/data.json").split("\n\n", 1)[1]
        self.assertEqual(json.loads(data), {"name": "agents", "stars": 3})

    def test_what_cannot_be_read_says_why(self):
        cases = {"/logo.png": "is not a text page (image/png)", "/missing": "HTTP 404",
                 "/app": "no text to extract: the page may be built by JavaScript"}
        for path, reason in cases.items():
            with self.assertRaisesRegex(ValueError, re.escape(reason)):
                WebPage().read(self.base + path)
        with self.assertRaisesRegex(ValueError, "Not a web address"):
            WebPage().read("file:///etc/passwd")
        with self.assertRaisesRegex(ValueError, "larger than 100 bytes"):
            WebPage(max_bytes=100).read(self.base + "/article")
        with self.assertRaisesRegex(ValueError, "past the end"):
            WebPage().read(self.base + "/notes.txt", start=500)

    def test_the_command_line_prints_the_text_or_the_error(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([f"--url={self.base}/notes.txt"]), 0)
            self.assertEqual(main(["--url=ftp://example.com"]), 1)
        self.assertIn("line two\nError: Not a web address: 'ftp://example.com'", out.getvalue())


if __name__ == "__main__":
    unittest.main()
