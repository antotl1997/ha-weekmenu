"""Recipe ingredients: parse the text, skip the pantry, pick an AH product, count packs."""

from __future__ import annotations

import html
import math
import re
import unicodedata
from dataclasses import dataclass

from app.ahclient import Product


@dataclass(frozen=True)
class Amount:
    qty: float | None
    unit: str | None
    name: str


# --- parsing the recipe text -------------------------------------------------

FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125}
_FRAC = "".join(FRACTIONS)

_NUMBER = re.compile(
    rf"^\s*(?:(?P<whole>\d+)\s*(?P<mfrac>[{_FRAC}])"  # 1½
    rf"|(?P<num>\d+)\s*/\s*(?P<den>\d+)"  # 1/2
    rf"|(?P<dec>\d+(?:[.,]\d+)?)"  # 2.5 or 2,5
    rf"|(?P<frac>[{_FRAC}]))"  # ½
)
_WORD = re.compile(r"^\s*([A-Za-z]+)\b")

UNITS = {
    "g": "g", "gr": "g", "gram": "g",
    "kg": "kg", "kilo": "kg",
    "ml": "ml",
    "l": "l", "liter": "l",
    "el": "el", "eetlepel": "el", "eetlepels": "el",
    "tl": "tl", "theelepel": "tl", "theelepels": "tl",
    "stuk": "stuks", "stuks": "stuks",
    "teen": "teen", "tenen": "teen", "teentje": "teen", "teentjes": "teen",
    "blik": "blik", "blikken": "blik", "blikje": "blik", "blikjes": "blik",
    "zak": "zak", "zakken": "zak", "zakje": "zak", "zakjes": "zak",
    "pak": "pak", "pakken": "pak", "pakje": "pak", "pakjes": "pak",
    "bos": "bos", "bossen": "bos", "bosje": "bos", "bosjes": "bos",
}


def parse_ingredient(text: str, name: str) -> Amount:
    """'1½ teen knoflook' -> Amount(1.5, 'teen', name). No number: qty and unit None."""
    name = (name or "").strip()
    t = html.unescape(text or "").strip()
    m = _NUMBER.match(t)
    if not m:
        return Amount(None, None, name)
    if m.group("whole"):
        qty = int(m.group("whole")) + FRACTIONS[m.group("mfrac")]
    elif m.group("num"):
        den = int(m.group("den"))
        qty = int(m.group("num")) / den if den else None
    elif m.group("dec"):
        qty = float(m.group("dec").replace(",", "."))
    else:
        qty = FRACTIONS[m.group("frac")]
    if qty is None:
        return Amount(None, None, name)
    w = _WORD.match(t[m.end():])
    unit = UNITS.get(w.group(1).lower()) if w else None
    return Amount(qty, unit or "stuks", name)


# --- pantry ------------------------------------------------------------------

# "peper" is only pantry as the spice. Every other *peper (chilipeper, jalapenopeper,
# rode peper, madame jeanette) is a fresh chili and gets bought.
PEPPER_SPICE_COMPOUNDS = frozenset({"cayennepeper", "citroenpeper"})
PEPPER_SPICE_MODIFIERS = frozenset({
    "zwarte", "witte", "versgemalen", "gemalen", "grof", "grove", "fijne", "vers",
    "en", "zout", "de", "molen",
})

# "tonijn in olie": only the part before in/op/met/voor/uit names the ingredient.
_CUT = re.compile(r"\b(?:voor|in|op|met|uit)\b.*$")


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", _fold(text))


# Tap water is never bought, whatever the pantry option says.
TAP_WATER_WORDS = frozenset({"water", "kraanwater", "kokend", "lauw", "koud", "warm", "heet"})


def is_pantry(name: str, pantry: list[str]) -> bool:
    """A pantry word equals a word of the name or is the tail of a compound word.

    'olijfolie' and 'zeezout' match 'olie' and 'zout'; 'oliebollen' and 'peperkoek'
    do not (the pantry word is at the start of the compound). Only the part before
    'in/op/met/voor/uit' counts ('tonijn in olie' is tuna), so 'olie voor het bakken'
    is pantry. 'peper' is pantry only as the spice. Plain (tap) water counts as pantry.
    """
    head = _CUT.sub("", " ".join(_words(re.sub(r"\([^)]*\)", " ", name or ""))))
    ws = head.split()
    if ws and set(ws) <= TAP_WATER_WORDS and ("water" in ws or "kraanwater" in ws):
        return True
    pantry_words = [w for p in pantry for w in _words(p)]
    for w in ws:
        for p in pantry_words:
            if p == "peper":
                if w in PEPPER_SPICE_COMPOUNDS or (
                    w == "peper" and all(o in PEPPER_SPICE_MODIFIERS for o in ws if o != "peper")
                ):
                    return True
            elif w == p or w.endswith(p):
                return True
    return False


# --- unit sizes and packs ----------------------------------------------------

