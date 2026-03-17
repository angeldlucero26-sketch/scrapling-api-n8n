"""
Scrapling API Microservice
===========================
A FastAPI wrapper around Scrapling for scraping websites and extracting
contact info (emails, phones, social media links).

Called from n8n's HTTP Request node.
"""

import re
import asyncio
import logging
from typing import Optional
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

from scrapling.fetchers import Fetcher, StealthyFetcher

# ── Logging ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapling-api")

# ── App ────────────────────────────────────────────────────
app = FastAPI(
    title="Scrapling API",
    description="Scrape websites and extract contact information using Scrapling",
    version="1.0.0",
)

# ── Regex patterns ─────────────────────────────────────────
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)
PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s\-.]?)?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}"
)
SOCIAL_DOMAINS = {
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "instagram.com": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "linkedin.com": "linkedin",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "wa.me": "whatsapp",
}

# Emails to ignore (common false positives)
IGNORE_EMAILS = {
    "example@example.com",
    "email@example.com",
    "name@domain.com",
    "user@example.com",
    "info@w3.org",
    "sentry@sentry.io",
}


# ── Models ─────────────────────────────────────────────────
class ScrapeRequest(BaseModel):
    website: str
    stealth: bool = False
    extract_subpages: bool = False
    timeout: int = 30


class ContactInfo(BaseModel):
    emails: list[str] = []
    phones: list[str] = []
    social: dict[str, list[str]] = {}


class ScrapeResponse(BaseModel):
    success: bool
    url: str
    title: str = ""
    description: str = ""
    contact: ContactInfo = ContactInfo()
    links_found: int = 0
    pages_scraped: int = 1
    error: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────
