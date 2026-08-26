import base64
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from scrapling.fetchers import StealthySession

from app.config import settings
from app.models import SERVICE_SLUG_PATTERN, ServiceStatus

_session_lock = threading.Lock()
_browser_lock = threading.Lock()
_session: StealthySession | None = None
_PROFILE_SINGLETON_NAMES = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
)

_OK_PATTERNS = (
    r"no current problems",
    r"nenhum problema",
    r"sem problemas",
)
_WARNING_PATTERNS = (
    r"possible problems",
    r"poss[ií]veis problemas",
    r"possivel problema",
    r"enfrenta problemas",
)
_DOWN_PATTERNS = (
    r"problems at",
    r"(?<!no current )problems with",
    r"having problems",
    r"problemas (?:em|no|na|com)",
    r"est[aá] tendo problemas",
    r"indicate problems",
    r"indicam problemas",
    r"show problems",
)


@dataclass
class ScrapeResult:
    service: str
    status: ServiceStatus
    label: str
    message: str
    source_url: str
    checked_at: datetime
    app_image_url: str | None = None
    failure_graph_image_url: str | None = None


@dataclass
class CriticalServiceCard:
    service: str
    status: ServiceStatus
    label: str
    message: str
    source_url: str
    app_image_url: str | None
    card_markup: str
    failure_graph_image_url: str | None = None


@dataclass
class CriticalServicesResult:
    services: list[CriticalServiceCard]
    image_url: str | None
    source_url: str
    checked_at: datetime
    message: str | None = None


def normalize_service(service: str) -> str:
    slug = service.strip().lower()
    if not SERVICE_SLUG_PATTERN.match(slug):
        raise ValueError(
            "Serviço inválido. Use apenas letras, números, hífen ou underscore "
            "(ex.: youtube, whatsapp, instagram)."
        )
    return slug


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_status_message(page, fallback_url: str) -> tuple[str, str]:
    source_url = getattr(page, "url", fallback_url)

    status_block = page.css("#company-status h1")
    if status_block:
        message = _normalize_text(status_block[0].get_all_text())
        if message:
            return message, source_url

    h1 = page.css("h1")
    if h1:
        message = _normalize_text(h1[0].get_all_text())
        if message:
            return message, source_url

    title = page.css("title::text").get()
    if title:
        return _normalize_text(title), source_url

    raise ValueError("Não foi possível localizar o status do serviço na página.")


