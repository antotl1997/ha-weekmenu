"""v1.1: an own recipe from a link on any recipe site ("Van een link").

This is the one place the add-on fetches a URL the user typed, and it runs inside Home
Assistant's network, next to http://supervisor and the HA API. So `fetch_page`:

- takes only http and https, on the default port (80 or 443), without user:pass@;
- refuses a single-label name (`supervisor`, `localhost`, `homeassistant`) and `.local`;
- resolves the host itself and refuses when ANY address is not public (`is_inward`,
  also an IPv4 address inside IPv6: mapped, NAT64, 6to4);
- connects to the address it checked, not to the name: the URL gets the IP, the Host
  header and the TLS server name (SNI, so the certificate is checked for the name)
  keep the name. A second DNS answer can therefore not point the request inward.
  `Connection: close`, so a connection to that IP is never reused for another name;
- follows at most MAX_REDIRECTS redirects by hand, each hop checked the same way;
- reads the body as a stream and stops above `max_bytes`.

`parse` turns the page into an OwnRecipe (id 0, not saved) with recipe-scrapers, which
falls back on the schema.org data most recipe sites carry. Every failure is a
RecipeImportError with a plain Dutch `message` for the app.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket

import httpx

from app.recipes import OwnRecipe

log = logging.getLogger(__name__)

MAX_REDIRECTS = 5
MAX_BYTES = 2_000_000
DEADLINE = 30  # seconds for the whole fetch: each read has its own 10 s, a slow drip not
REDIRECTS = {301, 302, 303, 307, 308}
DEFAULT_SERVINGS = 4
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; weekmenu-ah)",
    "Accept": "text/html,application/xhtml+xml",
    "Connection": "close",
}

BAD_LINK = "Plak een link die begint met http:// of https://"
CREDENTIALS = "Een link met een gebruikersnaam of wachtwoord open ik niet"
BAD_PORT = "Alleen gewone websites, zonder poortnummer, kan ik openen"
INWARD = "Deze link wijst naar je eigen netwerk; die open ik niet"
NOT_FOUND = "Deze site is niet te vinden"
TOO_MANY = "Deze site stuurt te vaak door"
TOO_BIG = "Deze pagina is te groot om te lezen"
TIMEOUT = "De site reageert niet op tijd"
UNREACHABLE = "De site is niet bereikbaar"
NO_RECIPE = "Op deze pagina staat geen recept dat ik kan lezen"

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class RecipeImportError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def is_inward(addr: str) -> bool:
    """True when `addr` is not a public internet address (or is not an address)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])  # an IPv6 scope id: fe80::1%eth0
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None and is_inward(str(inner)):
            return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified or not ip.is_global)


async def resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to (a module function, so tests can replace it)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise RecipeImportError(NOT_FOUND) from None
    return [info[4][0] for info in infos]


def _url(raw) -> httpx.URL:
    if not isinstance(raw, str) or not raw.strip():
        raise RecipeImportError(BAD_LINK)
    try:
        url = httpx.URL(raw.strip())
    except (httpx.InvalidURL, ValueError):
        raise RecipeImportError(BAD_LINK) from None
    if url.scheme not in ("http", "https") or not url.host:
        raise RecipeImportError(BAD_LINK)
    if url.userinfo:
        raise RecipeImportError(CREDENTIALS)
    if url.port not in (None, 80, 443):
        raise RecipeImportError(BAD_PORT)
    return url


async def _checked_ip(url: httpx.URL) -> str:
    """The address to connect to: the host must resolve to public addresses only."""
    host = url.raw_host.decode("ascii").rstrip(".").lower()  # IDN as punycode
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is None:
        if "." not in host or host.endswith((".local", ".localhost", ".internal")):
            raise RecipeImportError(INWARD)
        addrs = await resolve(host, url.port or (443 if url.scheme == "https" else 80))
    else:
        addrs = [host]
    if not addrs:
        raise RecipeImportError(NOT_FOUND)
    if any(is_inward(a) for a in addrs):
        raise RecipeImportError(INWARD)
    v4 = [a for a in addrs if ":" not in a]
    return (v4 or addrs)[0].split("%", 1)[0]


