# Web

Reads a web page and returns its main text as Markdown: headings, paragraphs, lists, tables and code, without the menus,
ads, scripts and styling around them. [trafilatura](https://trafilatura.readthedocs.io/) does the extraction, so the
model reads a few thousand tokens of text instead of a page of HTML. Plain text and JSON come back as they are.

From the repository root:

~~~bash
python3 tools/web/web.py --url=https://docs.python.org/3/library/json.html
python3 tools/web/web.py --url=https://docs.python.org/3/library/json.html --start=12000
python3 tools/web/web.py --url=https://example.com --links=true
~~~

The tool starts under the system's `python3` like the others and carries on under the agents' `.venv` Python, which has
trafilatura (`requirements.txt` here; `install.sh` installs it).

## Limits

The manifest's `env` sets them:

| Variable        | Default   | Meaning                                                 |
|-----------------|-----------|---------------------------------------------------------|
| `WEB_TIMEOUT`   | 20        | Seconds to wait for the server                          |
| `WEB_MAX_BYTES` | 5,000,000 | The largest page fetched; a larger one is refused       |
| `WEB_MAX_CHARS` | 12,000    | The most text returned at once; `start` reads on        |

A longer text ends with a line such as `[Characters 0 to 11,874 of 25,518. Call again with start=11874 for more.]`.

Only `http` and `https` addresses are read. A page that is not text, such as an image, a PDF or an archive, is refused
without being downloaded. A page whose text only JavaScript builds has nothing to extract, and the tool says so.

The tool runs as the current user, outside the shell's sandbox, so it can reach any address this machine can, including
services on the local network. The shell itself has no network.
