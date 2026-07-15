import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..database import get_db, get_service_db
from .embeddings import get_embedding

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Shared HTTP client (Task 13) — lazy-initialised, closed in lifespan shutdown
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            follow_redirects=True, headers=_HEADERS, timeout=30
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# Shared headless browser — lazy-launched on first use, closed in lifespan shutdown.
# Used as a fallback for pages that render their content client-side via JS, where a
# plain HTTP fetch only gets back an empty shell.
_playwright = None
_browser = None


async def get_browser():
    global _playwright, _browser
    if _browser is None or not _browser.is_connected():
        from playwright.async_api import async_playwright
        if _playwright is None:
            _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def close_browser() -> None:
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


async def fetch_page_with_browser(url: str, timeout: int = 30) -> str:
    """Render a page in a real (headless) browser and return the resulting HTML.

    For sites whose content is populated by client-side JavaScript rather than
    present in the initial HTML response. Uses the same browser identity as the
    plain HTTP fetcher (_HEADERS) — no automation-fingerprint evasion.
    """
    browser = await get_browser()
    context = await browser.new_context(
        user_agent=_HEADERS["User-Agent"],
        viewport={"width": 1280, "height": 900},
    )
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        await page.wait_for_timeout(1500)  # let client-side rendering settle
        return await page.content()
    finally:
        await context.close()


def validate_url(url: str) -> None:
    """Raise ValueError if the URL could be an SSRF vector."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme '{parsed.scheme}' is not allowed; use http or https.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname.")
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname '{hostname}': {exc}")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise ValueError(
                f"URL resolves to a non-public address ({ip}) and is not allowed."
            )


async def fetch_page(url: str, timeout: int = 30) -> str:
    response = await get_http_client().get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_content(html: str) -> tuple[str, str]:
    """Return (title, clean_text) extracted from raw HTML."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(
        ["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript", "svg"]
    ):
        tag.decompose()

    # Title
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    elif soup.find("h1"):
        title = soup.find("h1").get_text(strip=True)

    # Main content area (best-effort extraction)
    content_elem = (
        soup.find("article")
        or soup.find("main")
        or soup.find(id=re.compile(r"content|main|article", re.I))
        or soup.find(class_=re.compile(r"content|main|article|post|body", re.I))
        or soup.body
        or soup
    )

    text = content_elem.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return title, text


def chunk_text(text: str, chunk_size: int = 1500, overlap: int = 200) -> list[str]:
    """Split text into overlapping fixed-size chunks."""
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += chunk_size - overlap
    return chunks


_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SKIP_EXTENSIONS = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|webp|pdf|zip|mp4|mp3|css|js|ico|woff|woff2|ttf|eot)$", re.I
)


def extract_links(html: str, base_url: str) -> list[str]:
    """Return unique same-domain HTML-page links found on a page."""
    soup = BeautifulSoup(html, "lxml")
    base_parsed = urlparse(base_url)
    seen: set[str] = set()
    links: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        resolved = urljoin(base_url, href)
        p = urlparse(resolved)
        if (
            p.netloc == base_parsed.netloc
            and p.scheme in ("http", "https")
            and not _SKIP_EXTENSIONS.search(p.path)
        ):
            clean = p._replace(fragment="").geturl()
            if clean not in seen:
                seen.add(clean)
                links.append(clean)

    return links


