from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests import Response, Session
from requests.exceptions import RequestException


DEFAULT_CHECK_INTERVAL_SECONDS = 120
DEFAULT_DATABASE_PATH = "otomoto_monitor.sqlite3"
DATABASE_PATH = DEFAULT_DATABASE_PATH
MAX_NOTIFICATIONS_PER_CYCLE = 10
HTTP_TIMEOUT_SECONDS = 20
HTTP_RETRIES = 3
HTTP_BACKOFF_SECONDS = 2
DISCORD_RETRIES = 3
DISCORD_TIMEOUT_SECONDS = 15
PLAYWRIGHT_TIMEOUT_MS = 30_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 "
    "OTOMOTO-Monitor/1.0"
)

PRICE_RE = re.compile(
    r"(?i)(?:\d[\d\s.,]{1,15}\s*(?:zl|zł|pln|eur|€)|(?:do negocjacji|zapytaj o cene|zapytaj o cenę))"
)
WHITESPACE_RE = re.compile(r"\s+")
OTOMOTO_ID_RE = re.compile(r"(?:-|/)(ID[0-9A-Za-z]+)(?:\.html|/|$)")

logger = logging.getLogger("otomoto-monitor")


@dataclass(slots=True)
class Config:
    otomoto_urls: list[str]
    discord_webhook_url: str
    check_interval_seconds: int
    first_run_notify: bool


@dataclass(slots=True)
class Listing:
    listing_id: str
    url: str
    title: str
    price: str
    location: str
    thumbnail_url: str | None


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def split_env_urls(value: str) -> list[str]:
    parts = re.split(r"[\n|]+", value or "")
    return [part.strip() for part in parts if part.strip()]


def load_otomoto_urls() -> list[str]:
    raw_urls: list[str] = []

    legacy_url = os.getenv("OTOMOTO_URL", "").strip()
    if legacy_url:
        raw_urls.extend(split_env_urls(legacy_url))

    grouped_urls = os.getenv("OTOMOTO_URLS", "").strip()
    if grouped_urls:
        raw_urls.extend(split_env_urls(grouped_urls))

    numbered_urls: list[tuple[int, str]] = []
    for key, value in os.environ.items():
        match = re.fullmatch(r"OTOMOTO_URL_(\d+)", key)
        if match and value.strip():
            numbered_urls.append((int(match.group(1)), value.strip()))

    for _, value in sorted(numbered_urls):
        raw_urls.extend(split_env_urls(value))

    urls: list[str] = []
    seen: set[str] = set()
    for url in raw_urls:
        normalized = url.strip()
        if normalized and normalized not in seen:
            urls.append(normalized)
            seen.add(normalized)

    return urls


def load_config() -> Config:
    load_dotenv()

    otomoto_urls = load_otomoto_urls()
    discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    interval_raw = os.getenv("CHECK_INTERVAL_SECONDS", str(DEFAULT_CHECK_INTERVAL_SECONDS)).strip()

    if not otomoto_urls:
        raise ValueError("Brakuje linku OTOMOTO w .env. Uzyj OTOMOTO_URL_1= albo OTOMOTO_URL=.")
    if not discord_webhook_url:
        raise ValueError("Brakuje DISCORD_WEBHOOK_URL w pliku .env.")

    try:
        interval = int(interval_raw)
    except ValueError as exc:
        raise ValueError("CHECK_INTERVAL_SECONDS musi byc liczba calkowita.") from exc

    if interval < 60:
        logger.warning(
            "CHECK_INTERVAL_SECONDS=%s jest dosc agresywne. Zalecane minimum to 120 sekund.",
            interval,
        )

    logger.info("Wczytano %s linkow OTOMOTO z konfiguracji.", len(otomoto_urls))

    return Config(
        otomoto_urls=otomoto_urls,
        discord_webhook_url=discord_webhook_url,
        check_interval_seconds=interval,
        first_run_notify=parse_bool(os.getenv("FIRST_RUN_NOTIFY"), default=False),
    )