def normalize_url(url: str) -> str:
    """Ensure URL has a scheme."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def extract_emails(text: str) -> list[str]:
    """Extract valid email addresses from text."""
    found = set(EMAIL_RE.findall(text))
    # Filter out junk
    cleaned = set()
    for email in found:
        email_lower = email.lower()
        if email_lower in IGNORE_EMAILS:
            continue
        # Skip image/file extensions
        if any(email_lower.endswith(ext) for ext in (".png", ".jpg", ".gif", ".svg", ".webp", ".css", ".js")):
            continue
        cleaned.add(email_lower)
    return sorted(cleaned)


def extract_phones(text: str) -> list[str]:
    """Extract phone numbers from text."""
    found = PHONE_RE.findall(text)
    cleaned = set()
    for phone in found:
        digits = re.sub(r"[^\d+]", "", phone)
        if 7 <= len(digits) <= 15:
            cleaned.add(phone.strip())
    return sorted(cleaned)


def extract_social(html: str, base_url: str) -> dict[str, list[str]]:
    """Extract social media links from HTML."""
    social: dict[str, list[str]] = {}
    # Find all href values
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    for href in hrefs:
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        domain = parsed.netloc.lower().replace("www.", "")
        for social_domain, platform in SOCIAL_DOMAINS.items():
            if domain.endswith(social_domain):
                if platform not in social:
                    social[platform] = []
                if full_url not in social[platform]:
                    social[platform].append(full_url)
                break
    return social


def find_contact_pages(page, base_url: str) -> list[str]:
    """Find links to contact/about pages."""
    contact_keywords = ["contact", "contacto", "about", "sobre", "nosotros", "acerca"]
    links = []
    try:
        anchors = page.css("a")
        for a in anchors:
            href = a.attrib.get("href", "")
            text = (a.text or "").lower()
            href_lower = href.lower()
            if any(kw in text or kw in href_lower for kw in contact_keywords):
                full_url = urljoin(base_url, href)
                if full_url not in links and urlparse(full_url).netloc == urlparse(base_url).netloc:
                    links.append(full_url)
    except Exception:
        pass
    return links[:3]  # Max 3 subpages


# ── Main scrape logic ──────────────────────────────────────
async def scrape_website(req: ScrapeRequest) -> ScrapeResponse:
    url = normalize_url(req.website)
    logger.info(f"Scraping: {url} (stealth={req.stealth})")

    all_emails: set[str] = set()
    all_phones: set[str] = set()
    all_social: dict[str, list[str]] = {}
    title = ""
    description = ""
    pages_scraped = 0

    try:
        # ── Fetch main page ──
        if req.stealth:
            page = await asyncio.to_thread(
                StealthyFetcher.fetch, url, headless=True, timeout=req.timeout * 1000
            )
        else:
            page = await asyncio.to_thread(
                Fetcher.get, url, stealthy_headers=True, timeout=req.timeout
            )

        if page.status != 200:
            return ScrapeResponse(
                success=False, url=url,
                error=f"HTTP {page.status}"
            )

        pages_scraped += 1
        html = page.body if hasattr(page, 'body') else str(page)
        text = page.get_all_text() if hasattr(page, 'get_all_text') else page.text

        # Title & description
        title_el = page.css("title")
        if title_el:
            title = title_el[0].text or ""
        
        meta_desc = page.css('meta[name="description"]')
        if meta_desc:
            description = meta_desc[0].attrib.get("content", "")

        # Extract from main page
        all_emails.update(extract_emails(text))
        all_phones.update(extract_phones(text))
        social = extract_social(str(html), url)
        for platform, links in social.items():
            all_social.setdefault(platform, []).extend(links)

        # ── Optionally scrape contact/about subpages ──
        if req.extract_subpages:
            subpage_urls = find_contact_pages(page, url)
            for sub_url in subpage_urls:
                try:
                    logger.info(f"  Scraping subpage: {sub_url}")
                    if req.stealth:
                        sub_page = await asyncio.to_thread(
                            StealthyFetcher.fetch, sub_url, headless=True, timeout=req.timeout * 1000
                        )
                    else:
                        sub_page = await asyncio.to_thread(
                            Fetcher.get, sub_url, stealthy_headers=True, timeout=req.timeout
                        )

                    if sub_page.status == 200:
                        pages_scraped += 1
                        sub_text = sub_page.get_all_text() if hasattr(sub_page, 'get_all_text') else sub_page.text
                        sub_html = sub_page.body if hasattr(sub_page, 'body') else str(sub_page)
                        all_emails.update(extract_emails(sub_text))
                        all_phones.update(extract_phones(sub_text))
                        sub_social = extract_social(str(sub_html), sub_url)
                        for platform, links in sub_social.items():
                            all_social.setdefault(platform, []).extend(links)
                except Exception as e:
                    logger.warning(f"  Subpage error ({sub_url}): {e}")

        # Deduplicate social links
        for platform in all_social:
            all_social[platform] = list(set(all_social[platform]))

        links_found = len(page.css("a")) if hasattr(page, 'css') else 0

        return ScrapeResponse(
            success=True,
            url=url,
            title=title,
            description=description,
            contact=ContactInfo(
                emails=sorted(all_emails),
                phones=sorted(all_phones),
                social=all_social,
            ),
            links_found=links_found,
            pages_scraped=pages_scraped,
        )

    except Exception as e:
        logger.error(f"Scrape error: {e}")
        return ScrapeResponse(
            success=False,
            url=url,
            error=str(e),
        )


# ── Endpoints ──────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "scrapling-api"}


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(req: ScrapeRequest):
    """
    Scrape a website and extract contact information.
    
    - **website**: URL to scrape (e.g. "example.com")
    - **stealth**: Use headless browser for JS-rendered sites (slower but more complete)
    - **extract_subpages**: Also scrape /contact and /about pages
    - **timeout**: Request timeout in seconds
    """
    result = await scrape_website(req)
    if not result.success and result.error:
        logger.warning(f"Scrape failed for {req.website}: {result.error}")
    return result


@app.post("/scrape/batch")
async def scrape_batch(websites: list[ScrapeRequest]):
    """Scrape multiple websites concurrently."""
    if len(websites) > 10:
        raise HTTPException(400, "Maximum 10 websites per batch")
    tasks = [scrape_website(req) for req in websites]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [
        r if isinstance(r, ScrapeResponse) 
        else ScrapeResponse(success=False, url="unknown", error=str(r))
        for r in results
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)