_APP_IMAGE_SELECTORS = (
    "#company-logo img::attr(src)",
    "#company-card img::attr(src)",
    ".company-logo img::attr(src)",
    ".company-header img::attr(src)",
    "img[alt*='logo']::attr(src)",
    "img[alt*='Logo']::attr(src)",
    "meta[property='og:image']::attr(content)",
)
_FAILURE_GRAPH_IMAGE_SELECTORS = (
    "#chart-row img::attr(src)",
    "#reports-chart img::attr(src)",
    ".chart img::attr(src)",
    ".graph img::attr(src)",
    ".reports-chart img::attr(src)",
    "img[alt*='chart']::attr(src)",
    "img[alt*='Chart']::attr(src)",
    "img[alt*='graph']::attr(src)",
    "img[alt*='Graph']::attr(src)",
    "img[alt*='outage']::attr(src)",
    "img[alt*='Outage']::attr(src)",
    "img[alt*='falha']::attr(src)",
    "img[alt*='Falha']::attr(src)",
)
_FAILURE_GRAPH_SVG_SELECTORS = (
    ".recharts-responsive-container svg.recharts-surface",
    ".recharts-wrapper svg.recharts-surface",
)
_APP_IMAGE_KEYWORDS = ("logo", "company", "app", "icon")
_FAILURE_GRAPH_IMAGE_KEYWORDS = (
    "chart",
    "graph",
    "outage",
    "report",
    "problem",
    "reports",
    "falha",
    "problema",
)
_IGNORED_GRAPH_IMAGE_KEYWORDS = (
    "icon.svg",
    "country_flags",
    "avatar",
    "apple-store",
    "google-play",
    "explorer_promo",
    "dd-app-promo",
)
_IMAGE_ATTRS = ("src", "data-src", "data-lazy-src", "data-original")
_SRCSET_ATTRS = ("srcset", "data-srcset")
_SVG_TO_JPEG_SCRIPT = r"""
import base64
import sys

from playwright.sync_api import sync_playwright

markup = sys.stdin.read().strip()
if not markup.startswith("<svg"):
    raise SystemExit(1)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1000, "height": 500})
        page.set_content(
            f'''
            <!doctype html>
            <html>
              <body style="margin:0;background:#fff;--color-dd-red:#d71920;">
                <div id="chart" style="display:inline-block;background:#fff;color:#d71920;">
                  {markup}
                </div>
                <style>
                  #chart svg {{
                    background: #fff;
                    display: block;
                  }}
                </style>
              </body>
            </html>
            '''
        )
        image_bytes = page.locator("#chart").screenshot(type="jpeg", quality=90)
    finally:
        browser.close()

sys.stdout.write(base64.b64encode(image_bytes).decode("ascii"))
"""
_CARD_TO_JPEG_SCRIPT = r"""
import base64
import sys

from playwright.sync_api import sync_playwright

markup = sys.stdin.read().strip()
if not markup:
    raise SystemExit(1)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1800, "height": 360})
        page.set_content(
            f'''
            <!doctype html>
            <html>
              <head>
                <style>
                  :root {{
                    --color-dd-red: #d71920;
                    font-family: Arial, Helvetica, sans-serif;
                  }}
                  body {{
                    margin: 0;
                    background: #fff;
                  }}
                  #card-root {{
                    display: inline-block;
                    background: #fff;
                    padding: 0;
                  }}
                  #card-root .critical-cards-row {{
                    display: flex;
                    align-items: stretch;
                    gap: 16px;
                    background: #fff;
                    padding: 0;
                  }}
                  #card-root [id^="company-"] {{
                    box-sizing: border-box;
                    width: 292px;
                    min-height: 220px;
                    background: #fff;
                    color: #52525b;
                    border: 1px solid #e4e4e7;
                    border-radius: 12px;
                    text-align: center;
                    overflow: hidden;
                    position: relative;
                    display: flex;
                    flex-direction: column;
                  }}
                  #card-root a {{
                    color: inherit;
                    text-decoration: none;
                  }}
                  #card-root a.absolute {{
                    display: none;
                  }}
                  #card-root a > div,
                  #card-root [id^="company-"] > div > div {{
                    padding: 8px 24px;
                  }}
                  #card-root h2 {{
                    height: 48px;
                    margin: 0 0 8px;
                    color: #18181b;
                    font-size: 16px;
                    font-weight: 600;
                    line-height: 22px;
                    display: flex;
                    align-items: center;
                    justify-content: flex-start;
                    overflow: hidden;
                    text-align: left;
                  }}
                  #card-root h2 + div,
                  #card-root [id^="company-"] .flex-1.min-h-0 {{
                    height: 112px;
                    margin-bottom: 16px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                  }}
                  #card-root img {{
                    width: auto;
                    max-width: 252px;
                    max-height: 112px;
                    object-fit: contain;
                    object-position: center;
                  }}
                  #card-root [role="img"] {{
                    width: 243px;
                    height: 48px;
                    margin: 0 auto 8px;
                    padding: 4px 0;
                    box-sizing: border-box;
                    color: #f59e0b;
                    overflow: visible;
                  }}
                  #card-root svg {{
                    width: 100%;
                    height: 40px;
                    overflow: visible !important;
                    display: block;
                  }}
                  #card-root [role="img"] path,
                  #card-root [role="img"] line,
                  #card-root [role="img"] polyline {{
                    stroke: currentColor !important;
                    fill: none !important;
                    opacity: 1 !important;
                    visibility: visible !important;
                  }}
                </style>
              </head>
              <body>
                <div id="card-root">{markup}</div>
              </body>
            </html>
            '''
        )
        page.evaluate(
            '''() => {
                const colorForGraph = (element) => {
                    const label = (element.getAttribute('aria-label') || '').toLowerCase();
                    if (
                        label.includes('sem problemas') ||
                        label.includes('no current problems') ||
                        label.includes('não mostram') ||
                        label.includes('nao mostram')
                    ) {
                        return '#16a34a';
                    }
                    if (
                        label.includes('possíveis problemas') ||
                        label.includes('possiveis problemas') ||
                        label.includes('possible problems') ||
                        label.includes('enfrenta problemas')
                    ) {
                        return '#f59e0b';
                    }
                    return '#d71920';
                };

                document
                    .querySelectorAll('#card-root [role="img"]')
                    .forEach((element) => {
                        const color = colorForGraph(element);
                        element.style.setProperty('color', color, 'important');
                        element.style.display = 'block';
                        element.style.overflow = 'visible';
                        element.style.opacity = '1';
                        element.style.visibility = 'visible';
                    });

                document
                    .querySelectorAll('#card-root [role="img"] svg path, #card-root [role="img"] svg line, #card-root [role="img"] svg polyline')
                    .forEach((element) => {
                        const graph = element.closest('[role="img"]');
                        const color = graph ? colorForGraph(graph) : '#f59e0b';
                        const stroke = element.getAttribute('stroke');
                        if (!stroke || stroke.includes('var(')) {
                            element.setAttribute('stroke', color);
                        }
                        element.style.setProperty('stroke', color, 'important');
                        element.style.setProperty('fill', 'none', 'important');
                        element.style.setProperty('opacity', '1', 'important');
                        element.style.setProperty('visibility', 'visible', 'important');
                    });

                document
                    .querySelectorAll('#card-root [role="img"] svg')
                    .forEach((element) => {
                        element.style.display = 'block';
                        element.style.overflow = 'visible';
                        element.style.opacity = '1';
                        element.style.visibility = 'visible';
                    });
            }'''
        )
        page.wait_for_load_state("networkidle", timeout=10000)
        page.wait_for_function(
            "Array.from(document.images).every((img) => img.complete)",
            timeout=10000,
        )
        image_bytes = page.locator("#card-root").screenshot(type="jpeg", quality=90)
    finally:
        browser.close()

sys.stdout.write(base64.b64encode(image_bytes).decode("ascii"))
"""