async def _fetch_text(url: str, timeout: int = 10) -> str | None:
    """Fetch raw text from a URL, returning None on any error."""
    try:
        r = await get_http_client().get(url, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None


def _parse_sitemap_xml(xml_text: str) -> tuple[list[str], list[str]]:
    """
    Parse a sitemap or sitemap-index XML.
    Returns (page_urls, sub_sitemap_urls).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []

    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    ns = {"sm": _SITEMAP_NS}

    if tag == "sitemapindex":
        sub = [el.text.strip() for el in root.findall("sm:sitemap/sm:loc", ns) if el.text]
        return [], sub
    else:
        pages = [el.text.strip() for el in root.findall("sm:url/sm:loc", ns) if el.text]
        return pages, []


async def discover_sitemap_urls(base_url: str, max_urls: int = 50) -> list[str]:
    """
    Attempt to discover all page URLs via the site's sitemap.
    Checks /sitemap.xml first, then robots.txt as fallback.
    Returns a list of HTML page URLs (capped at max_urls), or [] if no sitemap found.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # 1. Candidate sitemap URLs
    candidates: list[str] = [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]

    robots = await _fetch_text(f"{origin}/robots.txt")
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                url = line.split(":", 1)[1].strip()
                if url not in candidates:
                    candidates.insert(0, url)  # robots.txt hint takes priority

    # 2. Fetch and parse each candidate
    all_pages: list[str] = []
    visited_sitemaps: set[str] = set()

    async def process_sitemap(sitemap_url: str) -> None:
        if sitemap_url in visited_sitemaps or len(all_pages) >= max_urls:
            return
        visited_sitemaps.add(sitemap_url)
        xml_text = await _fetch_text(sitemap_url)
        if not xml_text:
            return
        pages, sub_sitemaps = _parse_sitemap_xml(xml_text)
        for p in pages:
            if len(all_pages) >= max_urls:
                break
            # Only include same-domain HTML pages
            if urlparse(p).netloc == parsed.netloc and not _SKIP_EXTENSIONS.search(p):
                all_pages.append(p)
        for sub in sub_sitemaps:
            await process_sitemap(sub)

    for candidate in candidates:
        if len(all_pages) >= max_urls:
            break
        await process_sitemap(candidate)

    return all_pages[:max_urls]


async def _store_content_chunks(
    source_id: str, url: str, title: str, content: str,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> int:
    """Chunk, embed, and upsert content. Returns number of new chunks stored."""
    chunks = chunk_text(content)
    db = get_service_db()
    stored = 0
    now = datetime.now(timezone.utc).isoformat()

    # ── Batch existence check (Task 10) ──────────────────────────────────────
    all_hashes = [hashlib.sha256(c.encode()).hexdigest() for c in chunks]
    existing_rows = await asyncio.to_thread(
        lambda: db.table("scraped_content")
        .select("id, content_hash")
        .eq("source_id", source_id)
        .in_("content_hash", all_hashes)
        .execute()
    )
    existing_map: dict[str, str] = {
        row["content_hash"]: row["id"] for row in (existing_rows.data or [])
    }

    # Bulk-update last_seen_at for existing chunks
    update_payload: dict = {"last_seen_at": now}
    if session_id:
        update_payload["session_id"] = session_id
    for row_id in existing_map.values():
        try:
            await asyncio.to_thread(
                lambda rid=row_id: db.table("scraped_content")
                .update(update_payload)
                .eq("id", rid)
                .execute()
            )
        except Exception as exc:
            logger.warning("Could not update last_seen_at for chunk in %s: %s", url, exc)

    # Insert only new chunks
    semaphore = asyncio.Semaphore(5)

    async def _embed_and_insert(i: int, chunk: str, content_hash: str) -> None:
        nonlocal stored
        async with semaphore:
            try:
                embedding = await get_embedding(chunk)
            except Exception as exc:
                logger.error("Embedding failed for chunk %d of %s: %s", i, url, exc)
                return
            record = {
                "source_id": source_id,
                "url": url,
                "title": title if i == 0 else f"{title} (part {i + 1})",
                "content": chunk,
                "content_hash": content_hash,
                "embedding": embedding,
                "last_seen_at": now,
                "metadata": {
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "char_count": len(chunk),
                },
            }
            if session_id:
                record["session_id"] = session_id
            if workspace_id:
                record["workspace_id"] = workspace_id
            try:
                await asyncio.to_thread(
                    lambda r=record: db.table("scraped_content").insert(r).execute()
                )
                stored += 1
            except Exception as exc:
                logger.error("DB insert failed for %s chunk %d: %s", url, i, exc)

    tasks = [
        _embed_and_insert(i, chunk, h)
        for i, (chunk, h) in enumerate(zip(chunks, all_hashes))
        if h not in existing_map
    ]
    if tasks:
        await asyncio.gather(*tasks)

    return stored


# ── Feed support ──────────────────────────────────────────────────────────────

def _is_feed_url(url: str) -> bool:
    """Return True if the URL looks like an RSS/Atom feed (not a sitemap)."""
    path = urlparse(url).path.lower().rstrip("/")
    if "sitemap" in path:
        return False
    return (
        path.endswith("/feed")
        or path.endswith("/rss")
        or path.endswith("/atom")
        or path.endswith(".rss")
        or path.endswith(".xml")
        or "/feeds/" in path
        or "/rss/" in path
        or "feed=" in url.lower()
    )


def _parse_feed_xml(xml_text: str) -> list[dict]:
    """
    Parse RSS 2.0 or Atom feed XML.
    Returns list of {title, url, content} dicts.
    """
    articles: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return articles

    def local(tag: str) -> str:
        return tag.split("}")[-1] if "}" in tag else tag

    root_local = local(root.tag)

    if root_local == "rss":
        # RSS 2.0
        channel = root.find("channel")
        for item in (channel.findall("item") if channel is not None else []):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            content_el = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
            raw = (content_el.text if content_el is not None and content_el.text
                   else item.findtext("description") or "")
            clean = BeautifulSoup(raw, "lxml").get_text(separator=" ", strip=True)
            clean = re.sub(r"\s+", " ", clean).strip()
            if link and len(clean) >= 30:
                articles.append({"title": title, "url": link, "content": clean})

    elif root_local == "feed":
        # Atom
        ns = "http://www.w3.org/2005/Atom"
        for entry in root.findall(f"{{{ns}}}entry"):
            title_el = entry.find(f"{{{ns}}}title")
            title = (title_el.text or "").strip() if title_el is not None else ""
            link_el = (entry.find(f"{{{ns}}}link[@rel='alternate']")
                       or entry.find(f"{{{ns}}}link"))
            link = link_el.get("href", "").strip() if link_el is not None else ""
            content_el = entry.find(f"{{{ns}}}content")
            summary_el = entry.find(f"{{{ns}}}summary")
            raw = ""
            if content_el is not None and content_el.text:
                raw = content_el.text
            elif summary_el is not None and summary_el.text:
                raw = summary_el.text
            clean = BeautifulSoup(raw, "lxml").get_text(separator=" ", strip=True)
            clean = re.sub(r"\s+", " ", clean).strip()
            if link and len(clean) >= 30:
                articles.append({"title": title, "url": link, "content": clean})

    return articles


async def _scrape_feed(
    source_id: str, feed_url: str,
    session_id: str | None = None, workspace_id: str | None = None,
) -> dict:
    """Fetch an RSS/Atom feed and store each article as content chunks."""
    from ..scrape_status import set_status

    set_status(source_id, "running", "Fetching feed…")
    xml_text = await _fetch_text(feed_url, timeout=20)
    if not xml_text:
        set_status(source_id, "error", "Could not fetch feed")
        return {"pages": 0, "new_chunks": 0, "errors": 1}

    articles = _parse_feed_xml(xml_text)
    if not articles:
        set_status(source_id, "error", "No articles found in feed")
        return {"pages": 0, "new_chunks": 0, "errors": 1}

    logger.info("Feed %s: %d articles found", feed_url, len(articles))
    total_new = 0
    errors = 0

    for i, article in enumerate(articles, 1):
        set_status(source_id, "running",
                   f"Article {i}/{len(articles)} · {article['url'][:60]}")
        try:
            new = await _store_content_chunks(
                source_id, article["url"], article["title"], article["content"],
                session_id=session_id, workspace_id=workspace_id,
            )
            total_new += new
        except Exception as exc:
            logger.error("Feed article failed %s: %s", article["url"], exc)
            errors += 1

    db = get_service_db()
    await asyncio.to_thread(
        lambda: db.table("sources")
        .update({"last_scraped_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", source_id)
        .execute()
    )

    set_status(source_id, "completed",
               f"{len(articles)} articles · {total_new} new chunks · {errors} errors",
               new_chunks=total_new)
    return {"pages": len(articles), "new_chunks": total_new, "errors": errors}


async def scrape_source(source_id: str, base_url: str, max_pages: int = 50,
                        workspace_id: str | None = None) -> dict:
    """
    Full crawl for a source:
      1. Discover seed URLs from sitemap (if available).
      2. BFS-follow same-domain links found on every crawled page.
      3. Falls back to base URL only if no sitemap exists.
    Stops when max_pages is reached.
    """
    from ..scrape_status import get_status, set_status

    try:
        validate_url(base_url)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    # ── Concurrency guard (Task 9) ────────────────────────────────────────────
    current = get_status(source_id)
    if current.get("state") == "running":
        updated = current.get("updated_at")
        stale = True
        if updated:
            from datetime import timedelta
            age = datetime.now(timezone.utc) - datetime.fromisoformat(updated)
            stale = age > timedelta(minutes=30)
        if not stale:
            return {"skipped": True, "reason": "already running"}

    db = get_service_db()

    # ── Create scrape session ─────────────────────────────────────────────────
    session_started_at = datetime.now(timezone.utc).isoformat()
    session_row = await asyncio.to_thread(
        lambda: db.table("scrape_sessions").insert({
            "source_id": source_id,
            "workspace_id": workspace_id,
            "started_at": session_started_at,
        }).execute()
    )
    session_id: str | None = (session_row.data[0]["id"] if session_row.data else None)

    # Fast path: URL is explicitly a feed (RSS/Atom)
    if _is_feed_url(base_url):
        result = await _scrape_feed(source_id, base_url, session_id=session_id, workspace_id=workspace_id)
        if session_id:
            await asyncio.to_thread(
                lambda: db.table("scrape_sessions").update({
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "pages": result["pages"],
                    "new_chunks": result["new_chunks"],
                    "errors": result["errors"],
                }).eq("id", session_id).execute()
            )
        return result

    logger.info("Starting crawl for %s (max %d pages)", base_url, max_pages)
    base_parsed = urlparse(base_url)
    set_status(source_id, "running", "Discovering sitemap…")

    total_stored = 0
    errors = 0
    pages_crawled = 0
    _crawl_failed = False
    visited: set[str] = set()
    skipped_robots = 0

    try:
        # ── Step 1: Sitemap seeds ─────────────────────────────────────────────
        sitemap_urls = await discover_sitemap_urls(base_url, max_urls=max_pages)
        sitemap_found = len(sitemap_urls) > 0

        if sitemap_found:
            origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
            await asyncio.to_thread(
                lambda: db.table("sources")
                .update({"sitemap_url": f"{origin}/sitemap.xml"})
                .eq("id", source_id)
                .execute()
            )
            logger.info("Sitemap: %d seed URLs", len(sitemap_urls))
            seeds = sitemap_urls
            set_status(source_id, "running", f"Sitemap found — {len(sitemap_urls)} pages queued")
        else:
            logger.info("No sitemap — BFS from base URL")
            seeds = [base_url]
            set_status(source_id, "running", "No sitemap — crawling from base URL")

        # ── Step 1b: Parse robots.txt (Task 12) ──────────────────────────────
        origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
        robots_txt = await _fetch_text(f"{origin}/robots.txt")
        rp = urllib.robotparser.RobotFileParser()
        rp.parse((robots_txt or "").splitlines())
        _ua = _HEADERS["User-Agent"]
        skipped_robots = 0

        def _robots_allowed(u: str) -> bool:
            return rp.can_fetch(_ua, u) or rp.can_fetch("*", u)

        # ── Step 2: BFS crawl ─────────────────────────────────────────────────
        visited = set(seeds)
        queue: list[str] = list(seeds)

        while queue and pages_crawled < max_pages:
            url = queue.pop(0)

            if not _robots_allowed(url):
                skipped_robots += 1
                logger.debug("robots.txt disallows %s — skipping", url)
                continue

            logger.info("[%d/%d] Crawling %s", pages_crawled + 1, max_pages, url)

            try:
                try:
                    html = await fetch_page(url)
                    title, content = extract_content(html)
                except Exception as fetch_exc:
                    # Plain fetch failed outright — try a real browser render in
                    # case the site requires JS (this will not help against sites
                    # that are actively blocking automated requests).
                    logger.debug("Plain fetch failed for %s (%s); trying browser render", url, fetch_exc)
                    html = await fetch_page_with_browser(url)
                    title, content = extract_content(html)

                if len(content) < 50:
                    # Likely a client-side-rendered shell — retry with a real browser.
                    try:
                        browser_html = await fetch_page_with_browser(url)
                        b_title, b_content = extract_content(browser_html)
                        if len(b_content) > len(content):
                            html, title, content = browser_html, b_title, b_content
                    except Exception as browser_exc:
                        logger.debug("Browser fallback fetch failed for %s: %s", url, browser_exc)

                # Enqueue new same-domain links discovered on this page
                for link in extract_links(html, url):
                    if (
                        link not in visited
                        and urlparse(link).netloc == base_parsed.netloc
                        and len(visited) < max_pages
                    ):
                        visited.add(link)
                        queue.append(link)

                if len(content) >= 50:
                    stored = await _store_content_chunks(
                        source_id, url, title, content,
                        session_id=session_id, workspace_id=workspace_id,
                    )
                    total_stored += stored

                pages_crawled += 1
                set_status(source_id, "running",
                           f"Page {pages_crawled}/{max(len(visited), pages_crawled)} · {url[:70]}")
                await asyncio.sleep(0.5)  # polite crawl delay

            except Exception as exc:
                logger.error("Crawl error at %s: %s", url, exc)
                errors += 1

    except Exception as _outer_exc:
        _crawl_failed = True
        set_status(source_id, "error", str(_outer_exc))
        raise

    finally:
        # ── Step 3: Finalise session & source timestamp ───────────────────────
        now = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(
            lambda: db.table("sources")
            .update({"last_scraped_at": now})
            .eq("id", source_id)
            .execute()
        )
        if session_id:
            await asyncio.to_thread(
                lambda: db.table("scrape_sessions").update({
                    "finished_at": now,
                    "pages": pages_crawled,
                    "new_chunks": total_stored,
                    "errors": errors,
                }).eq("id", session_id).execute()
            )
        # ── Step 4: Expire stale chunks (Task 7) ─────────────────────────────
        if pages_crawled > 0:
            error_rate = errors / pages_crawled
            if error_rate < 0.20:
                try:
                    await asyncio.to_thread(
                        lambda: db.table("scraped_content")
                        .delete()
                        .eq("source_id", source_id)
                        .lt("last_seen_at", session_started_at)
                        .execute()
                    )
                    logger.info("Expired stale chunks for source %s", source_id)
                except Exception as _exc:
                    logger.warning("Could not expire stale chunks: %s", _exc)

    summary = {
        "urls_found": len(visited),
        "urls_scraped": pages_crawled,
        "chunks_stored": total_stored,
        "errors": errors,
        "skipped_robots": skipped_robots,
    }
    logger.info("Crawl complete for %s: %s", base_url, summary)
    set_status(source_id, "completed",
               f"{pages_crawled} pages · {total_stored} new chunks · {errors} errors",
               new_chunks=total_stored)
    return summary


async def scrape_and_store(source_id: str, url: str) -> int:
    """
    Scrape a single URL, chunk + embed, store in Supabase.
    Returns the number of new chunks stored.
    """
    html = await fetch_page(url)
    title, content = extract_content(html)

    if len(content) < 50:
        logger.warning("Very little content at %s — skipping", url)
        return 0

    stored = await _store_content_chunks(source_id, url, title, content)

    db = get_service_db()
    await asyncio.to_thread(
        lambda: db.table("sources")
        .update({"last_scraped_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", source_id)
        .execute()
    )
    return stored