_SIZE = re.compile(
    r"(?:(?P<mult>\d+(?:[.,]\d+)?)\s*x\s*)?(?P<n>\d+(?:[.,]\d+)?)\s*"
    r"(?P<u>milliliter|centiliter|deciliter|liter|kilo|kg|gram|stuks|stuk|ml|cl|dl|gr|g|l)\b"
)
_SIZE_FACTOR = {
    "g": ("g", 1), "gr": ("g", 1), "gram": ("g", 1), "kg": ("g", 1000), "kilo": ("g", 1000),
    "ml": ("ml", 1), "milliliter": ("ml", 1), "cl": ("ml", 10), "centiliter": ("ml", 10),
    "dl": ("ml", 100), "deciliter": ("ml", 100), "l": ("ml", 1000), "liter": ("ml", 1000),
    "stuk": ("stuks", 1), "stuks": ("stuks", 1),
}


def parse_unit_size(s: str | None) -> tuple[float, str] | None:
    """'2 x 125 g' -> (250, 'g'); '1 kg' -> (1000, 'g'); 'per stuk' -> (1, 'stuks').

    Loose goods ('Los', 'Los, per kilo') and anything unreadable give None.
    """
    t = (s or "").strip().lower()
    if not t or t.startswith("los"):
        return None
    if re.search(r"\bper stuk\b", t):
        return (1, "stuks")
    m = _SIZE.search(t)
    if not m:
        return None
    unit, factor = _SIZE_FACTOR[m.group("u")]
    n = float(m.group("n").replace(",", ".")) * factor
    if m.group("mult"):
        n *= float(m.group("mult").replace(",", "."))
    return (n, unit)


_TO_BASE = {
    "g": ("g", 1), "kg": ("g", 1000),
    "ml": ("ml", 1), "l": ("ml", 1000), "el": ("ml", 15), "tl": ("ml", 5),
    "stuks": ("stuks", 1),
}


def packs_needed(amount: Amount, unit_size: tuple[float, str] | None) -> int:
    """Packs to cover the amount, rounded up, at least 1. Not convertible: 1."""
    if amount.qty is None or unit_size is None or unit_size[0] <= 0:
        return 1
    base = _TO_BASE.get(amount.unit or "")
    if base is None or base[0] != unit_size[1]:
        return 1
    need = amount.qty * base[1]
    return max(1, math.ceil(need / unit_size[0] - 1e-9))


# --- choosing a product ------------------------------------------------------

# Size and preparation words that do not say which product to buy.
STOPWORDS = frozenset({
    "middelgrote", "middelgroot", "grote", "groot", "kleine", "klein", "verse", "vers",
    "gesneden", "fijngesneden", "gehakte", "geraspte", "geraspt", "halve", "hele", "fijne",
    "iets", "ca", "losse", "rijpe", "gesnipperde", "gewassen", "gepelde", "stijl",
    "biologische", "biologisch", "ah", "excellent",
    "in", "voor", "op", "met", "uit", "en", "van", "de", "het", "een",
})

# Plural forms that the simple rules below do not cover (matched as word endings).
PLURAL_EXCEPTIONS = {"eieren": "ei", "bladeren": "blad", "kinderen": "kind"}

_VOWELS = "aeiou"


def _forms(word: str) -> set[str]:
    """The word plus its likely singular forms (tomaten -> tomat, tomaat)."""
    out = {word}
    for suffix, repl in PLURAL_EXCEPTIONS.items():
        if word.endswith(suffix):
            out.add(word[: -len(suffix)] + repl)
    stems = set()
    if word.endswith("en") and len(word) > 3:
        stems.add(word[:-2])
    if word.endswith("s") and len(word) > 3:
        stems.add(word[:-1])
    for st in stems:
        out.add(st)
        # open syllable: tomat -> tomaat, boon from bon
        if (
            len(st) >= 3
            and st[-1] not in _VOWELS
            and st[-2] in "aeou"
            and st[-3] not in _VOWELS
        ):
            out.add(st[:-1] + st[-2] + st[-1])
    # adjective ending and diminutive: catalaanse -> catalaans, braadworstje -> braadworst
    for w in list(out):
        if len(w) > 5 and w.endswith("e"):
            out.add(w[:-1])
        for suffix in ("tje", "je"):
            if len(w) > len(suffix) + 4 and w.endswith(suffix):
                out.add(w[: -len(suffix)])
    return out


def keywords(name: str) -> list[str]:
    """Significant words of an ingredient name, without size and preparation words."""
    t = re.sub(r"\([^)]*\)", " ", name or "")
    t = _CUT.sub("", _fold(t))
    words = [w for w in re.findall(r"[a-z]+", t) if len(w) > 1]
    sig = [w for w in words if w not in STOPWORDS]
    return sig or words


# Preparation words: a product that is already cut or grated is not the plain ingredient.
PREP_WORDS = frozenset({"gesneden", "fijngesneden", "gehakte", "geraspte", "geraspt",
                        "gesnipperde", "gepelde", "gewassen"})