async def _read(response: httpx.Response, max_bytes: int) -> str:
    size = response.headers.get("content-length", "")
    if size.isdigit() and int(size) > max_bytes:
        raise RecipeImportError(TOO_BIG)
    chunks, total = [], 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise RecipeImportError(TOO_BIG)
        chunks.append(chunk)
    return b"".join(chunks).decode(response.charset_encoding or "utf-8", errors="replace")


async def fetch_page(http: httpx.AsyncClient, url: str,
                     max_bytes: int = MAX_BYTES) -> tuple[str, str]:
    """(final url, html) of a public web page. Raises RecipeImportError."""
    current = _url(url)
    try:
        async with asyncio.timeout(DEADLINE):
            return await _fetch(http, current, max_bytes)
    except TimeoutError:
        raise RecipeImportError(TIMEOUT) from None


async def _fetch(http: httpx.AsyncClient, current: httpx.URL,
                 max_bytes: int) -> tuple[str, str]:
    for _hop in range(MAX_REDIRECTS + 1):
        ip = await _checked_ip(current)
        headers = {**HEADERS, "Host": current.netloc.decode("ascii")}
        name = current.raw_host.decode("ascii")
        extensions = {"sni_hostname": name} if current.scheme == "https" else {}
        try:
            async with http.stream("GET", current.copy_with(host=ip), headers=headers,
                                   extensions=extensions) as r:
                if r.status_code in REDIRECTS and r.headers.get("location"):
                    try:
                        current = _url(str(current.join(r.headers["location"])))
                    except (httpx.InvalidURL, ValueError):
                        raise RecipeImportError(BAD_LINK) from None
                    continue
                if r.status_code != 200:
                    raise RecipeImportError(f"De site gaf de pagina niet (HTTP {r.status_code})")
                return str(current), await _read(r, max_bytes)
        except httpx.TimeoutException:
            raise RecipeImportError(TIMEOUT) from None
        except httpx.HTTPError:
            raise RecipeImportError(UNREACHABLE) from None
    raise RecipeImportError(TOO_MANY)


def _get(read, default=None):
    """One field from a scraper: recipe-scrapers raises all kinds of errors for a field
    the page does not have, and the page is untrusted, so every failure is `default`."""
    try:
        value = read()
    except Exception:
        return default
    return default if value is None else value


def _lines(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x.strip() for x in value if isinstance(x, str) and x.strip()]


def _servings(yields) -> int:
    m = re.search(r"\d+", yields if isinstance(yields, str) else str(yields or ""))
    n = int(m.group()) if m else DEFAULT_SERVINGS
    return n if 1 <= n <= 20 else DEFAULT_SERVINGS


def _web_link(value) -> str | None:
    if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
        return value.strip()
    return None


def parse(html: str, url: str) -> OwnRecipe:
    """The recipe on the page at `url`, as an unsaved own recipe (id 0)."""
    from recipe_scrapers import scrape_html  # heavy: only loaded when a link is read

    try:
        s = scrape_html(html, org_url=url, supported_only=False)
    except Exception:
        raise RecipeImportError(NO_RECIPE) from None
    ingredients = _lines(_get(s.ingredients, []))
    if not ingredients:
        raise RecipeImportError(NO_RECIPE)
    title = _get(s.title, "")
    cook = _get(s.total_time)
    host = _get(s.host, "") or httpx.URL(url).host
    host = host.lower().removeprefix("www.")
    return OwnRecipe(
        id=0,
        title=title.strip() if isinstance(title, str) else "",
        servings=_servings(_get(s.yields)),
        ingredients=ingredients,
        steps=_lines(_get(s.instructions_list, [])),
        cook=cook if isinstance(cook, int) and not isinstance(cook, bool) and cook > 0
        else None,
        source=host or None,
        url=url,
        image=_web_link(_get(s.image)),
    )