def _first_srcset_url(value: str) -> str | None:
    for candidate in value.split(","):
        parts = candidate.strip().split()
        if parts:
            return parts[0]
    return None


def _absolute_image_url(page, fallback_url: str, value: str | None) -> str | None:
    if not value:
        return None

    raw_value = value.strip()
    raw_url = (
        _first_srcset_url(raw_value)
        if "," in raw_value or " " in raw_value
        else raw_value
    )
    if not raw_url or raw_url.startswith("data:"):
        return None

    source_url = getattr(page, "url", fallback_url) or fallback_url
    return urljoin(source_url, raw_url)


def _image_url_from_element(page, fallback_url: str, image) -> str | None:
    attributes = getattr(image, "attrib", {})

    for attr in _IMAGE_ATTRS:
        url = _absolute_image_url(page, fallback_url, attributes.get(attr))
        if url:
            return url

    for attr in _SRCSET_ATTRS:
        url = _absolute_image_url(page, fallback_url, attributes.get(attr))
        if url:
            return url

    return None


def _extract_image_by_selectors(
    page,
    fallback_url: str,
    selectors: tuple[str, ...],
) -> str | None:
    for selector in selectors:
        url = _absolute_image_url(page, fallback_url, page.css(selector).get())
        if url:
            return url
    return None


def _extract_image_by_keywords(
    page,
    fallback_url: str,
    keywords: tuple[str, ...],
    ignored_keywords: tuple[str, ...] = (),
) -> str | None:
    for image in page.css("img"):
        attributes = getattr(image, "attrib", {})
        haystack = " ".join(
            str(value).lower() for value in attributes.values() if value
        )
        if any(keyword in haystack for keyword in keywords):
            url = _image_url_from_element(page, fallback_url, image)
            if url and not any(
                ignored in url.lower() for ignored in ignored_keywords
            ):
                return url

    return None