def init_db(db_path: str = DATABASE_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                listing_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                price TEXT NOT NULL,
                location TEXT NOT NULL,
                thumbnail_url TEXT,
                first_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_first_seen_at ON listings(first_seen_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_sources (
                source_key TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_scanned_at TEXT NOT NULL
            )
            """
        )


def load_seen_listings(db_path: str = DATABASE_PATH) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT listing_id FROM listings").fetchall()
    return {row[0] for row in rows}


def load_seen_sources(db_path: str = DATABASE_PATH) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT source_key FROM search_sources").fetchall()
    return {row[0] for row in rows}


def normalize_source_key(url: str) -> str:
    return normalize_url(url, url)


def save_source_scan(source_url: str, db_path: str = DATABASE_PATH) -> None:
    source_key = normalize_source_key(source_url)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO search_sources (source_key, url, first_seen_at, last_scanned_at)
            VALUES (?, ?, ?, ?)
            """,
            (source_key, source_url, now, now),
        )


def save_listing(listing: Listing, db_path: str = DATABASE_PATH) -> bool:
    first_seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO listings (
                listing_id,
                url,
                title,
                price,
                location,
                thumbnail_url,
                first_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.listing_id,
                listing.url,
                listing.title,
                listing.price,
                listing.location,
                listing.thumbnail_url,
                first_seen_at,
            ),
        )
    return cursor.rowcount > 0


def create_http_session() -> Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
        }
    )
    return session


def fetch_page(url: str, force_playwright: bool = False) -> str:
    if force_playwright:
        return fetch_page_with_playwright(url)

    session = create_http_session()
    last_error: Exception | None = None

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            if should_retry_http(response) and attempt < HTTP_RETRIES:
                sleep_with_backoff(attempt, response=response)
                continue

            html = response.text
            response.raise_for_status()

            if looks_like_js_or_challenge_page(html):
                logger.debug(
                    "Strona zawiera znaczniki JS/zabezpieczen, ale parser sprobuje normalnie."
                )

            return html
        except RequestException as exc:
            last_error = exc
            if attempt < HTTP_RETRIES:
                logger.warning("Blad HTTP przy pobieraniu OTOMOTO (%s/%s): %s", attempt, HTTP_RETRIES, exc)
                sleep_with_backoff(attempt)
                continue
            break

    raise RuntimeError(f"Nie udalo sie pobrac strony OTOMOTO: {last_error}")


def should_retry_http(response: Response) -> bool:
    return response.status_code in {408, 425, 429, 500, 502, 503, 504}


def sleep_with_backoff(attempt: int, response: Response | None = None) -> None:
    retry_after = None
    if response is not None:
        retry_after_raw = response.headers.get("Retry-After")
        if retry_after_raw and retry_after_raw.isdigit():
            retry_after = int(retry_after_raw)

    delay = retry_after if retry_after is not None else HTTP_BACKOFF_SECONDS * attempt
    time.sleep(delay)


def fetch_page_with_playwright(url: str) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright nie jest zainstalowany. Uruchom: python -m playwright install chromium"
        ) from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=USER_AGENT,
                    locale="pl-PL",
                    viewport={"width": 1366, "height": 900},
                )
                page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
                page.wait_for_timeout(2_000)
                html = page.content()
                if looks_like_js_or_challenge_page(html):
                    logger.warning(
                        "Playwright nadal widzi strone zabezpieczenia/CAPTCHA. Nie omijam zabezpieczen."
                    )
                return html
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"Timeout Playwright podczas ladowania OTOMOTO: {exc}") from exc


def looks_like_js_or_challenge_page(html: str) -> bool:
    lowered = html.lower()
    markers = [
        "captcha",
        "cf-challenge",
        "checking your browser",
        "enable javascript",
        "wlacz javascript",
        "włącz javascript",
        "access denied",
        "bot detection",
    ]
    return any(marker in lowered for marker in markers)


def parse_listings(html: str, base_url: str = "https://www.otomoto.pl/") -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")

    listings: list[Listing] = []
    listings.extend(parse_json_listings(soup, base_url))
    listings.extend(parse_html_listings(soup, base_url))

    merged: dict[str, Listing] = {}
    for listing in listings:
        existing = merged.get(listing.listing_id)
        if existing is None:
            merged[listing.listing_id] = listing
        else:
            merged[listing.listing_id] = merge_listing(existing, listing)

    return list(merged.values())


def parse_json_listings(soup: BeautifulSoup, base_url: str) -> list[Listing]:
    listings: list[Listing] = []

    for script in soup.find_all("script"):
        script_text = script.string or script.get_text(strip=True)
        if not script_text:
            continue

        json_payloads = extract_json_payloads(script, script_text)
        for payload in json_payloads:
            for candidate in walk_json(payload):
                listing = listing_from_json_candidate(candidate, base_url)
                if listing:
                    listings.append(listing)

    return listings


