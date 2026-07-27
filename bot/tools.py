"""
The tools the agent is allowed to call. Kept intentionally small:

  web_search(query) -> finds candidate URLs when the question doesn't hand
                        one over directly (e.g. "based on MOSPI data" with
                        no link) - uses DuckDuckGo's HTML endpoint, no API
                        key needed.
  fetch_url(url)    -> pulls a page/CSV/JSON from the public internet
                        (MOSPI pages, raw GitHub CSVs, Wikipedia tables, etc.)
  run_python(code)  -> runs short pandas/numpy snippets against data the
                        agent already has in its own working memory, and
                        returns whatever was printed.

All three are deliberately simple rather than "safe against a hostile
user" - the only caller is our own agent loop reacting to a trusted
grading account, not arbitrary internet traffic. Still, run_python strips
out the obviously dangerous builtins so a wayward LLM step can't shell out
or touch the filesystem.
"""
from __future__ import annotations

import builtins
import contextlib
import io
import json
import multiprocessing
import re
import traceback

import requests

from . import config

MAX_FETCH_BYTES = 400_000  # keep tool results small enough for the LLM context
FETCH_TIMEOUT = 15
SEARCH_TIMEOUT = 15

_SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "float", "format",
        "int", "len", "list", "max", "min", "print", "range", "repr",
        "round", "set", "sorted", "str", "sum", "tuple", "zip", "Exception",
        "ValueError", "TypeError", "KeyError", "IndexError", "isinstance",
    )
}

_RESULT_LINK_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


def web_search(query: str) -> str:
    """Search the web via DuckDuckGo's HTML endpoint (no API key required)
    and return the top results as 'title - url' lines. Use this to find a
    data source when the question doesn't give you a URL directly."""
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            timeout=SEARCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (tds-p1-data-analyst-bot/1.0)"},
        )
        resp.raise_for_status()
        matches = _RESULT_LINK_RE.findall(resp.text)[:8]
        if not matches:
            return "No results found - try a different, more specific query."
        lines = []
        for href, title_html in matches:
            title = _TAG_RE.sub("", title_html).strip()
            lines.append(f"{title} - {href}")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"SEARCH_ERROR: {exc}"


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def fetch_url(url: str) -> str:
    """Fetch a URL and return text content, truncated to a safe size."""
    try:
        resp = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": "tds-p1-data-analyst-bot/1.0"},
        )
        resp.raise_for_status()
        content = resp.text
        # Strip <script>/<style> blocks for HTML responses - they're pure
        # noise for our purposes and, on content-heavy pages like Wikipedia,
        # can eat a large share of the byte budget before any real table or
        # text content shows up. pd.read_html() doesn't need them either.
        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type or content.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            content = _SCRIPT_STYLE_RE.sub("", content)
        if len(content) > MAX_FETCH_BYTES:
            content = content[:MAX_FETCH_BYTES] + "\n...[truncated]"
        return content
    except Exception as exc:  # noqa: BLE001
        return f"FETCH_ERROR: {exc}"


def _run_in_subprocess(code: str, queue: multiprocessing.Queue) -> None:
    import re  # noqa: F401

    import numpy as np  # noqa: F401
    import pandas as pd  # noqa: F401

    safe_globals = {
        "__builtins__": _SAFE_BUILTINS,
        "pd": pd,
        "np": np,
        "json": json,
        "io": io,
        "re": re,
    }
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, safe_globals)  # noqa: S102
        queue.put({"ok": True, "stdout": buf.getvalue()})
    except Exception:  # noqa: BLE001
        queue.put({"ok": False, "stdout": buf.getvalue(), "error": traceback.format_exc()})


def run_python(code: str, timeout: int = 20) -> str:
    """
    Execute a short Python snippet with pandas/numpy available and return
    whatever it printed. The snippet must print its result — there is no
    implicit "last expression" return, to keep behaviour predictable for
    the model.
    """
    queue: multiprocessing.Queue = multiprocessing.Queue()
    proc = multiprocessing.Process(target=_run_in_subprocess, args=(code, queue))
    proc.start()
    proc.join(timeout)

    if proc.is_alive():
        proc.terminate()
        proc.join()
        return "RUN_ERROR: execution timed out"

    if queue.empty():
        return "RUN_ERROR: process exited without producing output (crashed?)"

    result = queue.get()
    if result["ok"]:
        return result["stdout"] or "(no output — remember to print() your result)"
    return f"RUN_ERROR:\n{result['error']}\nPartial stdout:\n{result['stdout']}"


TOOL_SPECS = [
    {
        "name": "web_search",
        "description": (
            "Search the web (DuckDuckGo) and get back a list of 'title - url' results. "
            "Use this FIRST when the question references a dataset or topic (e.g. "
            "'based on MOSPI data') without giving you a specific URL. Prefer picking "
            "an official/authoritative-looking result (government sites, Wikipedia, "
            "well-known data repositories) to fetch_url next."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch a public URL (webpage, CSV, JSON) and return its text content. "
            "Use this to pull MOSPI datasets or any other public data referenced in the question, "
            "including URLs you found via web_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch."}
            },
            "required": ["url"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run a Python snippet. `pd` (pandas), `np` (numpy), `json`, `io`, and `re` are "
            "already bound in scope — do NOT write `import` statements, they are blocked for "
            "safety and will error (these five are the only modules available). "
            "To parse CSV text you fetched with fetch_url, use pd.read_csv(io.StringIO(csv_text)). "
            "To parse a table out of a fetched HTML page (e.g. a Wikipedia article), use "
            "pd.read_html(io.StringIO(html_text)) - the io.StringIO() wrapper is required, a raw "
            "string gets misread as a file path and errors. It returns a list of DataFrames, one "
            "per <table> found; this is almost always easier and more reliable than regex for "
            "HTML tables. Use `re` only for text that isn't tabular. You must print() whatever "
            "you want returned — there is no implicit return value. Use this to parse fetched "
            "data and compute the final answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."}
            },
            "required": ["code"],
        },
    },
]

TOOL_IMPLS = {
    "web_search": lambda args: web_search(args["query"]),
    "fetch_url": lambda args: fetch_url(args["url"]),
    "run_python": lambda args: run_python(args["code"], timeout=config.TOOL_TIMEOUT_SECONDS),
}