def _svg_to_jpeg_data_url(svg_markup: str | None) -> str | None:
    if not svg_markup:
        return None

    markup = svg_markup.strip()
    if not markup.startswith("<svg"):
        return None

    try:
        completed = subprocess.run(
            [sys.executable, "-c", _SVG_TO_JPEG_SCRIPT],
            input=markup,
            capture_output=True,
            check=False,
            text=True,
            timeout=settings.request_timeout_seconds,
        )
    except Exception:
        return None

    encoded = completed.stdout.strip()
    if completed.returncode != 0 or not encoded:
        return None

    return f"data:image/jpeg;base64,{encoded}"


def _card_html_to_jpeg_data_url(card_markup: str | None) -> str | None:
    if not card_markup:
        return None

    markup = card_markup.strip()
    if not markup:
        return None

    try:
        completed = subprocess.run(
            [sys.executable, "-c", _CARD_TO_JPEG_SCRIPT],
            input=markup,
            capture_output=True,
            check=False,
            text=True,
            timeout=settings.request_timeout_seconds,
        )
    except Exception:
        return None

    encoded = completed.stdout.strip()
    if completed.returncode != 0 or not encoded:
        return None

    return f"data:image/jpeg;base64,{encoded}"


def _cards_html_to_jpeg_data_url(card_markups: list[str]) -> str | None:
    if not card_markups:
        return None

    markup = '<div class="critical-cards-row">' + "".join(card_markups) + "</div>"
    return _card_html_to_jpeg_data_url(markup)


def _extract_svg_by_selectors(page, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        data_url = _svg_to_jpeg_data_url(page.css(selector).get())
        if data_url:
            return data_url
    return None


def _service_path(service: str) -> str:
    return urlparse(settings.urls_for_service(service)[0]).path.rstrip("/") + "/"


def _card_matches_service(card_link, fallback_url: str, service: str) -> bool:
    href = getattr(card_link, "attrib", {}).get("href")
    if not href:
        return False

    expected_path = _service_path(service)
    href_path = urlparse(urljoin(fallback_url, href)).path.rstrip("/") + "/"
    return href_path == expected_path


def _company_card_element(card_link):
    """Sobe até o container `company-*` do card na home.

    No DOM atual o `<a>` é absoluto/vazio e logo/gráfico ficam como irmãos,
    então a busca precisa ser no card pai e não só dentro do link.
    """
    element = card_link
    while element is not None:
        element_id = getattr(element, "attrib", {}).get("id", "")
        if element_id.startswith("company-"):
            return element
        element = element.parent
    return None


def _company_card_markup(card_link) -> str | None:
    card = _company_card_element(card_link)
    if card is not None:
        return card.get()
    return card_link.get()


def _service_slug_from_card_link(card_link, fallback_url: str) -> str | None:
    href = getattr(card_link, "attrib", {}).get("href")
    if not href:
        return None

    path = urlparse(urljoin(fallback_url, href)).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1].strip().lower()
    if not SERVICE_SLUG_PATTERN.match(slug):
        return None

    return slug


def _critical_rank(status: ServiceStatus) -> int:
    return {
        ServiceStatus.DOWN: 0,
        ServiceStatus.WARNING: 1,
        ServiceStatus.OK: 2,
        ServiceStatus.UNKNOWN: 2,
    }[status]


def _extract_service_card(
    page,
    fallback_url: str,
    card_link,
) -> CriticalServiceCard | None:
    slug = _service_slug_from_card_link(card_link, fallback_url)
    if not slug:
        return None

    card_root = _company_card_element(card_link) or card_link
    graph_block = card_root.css("[role='img']")
    if not graph_block:
        return None

    message = getattr(graph_block[0], "attrib", {}).get("aria-label")
    if not message:
        return None

    markup = card_root.get() if card_root is not None else None
    if not markup:
        return None

    href = getattr(card_link, "attrib", {}).get("href")
    source_url = urljoin(fallback_url, href) if href else fallback_url
    card_images = card_root.css("img")
    app_image_url = (
        _image_url_from_element(page, fallback_url, card_images[0])
        if card_images
        else None
    )
    status = classify_status(message)

    return CriticalServiceCard(
        service=slug,
        status=status,
        label=status_label(status),
        message=message,
        source_url=source_url,
        app_image_url=app_image_url,
        card_markup=markup,
    )