def extract_json_payloads(script: Any, script_text: str) -> list[Any]:
    script_type = (script.get("type") or "").lower()
    script_id = (script.get("id") or "").lower()

    if script_type in {"application/json", "application/ld+json"} or script_id == "__next_data__":
        parsed = try_parse_json(script_text)
        return [parsed] if parsed is not None else []

    payloads: list[Any] = []

    # OTOMOTO has changed its frontend more than once. This conservative extraction
    # catches common embedded JSON blobs without trying to execute page scripts.
    for match in re.finditer(r"({\"(?:props|pageProps|adverts|search|items|offers|results)\".*?})\s*[;<]", script_text):
        parsed = try_parse_json(match.group(1))
        if parsed is not None:
            payloads.append(parsed)

    return payloads


def try_parse_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def walk_json(value: Any, max_depth: int = 12) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, dict):
            if looks_like_listing_dict(node):
                found.append(node)
            for child in node.values():
                walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)

    walk(value, 0)
    return found


def looks_like_listing_dict(data: dict[str, Any]) -> bool:
    url = first_present(data, ["url", "href", "detailUrl", "webUrl", "adUrl", "permalink", "seoUrl"])
    title = first_present(data, ["title", "name", "displayName"])
    listing_id = first_present(data, ["id", "advertId", "adId", "ad_id", "offerId", "listingId"])

    if not (url or listing_id):
        return False
    if not title and not has_any_key(data, ["price", "location", "photos", "images", "thumbnail"]):
        return False

    url_text = stringify(url)
    if url_text and is_probable_listing_url(url_text):
        return True

    return bool(listing_id and title)


def listing_from_json_candidate(data: dict[str, Any], base_url: str) -> Listing | None:
    raw_url = first_present(data, ["url", "href", "detailUrl", "webUrl", "adUrl", "permalink", "seoUrl"])
    raw_id = first_present(data, ["id", "advertId", "adId", "ad_id", "offerId", "listingId", "externalId"])

    url = normalize_url(stringify(raw_url), base_url) if raw_url else ""
    if not url and raw_id:
        maybe_url = find_first_nested_url(data)
        url = normalize_url(maybe_url, base_url) if maybe_url else ""

    if url and not is_probable_listing_url(url) and not raw_id:
        return None

    title = clean_text(stringify(first_present(data, ["title", "name", "displayName", "shortDescription"])))
    price = normalize_price(first_present(data, ["price", "priceGross", "displayPrice", "grossPrice"]))
    location = normalize_location(first_present(data, ["location", "address", "sellerLocation", "city", "region"]))
    image_url = find_first_nested_image(data)
    thumbnail_url = normalize_url(image_url, base_url) if image_url else None

    listing_id = stringify(raw_id) or extract_listing_id(url) or normalize_url_as_key(url)
    if not listing_id or not url:
        return None

    return Listing(
        listing_id=listing_id,
        url=url,
        title=title or "Ogłoszenie OTOMOTO",
        price=price or "Brak ceny",
        location=location or "Brak lokalizacji",
        thumbnail_url=thumbnail_url,
    )


def parse_html_listings(soup: BeautifulSoup, base_url: str) -> list[Listing]:
    listings: list[Listing] = []
    seen_urls: set[str] = set()

    for link in soup.select('a[href*="/oferta/"], a[href*="-ID"]'):
        raw_href = link.get("href")
        if not raw_href:
            continue

        url = normalize_url(raw_href, base_url)
        if not is_probable_listing_url(url) or url in seen_urls:
            continue

        seen_urls.add(url)
        card = find_listing_container(link)
        listing_id = extract_listing_id(url) or extract_listing_id_from_element(card) or normalize_url_as_key(url)

        # These selectors are intentionally grouped in one place. If OTOMOTO changes
        # its markup, this is the first area to update.
        title = extract_title(card, link)
        price = extract_price(card)
        location = extract_location(card)
        thumbnail_url = extract_thumbnail(card, base_url)

        listings.append(
            Listing(
                listing_id=listing_id,
                url=url,
                title=title or "Ogłoszenie OTOMOTO",
                price=price or "Brak ceny",
                location=location or "Brak lokalizacji",
                thumbnail_url=thumbnail_url,
            )
        )

    return listings


def find_listing_container(link: Any) -> Any:
    for parent_name in ["article", "li", "section", "div"]:
        parent = link.find_parent(parent_name)
        if parent and "/oferta/" in str(parent):
            return parent
    return link


