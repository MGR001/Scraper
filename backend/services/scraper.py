import asyncio
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..database import get_db
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


async def fetch_page(url: str, timeout: int = 30) -> str:
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=_HEADERS
    ) as client:
        response = await client.get(url)
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
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=_HEADERS) as client:
            r = await client.get(url)
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


async def _store_content_chunks(source_id: str, url: str, title: str, content: str) -> int:
    """Chunk, embed, and upsert content. Returns number of new chunks stored."""
    chunks = chunk_text(content)
    db = get_db()
    stored = 0

    for i, chunk in enumerate(chunks):
        content_hash = hashlib.sha256(chunk.encode()).hexdigest()

        existing = await asyncio.to_thread(
            lambda h=content_hash: db.table("scraped_content")
            .select("id")
            .eq("content_hash", h)
            .execute()
        )
        if existing.data:
            continue

        try:
            embedding = await get_embedding(chunk)
        except Exception as exc:
            logger.error("Embedding failed for chunk %d of %s: %s", i, url, exc)
            continue

        record = {
            "source_id": source_id,
            "url": url,
            "title": title if i == 0 else f"{title} (part {i + 1})",
            "content": chunk,
            "content_hash": content_hash,
            "embedding": embedding,
            "metadata": {
                "chunk_index": i,
                "total_chunks": len(chunks),
                "char_count": len(chunk),
            },
        }
        try:
            await asyncio.to_thread(
                lambda r=record: db.table("scraped_content").insert(r).execute()
            )
            stored += 1
        except Exception as exc:
            logger.error("DB insert failed for %s chunk %d: %s", url, i, exc)

    return stored


# ── Feed support ──────────────────────────────────────────────────────────────

def _is_feed_url(url: str) -> bool:
    """Return True if the URL looks like an RSS/Atom feed."""
    path = urlparse(url).path.lower().rstrip("/")
    return (
        path.endswith("/feed")
        or path.endswith("/rss")
        or path.endswith("/atom")
        or path.endswith(".rss")
        or path.endswith(".xml")
        or "/feeds/" in path
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


async def _scrape_feed(source_id: str, feed_url: str) -> dict:
    """Fetch an RSS/Atom feed and store each article as content chunks."""
    from datetime import datetime, timezone
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
                source_id, article["url"], article["title"], article["content"]
            )
            total_new += new
        except Exception as exc:
            logger.error("Feed article failed %s: %s", article["url"], exc)
            errors += 1

    db = get_db()
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


async def scrape_source(source_id: str, base_url: str, max_pages: int = 50) -> dict:
    """
    Full crawl for a source:
      1. Discover seed URLs from sitemap (if available).
      2. BFS-follow same-domain links found on every crawled page.
      3. Falls back to base URL only if no sitemap exists.
    Stops when max_pages is reached.
    """
    from datetime import datetime, timezone
    from ..scrape_status import set_status

    # Fast path: URL is explicitly a feed (RSS/Atom)
    if _is_feed_url(base_url):
        return await _scrape_feed(source_id, base_url)

    logger.info("Starting crawl for %s (max %d pages)", base_url, max_pages)
    base_parsed = urlparse(base_url)
    db = get_db()
    set_status(source_id, "running", "Discovering sitemap…")

    # ── Step 1: Sitemap seeds ─────────────────────────────────────────────────
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

    # ── Step 2: BFS crawl ─────────────────────────────────────────────────────
    visited: set[str] = set(seeds)
    queue: list[str] = list(seeds)
    total_stored = 0
    errors = 0
    pages_crawled = 0

    while queue and pages_crawled < max_pages:
        url = queue.pop(0)
        logger.info("[%d/%d] Crawling %s", pages_crawled + 1, max_pages, url)

        try:
            html = await fetch_page(url)

            # Enqueue new same-domain links discovered on this page
            for link in extract_links(html, url):
                if (
                    link not in visited
                    and urlparse(link).netloc == base_parsed.netloc
                    and len(visited) < max_pages
                ):
                    visited.add(link)
                    queue.append(link)

            title, content = extract_content(html)
            if len(content) >= 50:
                stored = await _store_content_chunks(source_id, url, title, content)
                total_stored += stored

            pages_crawled += 1
            set_status(source_id, "running",
                       f"Page {pages_crawled}/{max(len(visited), pages_crawled)} · {url[:70]}")
            await asyncio.sleep(0.5)  # polite crawl delay

        except Exception as exc:
            logger.error("Crawl error at %s: %s", url, exc)
            errors += 1

    # ── Step 3: Update last_scraped_at ────────────────────────────────────────
    await asyncio.to_thread(
        lambda: db.table("sources")
        .update({"last_scraped_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", source_id)
        .execute()
    )

    summary = {
        "urls_found": len(visited),
        "urls_scraped": pages_crawled,
        "chunks_stored": total_stored,
        "errors": errors,
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
    from datetime import datetime, timezone

    html = await fetch_page(url)
    title, content = extract_content(html)

    if len(content) < 50:
        logger.warning("Very little content at %s — skipping", url)
        return 0

    stored = await _store_content_chunks(source_id, url, title, content)

    db = get_db()
    await asyncio.to_thread(
        lambda: db.table("sources")
        .update({"last_scraped_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", source_id)
        .execute()
    )
    return stored