def _extract_card_image_urls(
    page,
    fallback_url: str,
    service: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    for card_link in page.css("a[href]"):
        if not _card_matches_service(card_link, fallback_url, service):
            continue

        card = _extract_service_card(page, fallback_url, card_link)
        if card is None:
            return None, None, None, None
        failure_graph_image_url = _card_html_to_jpeg_data_url(card.card_markup)
        return (
            card.app_image_url,
            failure_graph_image_url,
            card.message,
            card.source_url,
        )

    return None, None, None, None


def _extract_image_urls(page, fallback_url: str) -> tuple[str | None, str | None]:
    app_image_url = _extract_image_by_selectors(
        page,
        fallback_url,
        _APP_IMAGE_SELECTORS,
    ) or _extract_image_by_keywords(page, fallback_url, _APP_IMAGE_KEYWORDS)
    failure_graph_image_url = _extract_svg_by_selectors(
        page,
        _FAILURE_GRAPH_SVG_SELECTORS,
    ) or _extract_image_by_selectors(
        page,
        fallback_url,
        _FAILURE_GRAPH_IMAGE_SELECTORS,
    ) or _extract_image_by_keywords(
        page,
        fallback_url,
        _FAILURE_GRAPH_IMAGE_KEYWORDS,
        _IGNORED_GRAPH_IMAGE_KEYWORDS,
    )

    return app_image_url, failure_graph_image_url


def _fetch_service_card_image_urls(service: str) -> tuple[str | None, str | None]:
    home_url = f"{settings.downdetector_base_url().rstrip('/')}/"
    try:
        page = _get_session().fetch(home_url)
    except Exception:
        return None, None

    if getattr(page, "status", 200) >= 400:
        return None, None

    app_image_url, failure_graph_image_url, _message, _source_url = (
        _extract_card_image_urls(page, home_url, service)
    )
    return app_image_url, failure_graph_image_url


def _fetch_service_card_result(service: str) -> ScrapeResult | None:
    home_url = f"{settings.downdetector_base_url().rstrip('/')}/"
    try:
        page = _get_session().fetch(home_url)
    except Exception:
        return None

    if getattr(page, "status", 200) >= 400:
        return None

    app_image_url, failure_graph_image_url, message, source_url = (
        _extract_card_image_urls(page, home_url, service)
    )
    if not message or not source_url:
        return None

    status = classify_status(message)
    return ScrapeResult(
        service=service,
        status=status,
        label=status_label(status),
        message=message,
        source_url=source_url,
        checked_at=datetime.now(timezone.utc),
        app_image_url=app_image_url,
        failure_graph_image_url=failure_graph_image_url,
    )


def fetch_critical_services(
    limit: int = 5,
    max_attempts: int = 3,
) -> CriticalServicesResult:
    with _browser_lock:
        return _fetch_critical_services_locked(limit, max_attempts)


def _fetch_critical_services_locked(
    limit: int = 5,
    max_attempts: int = 3,
) -> CriticalServicesResult:
    home_url = f"{settings.downdetector_base_url().rstrip('/')}/"
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            page = _get_session().fetch(home_url)
            if getattr(page, "status", 200) >= 400:
                raise RuntimeError(
                    f"Downdetector retornou HTTP {page.status} para {home_url}"
                )
            break
        except Exception as exc:
            last_error = exc
            _reset_session()
            if attempt < max_attempts:
                time.sleep(3 * attempt)
    else:
        raise RuntimeError(
            f"Falha ao consultar a tela principal do Downdetector: {last_error}"
        ) from last_error

    ranked_cards: list[tuple[int, int, CriticalServiceCard]] = []
    seen_services: set[str] = set()

    for index, card_link in enumerate(page.css("a[href]")):
        card = _extract_service_card(page, home_url, card_link)
        if card is None or card.service in seen_services:
            continue

        seen_services.add(card.service)
        if card.status not in (ServiceStatus.DOWN, ServiceStatus.WARNING):
            continue

        ranked_cards.append((_critical_rank(card.status), index, card))

    ranked_cards.sort(key=lambda item: (item[0], item[1]))
    services = [card for _rank, _index, card in ranked_cards[:limit]]
    for card in services:
        card.failure_graph_image_url = _card_html_to_jpeg_data_url(card.card_markup)
    image_url = _cards_html_to_jpeg_data_url([card.card_markup for card in services])

    return CriticalServicesResult(
        services=services,
        image_url=image_url,
        source_url=home_url,
        checked_at=datetime.now(timezone.utc),
    )


def classify_status(message: str) -> ServiceStatus:
    text = message.lower()

    if any(re.search(p, text) for p in _OK_PATTERNS):
        return ServiceStatus.OK
    if any(re.search(p, text) for p in _WARNING_PATTERNS):
        return ServiceStatus.WARNING
    if any(re.search(p, text) for p in _DOWN_PATTERNS):
        return ServiceStatus.DOWN

    if "no current problems" in text or "nenhum problema" in text:
        return ServiceStatus.OK

    return ServiceStatus.OK


def status_label(status: ServiceStatus) -> str:
    return {
        ServiceStatus.OK: "bom",
        ServiceStatus.WARNING: "instável",
        ServiceStatus.DOWN: "ruim",
        ServiceStatus.UNKNOWN: "bom",
    }[status]


def _clear_browser_profile_locks(user_data_dir: str | None = None) -> None:
    """Remove locks residuais do Chromium em perfis persistentes.

    Após crash/restart, o SingletonLock impede um novo launch no mesmo
    `user_data_dir` (comum em Docker com volume `/browser-data`).
    """
    root = user_data_dir or settings.browser_user_data_dir
    if not root:
        return

    profile = Path(root)
    if not profile.is_dir():
        return

    for name in _PROFILE_SINGLETON_NAMES:
        lock_path = profile / name
        try:
            if lock_path.is_symlink() or lock_path.exists():
                lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _is_profile_lock_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "processsingleton" in message
        or "singletonlock" in message
        or "profile directory" in message
        or "profile is already in use" in message
    )


