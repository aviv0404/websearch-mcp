#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["zeromcp", "simple-reddit-mcp", "httpx", "trafilatura", "fastembed", "pypdf"]
# ///
"""Local web search MCP server (stdio).

web_search: fans each query out to SearXNG (Google CSE, Bing, Yandex) and to
Brave Search through FlareSolverr's real browser (Google CSE and Brave exit via a
Cloudflare WARP proxy to spread rate limits off the home IP), de-duplicates, and ranks
everything with a local cross-encoder. web_read: page -> clean markdown, with
FlareSolverr as the fallback for pages that block bots, Reddit via the Arctic
Shift archive, and question-focused passage selection for long pages.
"""

import asyncio
import datetime
import html
import io
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx
import trafilatura
from simple_reddit_mcp.server import get_thread
from zeromcp import McpServer, McpToolError

SEARXNG = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")
FLARESOLVERR = os.environ.get("FLARESOLVERR_URL", "http://127.0.0.1:8191")
RERANKER = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3-int8")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
TIME_RANGES = {"": "", "day": "pd", "week": "pw", "month": "pm", "year": "py"}  # SearXNG name -> Brave tf

mcp = McpServer("websearch", "0.1.0")
pool = ThreadPoolExecutor(16)
_encoder, _encoder_lock = None, threading.Lock()
BRAVE_PAUSE_FILE = "/tmp/websearch-brave-paused-until"  # written after a Brave captcha so we stop poking it


def rerank(query: str, texts: list[str]) -> list[float]:
    global _encoder
    with _encoder_lock:
        if _encoder is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            # Not fastembed's default /tmp cache: that's tmpfs on many distros, so the 560MB model re-downloaded every boot.
            cache = os.environ.get("FASTEMBED_CACHE_PATH", os.path.expanduser("~/.cache/fastembed"))
            _encoder = TextCrossEncoder(RERANKER, cache_dir=cache)
        return list(_encoder.rerank(query, texts))


