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
    rf"\s*(?:(?P<mwhole>\d+)\s+(?P<mnum>\d+)\s*/\s*(?P<mden>\d+)"  # 1 1/2
    rf"|(?P<whole>\d+)\s*(?P<mfrac>[{_FRAC}])"  # 1½
    rf"|(?P<num>\d+)\s*/\s*(?P<den>\d+)"  # 1/2
    rf"|(?P<dec>\d+(?:[.,]\d+)?)"  # 2.5 or 2,5
    rf"|(?P<frac>[{_FRAC}]))"  # ½
)
# "2-3 el", "2 à 3", "2 tot 3": a range; the top of it is what gets bought
_RANGE = re.compile(r"\s*(?:-|–|à|a|tot)(?=\s*\d|\s*[" + _FRAC + r"])")
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


def _number(t: str, pos: int = 0) -> tuple[float | None, int] | None:
    """The number at `pos` and where it ends; None when there is none (qty None: n/0)."""
    m = _NUMBER.match(t, pos)
    if not m:
        return None
    if m.group("mwhole"):
        den = int(m.group("mden"))
        qty = int(m.group("mwhole")) + int(m.group("mnum")) / den if den else None
    elif m.group("whole"):
        qty = int(m.group("whole")) + FRACTIONS[m.group("mfrac")]
    elif m.group("num"):
        den = int(m.group("den"))
        qty = int(m.group("num")) / den if den else None
    elif m.group("dec"):
        qty = float(m.group("dec").replace(",", "."))
    else:
        qty = FRACTIONS[m.group("frac")]
    return qty, m.end()


def parse_ingredient(text: str, name: str) -> Amount:
    """'1½ teen knoflook' -> Amount(1.5, 'teen', name). No number: qty and unit None.

    A range ('2-3 el', '2 à 3', '2 tot 3') gives its top; '1 1/2' is 1.5.
    """
    name = (name or "").strip()
    qty, unit, _ = _lead(html.unescape(text or "").strip())
    if qty is None:
        return Amount(None, None, name)
    return Amount(qty, unit or "stuks", name)


def _lead(t: str) -> tuple[float | None, str | None, int]:
    """The leading quantity of `t` (a range: its top), its unit (None: no unit word),
    and where they end. No usable number: (None, None, 0)."""
    got = _number(t)
    if got is None:
        return None, None, 0
    qty, end = got
    r = _RANGE.match(t, end)
    if r:
        top = _number(t, r.end())
        if top is not None and top[0] is not None:
            qty, end = top
    if qty is None:
        return None, None, 0
    w = _WORD.match(t[end:])
    unit = UNITS.get(w.group(1).lower()) if w else None
    return qty, unit, end + w.end() if unit else end


def ingredient_name(text: str) -> str:
    """'1 kg kruimige aardappelen' -> 'kruimige aardappelen': a recipe line without its
    leading quantity and unit, the name AH gives next to its own lines."""
    t = html.unescape(text or "").strip()
    return t[_lead(t)[2]:].strip() or t


# --- pantry ------------------------------------------------------------------