def extract_title(card: Any, link: Any) -> str:
    selectors = [
        "[data-testid*='title' i]",
        "[class*='title' i]",
        "h1",
        "h2",
        "h3",
        "h4",
    ]

    for selector in selectors:
        node = card.select_one(selector)
        text = clean_text(node.get_text(" ", strip=True)) if node else ""
        if text:
            return text

    return clean_text(link.get("aria-label") or link.get("title") or link.get_text(" ", strip=True))


def extract_price(card: Any) -> str:
    selectors = [
        "[data-testid*='price' i]",
        "[class*='price' i]",
        "[aria-label*='cena' i]",
    ]

    for selector in selectors:
        node = card.select_one(selector)
        text = clean_text(node.get_text(" ", strip=True)) if node else ""
        if text and looks_like_price(text):
            return text

    card_text = clean_text(card.get_text(" ", strip=True))
    match = PRICE_RE.search(card_text)
    return clean_text(match.group(0)) if match else ""


def extract_location(card: Any) -> str:
    selectors = [
        "[data-testid*='location' i]",
        "[class*='location' i]",
        "[aria-label*='lokalizacja' i]",
        "[class*='address' i]",
    ]

    for selector in selectors:
        node = card.select_one(selector)
        text = clean_text(node.get_text(" ", strip=True)) if node else ""
        if text and not looks_like_price(text):
            return text

    return ""


def extract_thumbnail(card: Any, base_url: str) -> str | None:
    image = card.select_one("img")
    if not image:
        return None

    candidates = [
        image.get("src"),
        image.get("data-src"),
        image.get("data-original"),
        image.get("data-lazy"),
    ]

    srcset = image.get("srcset") or image.get("data-srcset")
    if srcset:
        candidates.extend(part.strip().split(" ")[0] for part in srcset.split(",") if part.strip())

    for candidate in candidates:
        if candidate and not candidate.startswith("data:"):
            return normalize_url(candidate, base_url)

    return None


def extract_listing_id_from_element(element: Any) -> str | None:
    current = element
    for _ in range(4):
        if not current:
            return None
        for attr in ["data-id", "data-ad-id", "data-advert-id", "id"]:
            value = current.get(attr) if hasattr(current, "get") else None
            if value:
                text = stringify(value)
                if text:
                    return text
        current = current.parent
    return None


def merge_listing(primary: Listing, secondary: Listing) -> Listing:
    return Listing(
        listing_id=primary.listing_id,
        url=primary.url or secondary.url,
        title=prefer_value(primary.title, secondary.title, "Ogłoszenie OTOMOTO"),
        price=prefer_value(primary.price, secondary.price, "Brak ceny"),
        location=prefer_value(primary.location, secondary.location, "Brak lokalizacji"),
        thumbnail_url=primary.thumbnail_url or secondary.thumbnail_url,
    )


def prefer_value(first: str, second: str, placeholder: str) -> str:
    if first and first != placeholder:
        return first
    if second:
        return second
    return first


def first_present(data: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def has_any_key(data: dict[str, Any], keys: list[str]) -> bool:
    return any(key in data for key in keys)


def normalize_price(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, dict):
        display = first_present(
            value,
            ["displayValue", "formatted", "label", "valueFormatted", "gross", "net", "amount", "value"],
        )
        currency = stringify(first_present(value, ["currency", "currencyCode"]))
        text = stringify(display)
        if text and currency and currency.lower() not in text.lower():
            text = f"{text} {currency}"
        return clean_text(text)

    return clean_text(stringify(value))


def normalize_location(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, dict):
        parts = []
        for key in ["city", "name", "region", "province", "county", "street"]:
            text = clean_text(stringify(value.get(key)))
            if text and text not in parts:
                parts.append(text)
        return ", ".join(parts)

    if isinstance(value, list):
        return ", ".join(clean_text(stringify(item)) for item in value if clean_text(stringify(item)))

    return clean_text(stringify(value))


def find_first_nested_url(data: Any) -> str:
    return find_first_nested_value(
        data,
        key_names={"url", "href", "detailUrl", "webUrl", "adUrl", "permalink", "seoUrl"},
        predicate=is_probable_listing_url,
    )


def find_first_nested_image(data: Any) -> str:
    return find_first_nested_value(
        data,
        key_names={"thumbnail", "thumbnailUrl", "image", "imageUrl", "photo", "photoUrl", "src"},
        predicate=is_probable_image_url,
    )


def find_first_nested_value(data: Any, key_names: set[str], predicate: Any, depth: int = 0) -> str:
    if depth > 8:
        return ""

    if isinstance(data, dict):
        for key, value in data.items():
            if key in key_names:
                text = stringify(value)
                if text and predicate(text):
                    return text
            found = find_first_nested_value(value, key_names, predicate, depth + 1)
            if found:
                return found

    if isinstance(data, list):
        for item in data:
            found = find_first_nested_value(item, key_names, predicate, depth + 1)
            if found:
                return found

    if isinstance(data, str) and predicate(data):
        return data

    return ""


def is_probable_listing_url(url: str) -> bool:
    lowered = url.lower()
    return "/oferta/" in lowered or bool(OTOMOTO_ID_RE.search(url))


def is_probable_image_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith(("http://", "https://", "//")) and any(
        token in lowered for token in [".jpg", ".jpeg", ".png", ".webp", "images", "img"]
    )


def normalize_url(url: str, base_url: str) -> str:
    url = unescape(clean_text(url))
    if not url:
        return ""

    absolute = urljoin(base_url, url)
    parsed = urlparse(absolute)

    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            "",
            urlencode(query, doseq=True),
            "",
        )
    )