def strip_tags(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def browser_get(url: str, proxy: str = "") -> tuple[int, str]:
    # Worst case (15s direct fetch + 30s here + rerank) stays under the 60s tool timeout many harnesses (e.g. Codex) use.
    req = {"cmd": "request.get", "url": url, "maxTimeout": 20000} | ({"proxy": {"url": proxy}} if proxy else {})
    r = httpx.post(f"{FLARESOLVERR}/v1", json=req, timeout=30).json()
    if r.get("status") != "ok":
        raise RuntimeError(r.get("message", "flaresolverr failed"))
    return r["solution"]["status"], r["solution"]["response"]


def searxng(query: str, time_range: str) -> list[dict]:
    # language=en: otherwise Google CSE localises snippets/dates to the IP's country (Hebrew here).
    params = {"q": query, "format": "json", "categories": "general", "language": "en"}
    get = lambda p: httpx.get(f"{SEARXNG}/search", params=p, timeout=25).raise_for_status().json()
    data, tag = get(params | ({"time_range": time_range} if time_range else {})), set()
    if time_range and not data.get("results"):
        # Only Google CSE supports time_range (SearXNG 2026.9); while it's rate-limited, fall back to labelled unfiltered results.
        data, tag = get(params), {"NOT date-filtered"}
    if not data.get("results"):
        raise RuntimeError("no results; " + "; ".join(f"{e}: {why}" for e, why in data.get("unresponsive_engines", [])))
    return [
        {"url": x["url"], "title": x.get("title", ""), "snippet": x.get("content", ""),
         "date": (x.get("publishedDate") or "")[:10], "src": set(x.get("engines", [])) | tag}
        for x in data["results"]
    ]


def brave(query: str, time_range: str) -> list[dict]:
    try:  # the pause lives in a file so every harness's server process honours it
        paused_until = float(open(BRAVE_PAUSE_FILE).read())
    except (OSError, ValueError):
        paused_until = 0.0
    if time.time() < paused_until:
        raise RuntimeError("paused after a captcha, retrying after " + time.strftime("%H:%M", time.localtime(paused_until)))
    params = {"q": query, "source": "web"} | ({"tf": TIME_RANGES[time_range]} if time_range else {})
    # Via the WARP container (name resolved inside FlareSolverr's network): Brave captchas the home IP after ~20 queries.
    status, page = browser_get("https://search.brave.com/search?" + urlencode(params), proxy="socks5://warp:1080")
    out = []
    # ponytail: scrapes Brave's result HTML; if Brave changes markup this returns [] and SearXNG still answers.
    for block in page.split('data-type="web"')[1:]:
        link = re.search(r'<a href="(https?://[^"]+)"', block)
        if not link:
            continue
        title = re.search(r'class="title[^"]*"[^>]*title="([^"]*)"', block)
        body = re.search(r'class="generic-snippet.*?<div class="content[^"]*">(.*?)</div>', block, re.S)
        date = re.search(r'<span class="t-secondary">([^<]{1,40}?)\s*-\s*</span>', block)
        snippet = strip_tags(body.group(1)) if body else ""
        if date:
            snippet = snippet.removeprefix(date.group(1)).lstrip(" -")
        out.append({"url": html.unescape(link.group(1)), "title": html.unescape(title.group(1)) if title else "",
                    "snippet": snippet, "date": date.group(1) if date else "", "src": {"brave"}})
    if not out and "captcha" in page.lower():
        # ponytail: fixed 1h pause (Brave's flag lasted >1h on 2026-11-02); back off exponentially if retries keep re-flagging.
        with open(BRAVE_PAUSE_FILE, "w") as f:
            f.write(str(time.time() + 3600))
        raise RuntimeError("captcha (rate-limited); pausing Brave for 1h")
    if not out and status != 200:
        raise RuntimeError(f"brave returned HTTP {status}")
    return out


def url_key(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url.split("#")[0]).rstrip("/").lower()


@mcp.tool(read_only=True, open_world=True)
def web_search(
    queries: Annotated[list[str], "1-6 differently-worded queries for ONE information need. Put the full natural-language question first (it is also what results are ranked against), then variants such as '<topic> 2026', '<topic> reddit', '<topic> docs', '<a> vs <b>'."],
    time_range: Annotated[str, "Only results from the last 'day', 'week', 'month' or 'year'. Empty = any time."] = "",
    max_results: Annotated[int, "Number of ranked results to return, 1-30."] = 10,
) -> str:
    """Search the web: Google, Bing and Yandex (via SearXNG) plus Brave, merged and ranked by a local reranker.

    Snippets are short and often stale or incomplete - call web_read on the most promising URLs before answering.
    """
    queries = [q.strip() for q in queries if q.strip()][:6]
    if not queries:
        raise McpToolError("`queries` must contain at least one non-empty query.")
    if time_range not in TIME_RANGES:
        raise McpToolError("`time_range` must be '', 'day', 'week', 'month' or 'year'.")
    max_results = min(max(max_results, 1), 30)

    # Brave only gets the main question: bursts of parallel queries trip its captcha.
    jobs = {pool.submit(searxng, q, time_range): f"searxng({q!r})" for q in queries}
    jobs[pool.submit(brave, queries[0], time_range)] = f"brave({queries[0]!r})"
    merged: dict[str, dict] = {}
    errors = []
    for job, label in jobs.items():
        try:
            for r in job.result():
                seen = merged.setdefault(url_key(r["url"]), r)
                if seen is not r:
                    seen["src"] |= r["src"]
                    seen["date"] = seen["date"] or r["date"]
                    if len(r["snippet"]) > len(seen["snippet"]):
                        seen["snippet"] = r["snippet"]
        except Exception as e:
            errors.append(f"{label}: {e}")
    if not merged:
        raise McpToolError("No results. Errors: " + "; ".join(errors or ["none"]))

    results = list(merged.values())
    scores = rerank(queries[0], [f"{r['title']}\n{r['snippet']}" for r in results])
    ranked = sorted(zip(scores, results), key=lambda x: x[0], reverse=True)[:max_results]

    lines = [f"Today is {datetime.date.today()}. {len(results)} unique results from {len(jobs)} searches; top {len(ranked)}:"]
    for i, (_, r) in enumerate(ranked, 1):
        meta = " · ".join(filter(None, [r["date"], ", ".join(sorted(r["src"]))]))
        lines.append(f"\n{i}. {r['title']}\n   {r['url']}\n   {meta}\n   {r['snippet']}")
    if errors:
        lines.append("\n(some searches failed: " + "; ".join(errors)[:500] + ")")
    return "\n".join(lines)


def extract(page: str, url: str) -> str:
    return trafilatura.extract(page, url=url, output_format="markdown", with_metadata=True,
                               include_tables=True, include_links=False, favor_recall=True) or ""


def fetch_markdown(url: str) -> str:
    text = ""
    try:
        r = httpx.get(url, headers=HEADERS, follow_redirects=True, timeout=15)
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and "pdf" in ctype:
            from pypdf import PdfReader
            return "\n\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(r.content)).pages)
        if r.status_code == 200 and "html" not in ctype and re.search(r"text|json|xml|markdown", ctype):
            return r.text
        if r.status_code == 200:
            text = extract(r.text, url)
    except httpx.HTTPError:
        pass
    # Blocked (403/429/CAPTCHA), JS-rendered, or near-empty: retry in a real browser.
    if len(text) < 1500:
        try:
            text = max(text, extract(browser_get(url)[1], url), key=len)
        except Exception as e:
            if not text:
                raise McpToolError(f"Could not read {url}: {e}") from None
    return text


def best_passages(text: str, question: str, max_chars: int) -> str:
    head, sep, body = text.partition("\n---\n") if text.startswith("---\n") else ("", "", text)
    chunks, cur = [], ""
    for para in body.split("\n\n"):
        if cur and len(cur) + len(para) > 1200:
            chunks.append(cur)
            cur = ""
        cur += para + "\n\n"
    chunks.append(cur)
    # The reranker costs ~1s per 10 chunks on CPU, so huge pages are first cut to 40 chunks by IDF-weighted keyword overlap.
    # ponytail: misses passages sharing no words with the question; use embeddings for the prefilter if that bites.
    cand = list(range(len(chunks)))
    if len(chunks) > 40:
        terms = set(re.findall(r"\w{3,}", question.lower()))
        sets = [set(re.findall(r"\w+", c.lower())) for c in chunks]
        idf = {t: math.log(len(chunks) / (1 + sum(t in s for s in sets))) for t in terms}
        cand = sorted(cand, key=lambda i: -sum(idf[t] for t in terms & sets[i]))[:40]
    # Scores each chunk's first 1500 chars: one 5k-char chunk pads the whole batch and made reranking ~10x slower.
    scores = rerank(question, [chunks[i][:1500] for i in cand])
    keep, used = set(), len(head)
    for score, i in sorted(zip(scores, cand), reverse=True):
        if used + len(chunks[i]) > max_chars:
            continue
        keep.add(i)
        used += len(chunks[i])
    picked = "\n[…]\n\n".join(chunks[i].strip() for i in sorted(keep))
    return f"{head}{sep}\n(Passages most relevant to {question!r}, in page order; {len(text)} chars total.)\n\n{picked}"


@mcp.tool(read_only=True, open_world=True)
def web_read(
    url: Annotated[str, "http(s) URL to read (web page, PDF, plain text, or Reddit thread)."],
    question: Annotated[str, "What you need from the page. Long pages are cut down to the passages most relevant to it."] = "",
    max_chars: Annotated[int, "Character budget for the returned text."] = 12000,
) -> str:
    """Read a URL as clean markdown (main content only, with title/date metadata when available).

    Uses a real browser automatically for pages that block bots. Reddit threads come from the Arctic Shift archive.
    """
    host = urlsplit(url).hostname or ""
    if not url.startswith(("http://", "https://")) or not host:
        raise McpToolError("`url` must be an absolute http(s) URL.")
    if re.search(r"(^|\.)(reddit\.com|redd\.it)$", host):
        return get_thread(url, comment_limit=300, max_chars=max_chars)
    text = fetch_markdown(url)
    if len(text) <= max_chars:
        return text
    if question:
        return best_passages(text, question, max_chars)
    return text[:max_chars] + f"\n\n[truncated: {len(text)} chars total; pass `question` to get the relevant passages instead]"


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:  # live smoke test: ./websearch.py --selfcheck
        out = web_search(["what is the capital city of France"], max_results=5)
        assert "\n1. " in out and "paris" in out.lower(), out
        print(out.splitlines()[0], out[out.find("(some searches failed"):] if "(some searches failed" in out else "")
        out = web_search(["latest Python release news"], time_range="month", max_results=3)
        assert "\n1. " in out, out  # date-filtered search must not come back empty when Google CSE is rate-limited
        print("time_range=month:", "unfiltered fallback" if "NOT date-filtered" in out else "filtered")
        t = time.time()
        out = web_read("https://docs.python.org/3/whatsnew/3.14.html", "free-threaded build changes", 6000)
        assert "Passages most relevant" in out and "free-thread" in out.lower() and len(out) < 7000, out[:500]
        print(f"web_read long page + question: {time.time() - t:.1f}s")
        assert "Attention" in web_read("https://arxiv.org/pdf/1706.03762", max_chars=3000)
        print("selfcheck ok")
        sys.exit()
    print("websearch MCP: serving web_search and web_read over stdio", file=sys.stderr)
    asyncio.run(mcp.stdio_async())