# "peper" is only pantry as the spice. Every other *peper (chilipeper, jalapenopeper,
# rode peper, madame jeanette) is a fresh chili and gets bought.
PEPPER_SPICE_COMPOUNDS = frozenset({"cayennepeper", "citroenpeper"})
PEPPER_SPICE_MODIFIERS = frozenset({
    "zwarte", "witte", "versgemalen", "gemalen", "grof", "grove", "fijne", "vers",
    "en", "zout", "de", "molen",
    # filler: "peper naar smaak", "een snufje peper", "beetje peper"
    "naar", "smaak", "snufje", "snuf", "beetje", "wat", "een", "het",
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


# --- v0.9: "Klopt niet" choices, before every heuristic -------------------------------

PRODUCT, SKIP, PANTRY = "product", "skip", "pantry"
KINDS = (PRODUCT, SKIP, PANTRY)


def override_key(name: str) -> str:
    """The normalised ingredient name a choice is kept under: no amount or unit in
    front, no trailing ", ..." clause (like is_pantry), folded, words only. Words between
    brackets stay: "tomaten (in blik)" is not "tomaten". ("2-3 el Olijfolie" -> "olijfolie")"""
    t = html.unescape(name or "").strip()
    got = _number(t)
    if got is not None:
        end = got[1]
        r = _RANGE.match(t, end)
        if r:
            top = _number(t, r.end())
            if top is not None:
                end = top[1]
        w = _WORD.match(t[end:])
        if w and w.group(1).lower() in UNITS:
            end += w.end()
        t = t[end:]
    t = re.sub(r",[^()]*$", "", t)  # "peper, naar smaak" -> "peper"
    return " ".join(_words(t))


# What a choice keeps of a product: who it is and its size, never this week's deal.
STORED_FIELDS = ("id", "title", "unit_size", "regular", "orderable", "category", "image")


def stored_product(d: dict) -> Product:
    """A chosen product as kept in overrides.json, without any bonus: price `regular`,
    no deal. The bonus version comes only from the bonus list of the week itself.
    Raises KeyError or TypeError/ValueError for an entry that is not a product."""
    return Product(int(d["id"]), str(d["title"]), d.get("unit_size"),
                   float(d["regular"]), float(d["regular"]), False, None,
                   bool(d.get("orderable", True)), d.get("category"),
                   image=d.get("image"))


def override_for(name: str, overrides: dict | None) -> dict | None:
    """The user's choice for this ingredient name, if any."""
    if not overrides:
        return None
    return overrides.get(override_key(name))


def is_pantry(name: str, pantry: list[str], overrides: dict | None = None) -> bool:
    """A pantry word equals a word of the name or is the tail of a compound word.

    'olijfolie' and 'zeezout' match 'olie' and 'zout'; 'oliebollen' and 'peperkoek'
    do not (the pantry word is at the start of the compound). Only the part before
    'in/op/met/voor/uit' counts ('tonijn in olie' is tuna), so 'olie voor het bakken'
    is pantry. 'peper' is pantry only as the spice. Plain (tap) water counts as pantry.

    A "Klopt niet" choice goes first: "Hebben we al" and "Hoeft niet" are never bought,
    "Ander product" always is.
    """
    ov = override_for(name, overrides)
    if ov is not None:
        return ov["kind"] in (PANTRY, SKIP)
    first = (name or "").split(",")[0]  # drop a trailing ", snufje" clause
    head = _CUT.sub("", " ".join(_words(re.sub(r"\([^)]*\)", " ", first))))
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


# "AH Rundergehakt per 100 gram": priced by weight. Without a pack size the title's
# weight is no pack, so never buy several of them to reach the recipe's weight.
_PER_WEIGHT = re.compile(r"\bper\s*(?:\d+(?:[.,]\d+)?\s*)?(?:gram|gr|g|kilo|kg)\b")


def weight_priced(p: Product) -> bool:
    """A title priced 'per 100 gram' or 'per kilo' and no pack size: 1 piece, check it."""
    return parse_unit_size(p.unit_size) is None and bool(_PER_WEIGHT.search(_fold(p.title)))


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


def pack_fraction(amount: Amount, unit_size: tuple[float, str] | None) -> float | None:
    """The amount as a part of one pack (200 g of a 500 g pack: 0.4).

    None when it cannot be converted; then only whole packs (packs_needed) count.
    """
    if amount.qty is None or unit_size is None or unit_size[0] <= 0:
        return None
    base = _TO_BASE.get(amount.unit or "")
    if base is None or base[0] != unit_size[1]:
        return None
    return amount.qty * base[1] / unit_size[0]


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


# Compound words whose last part is not what they are: pindakaas is no cheese.
NOT_A_TAIL = frozenset({"pindakaas"})
# A last part that only says the form, not what it is: kruidenmix is kruiden,
# spekblokjes is spek, kipfiletreepjes is kipfilet.
FORM_SUFFIXES = ("mix", "blokjes", "blokje", "plakjes", "reepjes", "schijfjes", "stukjes")
# Distinguishing adjectives: before the core word they must be in the title too, so "rode
# ui" never falls back on white onions. "verse" is handled apart: a plain title is fresh.
_ADJECTIVES = [("rode", "rood"), ("witte", "wit"), ("gele", "geel"), ("zoete", "zoet"),
               ("gerookte", "gerookt"), ("gedroogde", "gedroogd")]
ADJECTIVE_FORMS = {w: frozenset(group) for group in _ADJECTIVES for w in group}
FRESH_WORDS = frozenset({"verse", "vers"})
PRESERVED = ("gedroogd", "gerookt", "diepvries", "gevriesdroogd")


class _Title:
    def __init__(self, title: str):
        self.words = _words(title)
        self.forms = [_forms(w) for w in self.words]
        self.flat = set().union(*self.forms) if self.forms else set()
        self.nospace = "".join(self.words)
        self.ends = set()
        n = 0
        for w in self.words:
            n += len(w)
            self.ends.add(n)

    def has(self, kw: str, kws: list[str] = ()) -> bool:
        """The keyword is a title word, or the last part of one (kaas: geitenkaas, not
        kaasstengels or pindakaas). A distinguishing adjective may also start a word
        (rode: rodekool)."""
        if kw in ADJECTIVE_FORMS:
            return self.has_adjective(kw, kws)
        for f in _forms(kw):
            if f in self.flat:
                return True
            if len(f) >= 4 and any(
                tf.endswith(f) or any(tf.endswith(f + x) for x in FORM_SUFFIXES)
                for w, forms in zip(self.words, self.forms) if w not in NOT_A_TAIL
                for tf in forms
            ):
                return True  # compound: kipfilet in scharrelkipfilet
            if len(f) >= 6 and self._split_tail(f):
                return True  # split compound: "scharrel kip filet"
        return False

    def _split_tail(self, f: str) -> bool:
        """`f` spans title words and ends where a word ends (a plural s/en may follow)."""
        start = self.nospace.find(f)
        while start >= 0:
            rest = self.nospace[start + len(f):]
            nxt = min((e for e in self.ends if e >= start + len(f)), default=None)
            if nxt is not None and rest[: nxt - start - len(f)] in ("", "s", "en"):
                return True
            start = self.nospace.find(f, start + 1)
        return False

    def has_adjective(self, kw: str, kws=()) -> bool:
        """A whole title word ("Gelei suiker" is not gele, "Zoetzuur" not zoete), or the
        adjective glued to another keyword of the same name ("rode kool": Rodekool)."""
        forms = ADJECTIVE_FORMS[kw]
        glued = {a + f for a in forms for k in kws if k != kw for f in _forms(k)}
        return any(w in forms or w in glued for w in self.words)

    def preserved(self) -> bool:
        """Dried, smoked or frozen: not what a recipe asking for "verse" means."""
        return any(w.startswith(PRESERVED) for w in self.words)

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
# and (folded, substring) category names, each with the keyword parts that make the
# product plausible after all (a recipe that asks for appelsap or cola).
IMPLAUSIBLE_TITLE_WORDS = frozenset({
    "drank", "sap", "siroop", "ijs", "chips", "snoep", "limonade", "smoothie", "knijpfruit",
})
# Ready meals named after a dish ("Hachee stoofpeer"), fine when the title is a seasoning
# for that dish ("Kruidenmix Hachee").
READY_MEAL_TITLE_WORDS = frozenset({"hachee", "stoofpot"})
SEASONING_TITLE_PREFIXES = ("kruid", "mix", "specerij", "saus")
IMPLAUSIBLE_CATEGORIES = {
    "frisdrank": ("sap", "water", "drank", "limonade", "cola", "tonic", "bier", "beer"),
    "baby en kind": ("baby",),
}


def _plausible(p: Product, t: "_Title", kws: list[str]) -> bool:
    for m in IMPLAUSIBLE_TITLE_WORDS:
        if m in t.words and not any(m in k for k in kws):
            return False
    if any(m in t.words for m in READY_MEAL_TITLE_WORDS) and not (
        any(w.startswith(SEASONING_TITLE_PREFIXES) for w in t.words)
        or any(m in k for k in kws for m in READY_MEAL_TITLE_WORDS)
    ):
        return False
    cat = _fold(p.category or "")
    for c, exempt in IMPLAUSIBLE_CATEGORIES.items():
        if c in cat and not any(e in k for k in kws for e in exempt):
            return False
    return True


def _variant_ok(t: "_Title", wanted: frozenset[str]) -> bool:
    """A recipe asking for "verse" gets no dried, smoked or frozen product."""
    return not (wanted & FRESH_WORDS and t.preserved())


def _discount(p: Product) -> float:
    return (p.regular - p.now) / p.regular if p.regular else 0.0


def _best_search_hit(
    orderable: list[Product], kws: list[str], wanted: frozenset[str]
) -> Product | None:
    orderable = [p for p in orderable if _plausible(p, _Title(p.title), kws)
                 and _variant_ok(_Title(p.title), wanted)]
    titles = [_Title(p.title) for p in orderable]
    # 1. all keywords in the title; the plainest product (fewest unrelated words) first
    full = [(t.extras(kws, wanted), i) for i, t in enumerate(titles)
            if all(t.has(k, kws) for k in kws)]
    if full:
        return orderable[min(full)[1]]
    # 2. some keywords, but the last one (the thing itself: appel, not friszoete) must match
    part = []
    for i, t in enumerate(titles):
        hits = [t.has(k, kws) for k in kws]
        if hits[-1] and all(h for k, h in zip(kws, hits) if k in ADJECTIVE_FORMS):
            part.append((-sum(hits), t.extras(kws, wanted), i))
    return orderable[min(part)[2]] if part else None


async def choose_product(
    client,
    amount: Amount,
    bonus: list[Product],
    cache: dict | None = None,
    overrides: dict | None = None,
) -> Product | None:
    """A bonus product with all keywords in its title, else the best orderable search hit.

    Only orderable products count, also in the bonus list. Nothing that fits: None (the
    ingredient goes to Plan.not_found). Search results are cached per name in `cache`.

    A "Klopt niet" choice goes first, without any search: "Ander product" gives that
    product (its bonus version when it is in `bonus`), "Hoeft niet" and "Hebben we al"
    give None.
    """
    ov = override_for(amount.name, overrides)
    if ov is not None:
        if ov["kind"] != PRODUCT:
            return None
        chosen = stored_product(ov["product"])
        fresh = next((p for p in bonus if p.id == chosen.id and p.orderable), None)
        return fresh or chosen
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
            all(t.has(k, kws) for k in kws)
            and t.extras(kws, wanted) <= MAX_EXTRA_WORDS_BONUS
            and _plausible(p, t, kws)
            and _variant_ok(t, wanted)
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