# Words in a product title that are not about what the product is.
SIZE_WORDS = frozenset({"pack", "per", "stuk", "stuks", "los", "gram", "kilo", "liter"})
# A bonus product with more than this many unrelated words (brand, dish) is not trusted:
# "Ola Festini peer" is ice cream, not a pear.
MAX_EXTRA_WORDS_BONUS = 1


class _Title:
    def __init__(self, title: str):
        self.words = _words(title)
        self.forms = [_forms(w) for w in self.words]
        self.flat = set().union(*self.forms) if self.forms else set()
        self.nospace = "".join(self.words)

    def has(self, kw: str) -> bool:
        for f in _forms(kw):
            if f in self.flat:
                return True
            if len(f) >= 4 and any(f in tf for forms in self.forms for tf in forms):
                return True  # compound: kipfilet in scharrelkipfilet
            if len(f) >= 6 and f in self.nospace:
                return True  # split compound: "scharrel kipfilet"
        return False

    def extras(self, kws: list[str], wanted: frozenset[str] = frozenset()) -> int:
        """Title words that say nothing about the wanted ingredient (brand, other dish).

        A preparation word (gesneden, geraspt) in the title counts as extra unless the
        recipe asks for it, so whole onions win from cut ones.
        """
        kw_forms = set().union(*(_forms(k) for k in kws)) if kws else set()
        n = 0
        for w in self.words:
            if w in PREP_WORDS and w not in wanted:
                n += 1
                continue
            if len(w) <= 2 or w in STOPWORDS or w in SIZE_WORDS:
                continue
            wf = _forms(w)
            if wf & kw_forms or any(
                (len(w) >= 3 and w in f) or (len(f) >= 4 and f in w) for f in kw_forms
            ):
                continue
            n += 1
        return n


# Products that are not the plain ingredient: drinks, sweets, ready meals. Whole title words
# and (folded, substring) category names, each with the keyword endings that make the
# product plausible after all (a recipe that asks for appelsap or cola).
IMPLAUSIBLE_TITLE_WORDS = frozenset({
    "drank", "sap", "siroop", "ijs", "chips", "snoep", "limonade", "smoothie", "knijpfruit",
    "hachee",
})
IMPLAUSIBLE_CATEGORIES = {
    "frisdrank": ("sap", "water", "drank", "limonade", "cola", "tonic", "bier"),
    "baby en kind": ("baby",),
}


def _plausible(p: Product, t: "_Title", kws: list[str]) -> bool:
    for m in IMPLAUSIBLE_TITLE_WORDS:
        if m in t.words and not any(k.endswith(m) for k in kws):
            return False
    cat = _fold(p.category or "")
    for c, exempt in IMPLAUSIBLE_CATEGORIES.items():
        if c in cat and not any(k.endswith(e) for k in kws for e in exempt):
            return False
    return True


def _discount(p: Product) -> float:
    return (p.regular - p.now) / p.regular if p.regular else 0.0


def _best_search_hit(
    orderable: list[Product], kws: list[str], wanted: frozenset[str]
) -> Product | None:
    orderable = [p for p in orderable if _plausible(p, _Title(p.title), kws)]
    titles = [_Title(p.title) for p in orderable]
    # 1. all keywords in the title; the plainest product (fewest unrelated words) first
    full = [(t.extras(kws, wanted), i) for i, t in enumerate(titles) if all(t.has(k) for k in kws)]
    if full:
        return orderable[min(full)[1]]
    # 2. some keywords, but the last one (the thing itself: appel, not friszoete) must match
    part = []
    for i, t in enumerate(titles):
        hits = [t.has(k) for k in kws]
        if hits[-1]:
            part.append((-sum(hits), t.extras(kws, wanted), i))
    return orderable[min(part)[2]] if part else None


async def choose_product(
    client,
    amount: Amount,
    bonus: list[Product],
    cache: dict | None = None,
) -> Product | None:
    """A bonus product with all keywords in its title, else the best orderable search hit.

    Only orderable products count, also in the bonus list. Nothing that fits: None (the
    ingredient goes to Plan.not_found). Search results are cached per name in `cache`.
    """
    name = (amount.name or "").strip()
    kws = keywords(name)
    if not name or not kws:
        return None
    wanted = frozenset(_words(name))

    hits = []
    for p in bonus:
        if not p.orderable:
            continue
        t = _Title(p.title)
        if (
            all(t.has(k) for k in kws)
            and t.extras(kws, wanted) <= MAX_EXTRA_WORDS_BONUS
            and _plausible(p, t, kws)
        ):
            hits.append(p)
    if hits:
        return max(hits, key=_discount)  # max keeps the first of equal discounts

    key = name.lower()
    if cache is not None and key in cache:
        results = cache[key]
    else:
        results = await client.search_products(name)
        if cache is not None:
            cache[key] = results
    return _best_search_hit([p for p in results if p.orderable], kws, wanted)