def normalize_url_as_key(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def extract_listing_id(url: str) -> str | None:
    match = OTOMOTO_ID_RE.search(url)
    return match.group(1) if match else None


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, dict):
        text = first_present(value, ["displayValue", "formatted", "label", "name", "value", "url", "href"])
        return stringify(text)
    if isinstance(value, list):
        return " ".join(stringify(item) for item in value)
    return str(value)


def clean_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", unescape(text or "")).strip()


def looks_like_price(text: str) -> bool:
    return bool(PRICE_RE.search(text))


def send_discord_notification(webhook_url: str, listing: Listing) -> bool:
    payload: dict[str, Any] = {
        "content": "Nowe ogłoszenie z OTOMOTO",
        "embeds": [
            {
                "title": listing.title[:256],
                "url": listing.url,
                "description": f"**Cena:** {listing.price}\n**Lokalizacja:** {listing.location}",
                "color": 0x00A3E0,
                "fields": [
                    {
                        "name": "Link",
                        "value": f"[Otwórz ogłoszenie]({listing.url})",
                        "inline": False,
                    }
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    if listing.thumbnail_url:
        payload["embeds"][0]["thumbnail"] = {"url": listing.thumbnail_url}

    return post_to_discord(webhook_url, payload)


def send_discord_text(webhook_url: str, message: str) -> bool:
    return post_to_discord(webhook_url, {"content": message})


def post_to_discord(webhook_url: str, payload: dict[str, Any]) -> bool:
    session = requests.Session()

    for attempt in range(1, DISCORD_RETRIES + 1):
        try:
            response = session.post(webhook_url, json=payload, timeout=DISCORD_TIMEOUT_SECONDS)

            if response.status_code == 429 and attempt < DISCORD_RETRIES:
                retry_after = get_discord_retry_after(response)
                logger.warning("Discord rate limit. Czekam %.2f s.", retry_after)
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            return True
        except RequestException as exc:
            if attempt < DISCORD_RETRIES:
                logger.warning("Blad Discord webhook (%s/%s): %s", attempt, DISCORD_RETRIES, exc)
                time.sleep(HTTP_BACKOFF_SECONDS * attempt)
                continue
            logger.error("Nie udalo sie wyslac wiadomosci Discord: %s", exc)
            return False

    return False


def get_discord_retry_after(response: Response) -> float:
    try:
        data = response.json()
        retry_after = float(data.get("retry_after", 1.0))
    except (ValueError, TypeError):
        retry_after = 1.0
    return max(retry_after, 1.0)


def fetch_and_parse_listings_for_url(url: str) -> list[Listing]:
    html = fetch_page(url)
    listings = parse_listings(html, url)

    if not listings:
        if looks_like_js_or_challenge_page(html):
            logger.warning(
                "Nie znaleziono ogloszen, a strona wyglada na wymagajaca JS/zabezpieczenie. "
                "Probuje opcjonalny fallback Playwright."
            )
        else:
            logger.warning("Nie znaleziono ogloszen w HTML/JSON. Probuje opcjonalny fallback Playwright.")

        try:
            html = fetch_page(url, force_playwright=True)
            listings = parse_listings(html, url)
        except Exception as exc:
            logger.warning("Fallback Playwright niedostepny albo nie powiodl sie: %s", exc)

    return listings


def run_once(config: Config, db_path: str = DATABASE_PATH) -> None:
    scan_started_at = datetime.now(timezone.utc)
    notification_sent = False
    init_db(db_path)

    logger.info("Start skanu: %s", scan_started_at.isoformat(timespec="seconds"))

    seen_before_scan = load_seen_listings(db_path)
    seen_sources = load_seen_sources(db_path)
    first_run = len(seen_before_scan) == 0

    found_total = 0
    unique_found: dict[str, Listing] = {}
    new_ids_this_cycle: set[str] = set()
    notification_candidates: dict[str, Listing] = {}
    baseline_only_count = 0

    for index, url in enumerate(config.otomoto_urls, start=1):
        source_key = normalize_source_key(url)
        source_first_run = source_key not in seen_sources
        source_note = " - nowy link, zapis bazowy" if source_first_run and not config.first_run_notify else ""
        logger.info("Skan linku %s/%s%s", index, len(config.otomoto_urls), source_note)

        try:
            listings = fetch_and_parse_listings_for_url(url)
            found_total += len(listings)

            if listings or not source_first_run:
                save_source_scan(url, db_path)
                seen_sources.add(source_key)
            else:
                logger.warning(
                    "Nowy link %s/%s nie zostal jeszcze zapisany jako bazowy, bo nie znaleziono ogloszen.",
                    index,
                    len(config.otomoto_urls),
                )

            for listing in listings:
                unique_found.setdefault(listing.listing_id, listing)

                if listing.listing_id in seen_before_scan:
                    continue

                if listing.listing_id not in new_ids_this_cycle:
                    if save_listing(listing, db_path):
                        new_ids_this_cycle.add(listing.listing_id)

                should_notify_listing = config.first_run_notify or (not first_run and not source_first_run)
                if should_notify_listing:
                    notification_candidates.setdefault(listing.listing_id, listing)
                else:
                    baseline_only_count += 1

            logger.info("Link %s/%s: znaleziono=%s", index, len(config.otomoto_urls), len(listings))
        except Exception:
            logger.exception(
                "Skan linku %s/%s nie powiodl sie. Pozostale linki beda skanowane dalej.",
                index,
                len(config.otomoto_urls),
            )

    new_listings = list(notification_candidates.values())

    if new_listings:
        to_send = new_listings[:MAX_NOTIFICATIONS_PER_CYCLE]
        skipped = len(new_listings) - len(to_send)

        for listing in to_send:
            if send_discord_notification(config.discord_webhook_url, listing):
                notification_sent = True

        if skipped > 0:
            message = (
                f"Wykryto jeszcze {skipped} nowych ogłoszeń — "
                "pominięto w tym cyklu, ale zapisano w bazie"
            )
            if send_discord_text(config.discord_webhook_url, message):
                notification_sent = True
    elif first_run and new_ids_this_cycle:
        logger.info(
            "Pierwszy skan: zapisano %s ogloszen jako stan bazowy, bez wysylania powiadomien.",
            len(new_ids_this_cycle),
        )
    elif baseline_only_count > 0:
        logger.info(
            "Zapisano %s ogloszen z nowych linkow jako stan bazowy, bez wysylania powiadomien.",
            baseline_only_count,
        )

    logger.info(
        "Koniec skanu: linki=%s, znaleziono=%s, unikalne=%s, nowe_zapisane=%s, do_powiadomienia=%s, powiadomienie_wyslane=%s",
        len(config.otomoto_urls),
        found_total,
        len(unique_found),
        len(new_ids_this_cycle),
        len(new_listings),
        notification_sent,
    )


def main_loop(config: Config, db_path: str = DATABASE_PATH) -> None:
    logger.info(
        "Monitor OTOMOTO uruchomiony. Linki: %s. Interwal: %s s.",
        len(config.otomoto_urls),
        config.check_interval_seconds,
    )
    while True:
        run_once(config, db_path=db_path)
        logger.info("Czekam %s s do kolejnego skanu.", config.check_interval_seconds)
        time.sleep(config.check_interval_seconds)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor nowych ogloszen OTOMOTO -> Discord.")
    parser.add_argument("--once", action="store_true", help="Wykonaj jeden skan i zakoncz.")
    parser.add_argument(
        "--db",
        default=None,
        help=f"Sciezka do pliku SQLite. Domyslnie: env DATABASE_PATH albo {DEFAULT_DATABASE_PATH}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv or sys.argv[1:])

    try:
        config = load_config()
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    db_path = args.db or os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH)

    if args.once:
        run_once(config, db_path=db_path)
    else:
        main_loop(config, db_path=db_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