def _build_session_kwargs(*, use_user_data_dir: bool = True) -> dict:
    session_kwargs = {
        "headless": settings.headless,
        "network_idle": True,
        "solve_cloudflare": True,
        "timeout": settings.request_timeout_seconds * 1000,
        "locale": settings.browser_locale,
        "wait_selector": "#company-status h1, h1",
        "wait_selector_state": "visible",
        "block_webrtc": bool(settings.downdetector_proxy),
    }
    if settings.downdetector_proxy:
        session_kwargs["proxy"] = settings.downdetector_proxy
    if use_user_data_dir and settings.browser_user_data_dir:
        session_kwargs["user_data_dir"] = settings.browser_user_data_dir
    return session_kwargs


def _reset_session() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
            _session = None
        # Dá tempo do Chromium liberar o perfil antes de limpar o lock.
        time.sleep(0.5)
        _clear_browser_profile_locks()


def _get_session() -> StealthySession:
    global _session
    with _session_lock:
        if _session is not None:
            return _session

        _clear_browser_profile_locks()
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                session = StealthySession(
                    **_build_session_kwargs(use_user_data_dir=True)
                )
                session.start()
                _session = session
                return _session
            except Exception as exc:
                last_error = exc
                if _is_profile_lock_error(exc):
                    _clear_browser_profile_locks()
                    time.sleep(0.5)
                    continue
                break

        # Perfil persistente indisponível: sobe sessão efêmera.
        if settings.browser_user_data_dir:
            try:
                session = StealthySession(
                    **_build_session_kwargs(use_user_data_dir=False)
                )
                session.start()
                _session = session
                return _session
            except Exception as exc:
                last_error = exc

        raise RuntimeError(
            f"Falha ao iniciar o navegador: {last_error}"
        ) from last_error


def fetch_service_status(service: str, max_attempts: int = 3) -> ScrapeResult:
    with _browser_lock:
        return _fetch_service_status_locked(service, max_attempts)


def _fetch_service_status_locked(
    service: str, max_attempts: int = 3
) -> ScrapeResult:
    slug = normalize_service(service)
    urls = settings.urls_for_service(slug)
    last_error: Exception | None = None

    for url in urls:
        for attempt in range(1, max_attempts + 1):
            try:
                page = _get_session().fetch(url)

                if getattr(page, "status", 200) >= 400:
                    if page.status in (403, 429):
                        fallback_result = _fetch_service_card_result(slug)
                        if fallback_result is not None:
                            return fallback_result

                    raise RuntimeError(
                        f"Downdetector retornou HTTP {page.status} para {url}"
                    )

                message, source_url = _extract_status_message(page, url)
                app_image_url, page_failure_graph_image_url = _extract_image_urls(
                    page,
                    source_url,
                )
                card_app_image_url, card_failure_graph_image_url = (
                    _fetch_service_card_image_urls(slug)
                )
                app_image_url = card_app_image_url or app_image_url
                failure_graph_image_url = (
                    card_failure_graph_image_url or page_failure_graph_image_url
                )
                status = classify_status(message)

                return ScrapeResult(
                    service=slug,
                    status=status,
                    label=status_label(status),
                    message=message,
                    source_url=source_url,
                    checked_at=datetime.now(timezone.utc),
                    app_image_url=app_image_url,
                    failure_graph_image_url=failure_graph_image_url,
                )
            except Exception as exc:
                last_error = exc
                _reset_session()
                if attempt < max_attempts:
                    time.sleep(3 * attempt)

    fallback_result = _fetch_service_card_result(slug)
    if fallback_result is not None:
        return fallback_result

    raise RuntimeError(
        f"Falha ao consultar o Downdetector para '{slug}': {last_error}"
    ) from last_error


class StatusCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[ScrapeResult, float]] = {}
        self._lock = threading.Lock()

    def get(
        self, service: str, force_refresh: bool = False
    ) -> tuple[ScrapeResult, bool]:
        slug = normalize_service(service)
        now = time.monotonic()

        with self._lock:
            if not force_refresh and slug in self._entries:
                result, expires_at = self._entries[slug]
                if now < expires_at:
                    return result, True

        result = fetch_service_status(slug)

        with self._lock:
            self._entries[slug] = (result, now + self.ttl_seconds)

        return result, False


class CriticalServicesCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[int, tuple[CriticalServicesResult, float]] = {}
        self._lock = threading.Lock()

    def get(
        self,
        limit: int,
        force_refresh: bool = False,
    ) -> tuple[CriticalServicesResult, bool]:
        now = time.monotonic()

        with self._lock:
            cached_entry = self._entries.get(limit)
            if not force_refresh and cached_entry is not None:
                result, expires_at = cached_entry
                if now < expires_at:
                    return result, True

        try:
            result = fetch_critical_services(limit=limit)
        except Exception as exc:
            with self._lock:
                cached_entry = self._entries.get(limit)

            if cached_entry is not None:
                result, _expires_at = cached_entry
                result.message = (
                    "Downdetector bloqueou a consulta atual; retornando último "
                    "resultado em cache."
                )
                return result, True

            return (
                CriticalServicesResult(
                    services=[],
                    image_url=None,
                    source_url=f"{settings.downdetector_base_url().rstrip('/')}/",
                    checked_at=datetime.now(timezone.utc),
                    message=(
                        "Downdetector bloqueou a consulta atual e ainda não há "
                        f"cache disponível: {exc}"
                    ),
                ),
                False,
            )

        with self._lock:
            self._entries[limit] = (result, now + self.ttl_seconds)

        return result, False


status_cache = StatusCache(settings.cache_ttl_seconds)
critical_services_cache = CriticalServicesCache(settings.cache_ttl_seconds)
