"""AMMC (Autorité Marocaine du Marché des Capitaux) statutory filings.

Every listed issuer publishes its financial statements on ``ammc.ma`` as PDF
attachments. This module locates those filings on the AMMC index pages,
downloads the PDF and parses the primary statements (income statement,
balance sheet, cash-flow statement) straight from the document text.

>>> import madfinance as mf
>>> mf.ammc.search(year=2025, symbol="ATW")
>>> statements = mf.Ticker("ATW").statements(2025)
>>> statements["income"].head()

Requires the optional ``pdf`` extra (``pip install 'madfinance[pdf]'``) for
parsing; searching and downloading work without it.

Limitations
-----------

* Scanned filings (image-only PDFs — common for smaller issuers) raise
  :class:`SourceError` instead of returning garbage; OCR is not supported.
* Values are reported exactly as printed — usually **thousands of MAD**; the
  unit note at the top of the statement ("en milliers de dirhams") is kept in
  the PDF for reference.
* The first statement set of a filing is parsed — for banks usually the
  consolidated statements (the parent-company statements follow later in the
  same document).
"""

from __future__ import annotations

import hashlib
import io
import re
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

import pandas as pd

from . import _http
from ._exceptions import DependencyMissing, MadFinanceError, SourceError
from .cache import Cache
from .sources import bourse, tradingview
from .sources.bourse import resolve_symbol
from .utils import cache_dir, parse_fr_number

BASE = "https://www.ammc.ma"
LIST_PATH = "/fr/liste-etats-financiers-emetteurs"
REPORT_PREFIX = "/fr/espace-emetteurs/etats-financiers/"

_TTL_LIST = 7 * 86400.0        # index pages (list grows, but slowly)
_MAX_SCAN_PAGES = 60           # sequential scan budget when no year given
_MAX_SECTION_PAGES = 4         # statement span: title page + continuations

_cache = Cache("ammc")

# AMMC list-page HTML --------------------------------------------------------
_TABLE_RE = re.compile(r"<table[^>]*>([\s\S]*?)</table>", re.I)
_ROW_RE = re.compile(r"<tr[^>]*>([\s\S]*?)</tr>", re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r'href="([^"]+)"')
_PAGER_RE = re.compile(r"[?&]page=(\d+)")
_YEAR_CELL_RE = re.compile(r"^\d{4}$")

# Statement text (accent-folded) ---------------------------------------------
_SECTIONS: Dict[str, Tuple[str, ...]] = {
    "income": (
        "compte de resultat",
        "comptes de resultat",
        "etat des soldes de gestion",
        "tableau de formation des resultats",
        "etat de resultat global",
        "etat du resultat global",
    ),
    "balance": (
        "etat synthetique de la situation",
        "etat de la situation patrimoniale",
        "etat de la situation financiere",
        "bilan",
    ),
    "cashflow": (
        "etat des flux de tresorerie",
        "tableau des flux de tresorerie",
    ),
}
_SECTION_ORDER = ("income", "balance", "cashflow")

_BARE_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_TITLE_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_NUM_CELL_RE = re.compile(r"^\(?-?[\d\u00a0\u202f][\d\u00a0\u202f .,]*\)?$")
_PAGE_DATE_RE = re.compile(
    r"\b\d{1,2}/\d{1,2}/\d{4}\b|"
    r"(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|"
    r"octobre|novembre|decembre)\s+(?:19|20)\d{2}"
)
_MONTHS3 = {"jan", "fev", "mar", "avr", "mai", "jui", "aou", "sep", "oct",
            "nov", "dec"}
#: words a wrapped label usually ends on (prepositions / articles / joins) —
#: a fragment ending in one of these continues into the next line
_FUNCTION_WORDS = {"de", "des", "du", "d", "la", "le", "les", "un", "une",
                   "et", "ou", "aux", "a", "par", "pour", "sur", "en", "dans",
                   "avec", "au", "se", "son", "sa", "ses", "ce", "cette",
                   "ces", "l", "n"}
_MARKERS = "+-–—−•◦▪·*"


# ---------------------------------------------------------------------------
# list pages: search filings
# ---------------------------------------------------------------------------

def search(
    year: Optional[int] = None,
    symbol: Optional[str] = None,
    issuer: Optional[str] = None,
    kind: Optional[str] = None,
    max_pages: Optional[int] = None,
) -> pd.DataFrame:
    """Find AMMC filings; columns ``issuer, year, type, url``.

    * ``year`` — fiscal year of the filing. With a year, the ~430-page index
      is binary-searched to its year block (a handful of requests, cached).
    * ``symbol`` / ``issuer`` — filter by company (ticker, ISIN or name all
      resolve; matched fuzzily against the AMMC issuer name).
    * ``kind`` — filter on the filing type, e.g. ``"annuel"``,
      ``"1er semestre"``, ``"consolid"`` (accent-insensitive substring).
    * ``max_pages`` — page budget for a year-less scan (default 60).
    """
    if year is None and symbol is None and issuer is None and max_pages is None:
        max_pages = _MAX_SCAN_PAGES
    rows = _collect(year=year, max_pages=max_pages)

    names = [_fold(n) for n in _candidate_names(symbol)] if symbol else []
    needle = _fold(issuer) if issuer else None
    kind_f = _fold(kind) if kind else None

    keep: List[dict] = []
    for row in rows:
        folded = _fold(row["issuer"])
        if names and not any(n in folded or folded in n for n in names):
            continue
        if needle and needle not in folded and folded not in needle:
            continue
        if kind_f and kind_f not in _fold(row["type"]):
            continue
        keep.append(row)
    df = pd.DataFrame(keep, columns=["issuer", "year", "type", "url"])
    if not df.empty:
        df = df.drop_duplicates(subset="url").reset_index(drop=True)
    return df


def statements(
    symbol: str, year: int, statement: Optional[str] = None,
):
    """Statements of ``symbol``'s ``year`` filing parsed from the PDF.

    ``statement`` selects a single table — ``"income"``, ``"balance"`` or
    ``"cashflow"`` — otherwise a ``{statement: DataFrame}`` dict of everything
    found. When several filings match (annual/consolidated/semester), the most
    statutory one is used: annual > consolidated annual > semester.
    """
    symbol = resolve_symbol(symbol)
    filings = search(year=year, symbol=symbol)
    if filings.empty:
        raise SourceError(f"No AMMC filing found for {symbol} in {year}")
    filings = filings.assign(
        _rank=filings["type"].map(_type_rank)).sort_values("_rank", kind="stable")
    report = filings.iloc[0]

    pdf = fetch_pdf(pdf_url(report["url"]))
    parsed = parse_report(pdf)
    if statement is not None:
        if statement not in parsed:
            raise SourceError(
                f"{statement!r} not found in {report['type']} {year} "
                f"({report['issuer']}); parsed: {', '.join(parsed)}")
        return parsed[statement]
    return parsed


def pdf_url(report_url: str) -> str:
    """Direct PDF link of a filing's report page."""
    html = _http.get_text(_absolute(report_url), retries=2)
    for href in _HREF_RE.findall(html):
        if ".pdf" in href.lower():
            return _absolute(href.replace("&amp;", "&"))
    raise SourceError(f"No PDF attachment found on {report_url}")


def fetch_pdf(url: str) -> bytes:
    """Download a filing PDF (disk-cached; filings are immutable)."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    path = cache_dir() / "pdfs" / f"{digest}.pdf"
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    try:
        resp = _http.session().get(url, timeout=120)
    except Exception as exc:
        raise SourceError(f"PDF download failed: {exc}") from exc
    if resp.status_code != 200 or not resp.content:
        raise SourceError(f"PDF download failed: HTTP {resp.status_code} from {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return resp.content


# ---------------------------------------------------------------------------
# list-page plumbing
# ---------------------------------------------------------------------------

def _collect(year: Optional[int], max_pages: Optional[int]) -> List[dict]:
    if year is not None:
        span = _year_pages(year)
        if span is None:
            return []
        pages: List[int] = list(range(span[0], span[1] + 1))
    else:
        pages = list(range(0, max_pages or _MAX_SCAN_PAGES))
    rows: List[dict] = []
    for page in pages:
        page_rows = parse_list_html(_fetch_page(page))
        rows.extend(r for r in page_rows if year is None or r["year"] == year)
    return rows


def _fetch_page(page: int) -> str:
    key = f"html:{page}"
    cached = _cache.get_json(key, _TTL_LIST)
    if cached:
        return cached
    url = BASE + LIST_PATH + (f"?page={page}" if page else "")
    html = _http.get_text(url, retries=2)
    _cache.set_json(key, html)
    time.sleep(0.1)  # politeness between index requests
    return html


def _page_year_span(page: int) -> Optional[Tuple[int, int]]:
    years = [r["year"] for r in parse_list_html(_fetch_page(page))]
    return (min(years), max(years)) if years else None


def _year_pages(year: int) -> Optional[Tuple[int, int]]:
    """First/last index pages whose rows belong to ``year``'s block."""
    hi = _last_page()

    def has_older(p: int) -> bool:      # min-year on page <= year (or past end)
        if p > hi:
            return True
        span = _page_year_span(p)
        return span is None or span[0] <= year

    def only_older(p: int) -> bool:     # max-year on page < year (or past end)
        if p > hi:
            return True
        span = _page_year_span(p)
        return span is None or span[1] < year

    first = _bisect_left(has_older, 0, hi + 1)
    if first > hi or not has_older(first):
        return None
    last = _bisect_left(only_older, 0, hi + 1) - 1
    return (first, last) if last >= first else None


def _last_page() -> int:
    pages = [int(m) for m in _PAGER_RE.findall(_fetch_page(0))]
    return max(pages) if pages else 0


def _bisect_left(pred, lo: int, hi: int) -> int:
    while lo < hi:
        mid = (lo + hi) // 2
        if pred(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def parse_list_html(html: str) -> List[dict]:
    """Rows of the filing index: ``{issuer, year, type, url}``."""
    rows: List[dict] = []
    for table in _TABLE_RE.findall(html):
        for row_html in _ROW_RE.findall(table):
            href = next((h for h in _HREF_RE.findall(row_html)
                         if REPORT_PREFIX in h), None)
            if not href:
                continue
            cells = [_TAG_RE.sub("", c).replace("\xa0", " ").strip()
                     for c in _CELL_RE.findall(row_html)]
            cells = [c for c in cells if c]
            year_idx = next(
                (i for i, c in enumerate(cells) if _YEAR_CELL_RE.match(c)), None)
            if not year_idx:
                continue
            rows.append({
                "issuer": cells[year_idx - 1],
                "year": int(cells[year_idx]),
                "type": cells[year_idx + 1] if year_idx + 1 < len(cells) else "",
                "url": _absolute(href),
            })
    return rows


def _candidate_names(symbol: str) -> List[str]:
    """Names that may identify ``symbol`` on the AMMC site."""
    symbol = symbol.strip().upper()
    names: List[str] = [symbol]
    try:
        for u in bourse.get_universe():
            if (u.get("symbol") or "").upper() == symbol:
                names.extend([u.get("name"), u.get("name_en")])
    except MadFinanceError:
        pass
    try:
        for row in tradingview.scan_market():
            if (row.get("ticker") or "").upper() == symbol and row.get("long_name"):
                names.append(row["long_name"])
    except MadFinanceError:
        pass
    return [n for n in names if n]


def _type_rank(type_name: str) -> int:
    folded = _fold(type_name)
    if folded == "rapports annuels":
        return 0
    if "consolide" in folded and "annuel" in folded:
        return 1
    if "annuel" in folded:
        return 2
    if "semestr" in folded:            # semestre / semestriel / semestriels
        return 3
    return 4


def _absolute(href: str) -> str:
    return href if href.startswith("http") else BASE + href


def _fold(text: str) -> str:
    """Accent-free, lowercase, space-normalized text for fuzzy matching."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_ = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", ascii_.lower()).strip()


# ---------------------------------------------------------------------------
# PDF statement parsing (pure text/layout heuristics)
# ---------------------------------------------------------------------------

def parse_report(pdf: bytes) -> Dict[str, pd.DataFrame]:
    """Parse the primary statements of a filing's PDF.

    Returns ``{statement: DataFrame}`` with French line items as index and
    fiscal years (newest first) as columns. Scanned (image-only) PDFs raise
    :class:`SourceError`.
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise DependencyMissing("AMMC PDF statement parsing", "pdf") from exc

    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        texts = [page.extract_text() or "" for page in doc.pages]
        if not any(t.strip() for t in texts):
            raise SourceError(
                "AMMC PDF has no extractable text (scanned document); "
                "OCR is not supported")

        result: Dict[str, pd.DataFrame] = {}
        for kind, candidates in _locate_sections(texts).items():
            for start in candidates:      # best-first; dividers parse empty
                # a statement can share its page with other tables (e.g. changes
                # in equity above the cash-flow): parse only below the title line
                start_top = _title_top(_group_lines(
                    doc.pages[start].extract_words(
                        x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False)),
                    kind)
                frame = _parse_section(
                    _section_pages(start, kind, texts, doc),
                    _title_year(texts[start].splitlines()), start_top)
                if frame is not None and not frame.empty:
                    result[kind] = frame
                    break
        result = {kind: result[kind] for kind in _SECTION_ORDER
                  if kind in result}

    if not result:
        raise SourceError("No primary statements could be parsed from the AMMC PDF")
    return result


def _section_pages(start: int, kind: str, texts: List[str], doc) -> list:
    """Statement pages: title page plus continuations until the next table."""
    pages = [doc.pages[start]]
    for j in range(start + 1, min(start + _MAX_SECTION_PAGES, len(texts))):
        page_text = texts[j]
        if not _PAGE_DATE_RE.search(_fold(page_text)):
            break                       # dates mark statement tables
        real_kinds = {k for k in (_section_on_line(line)
                                  for line in page_text.splitlines()) if k}
        if real_kinds - {kind}:
            break                       # another statement starts here
        pages.append(doc.pages[j])
    return pages


def _locate_sections(page_texts: List[str]) -> Dict[str, List[int]]:
    """Candidate start pages per statement kind, best score first.

    The score prefers dated, consolidated, all-caps titles so the real
    statements beat five-year summaries; divider pages (statement names, no
    table) parse empty and fall through to the next candidate.
    """
    candidates: Dict[str, List[Tuple[int, int]]] = {}
    for idx, text in enumerate(page_texts):
        if not text.strip():
            continue
        best: Dict[str, int] = {}
        for line in text.splitlines():
            kind = _section_on_line(line)
            if kind:
                best[kind] = max(best.get(kind, -1), _title_score(line))
        for kind, score in best.items():
            candidates.setdefault(kind, []).append((score, idx))
    return {kind: [idx for score, idx in sorted(cands, key=lambda c: (-c[0], c[1]))]
            for kind, cands in candidates.items()}


def _title_score(line: str) -> int:
    folded = _fold(line)
    return ((2 if _PAGE_DATE_RE.search(folded) or _PAGE_DATE_RE.search(line) else 0)
            + (1 if "consolid" in folded else 0)
            + (1 if line.isupper() else 0))


def _section_on_line(line: str) -> Optional[str]:
    """Statement kind whose title appears on ``line`` (accent-insensitive).

    Real titles are short, start with the pattern and either carry the period
    ("Bilan consolidé au 31 décembre 2025 (en milliers de dirhams)") or are
    bare ("TABLEAU DES FLUX DE TRÉSORERIE CONSOLIDÉS"). This rejects
    table-of-contents leaders, wrapped prose fragments (lowercase starts),
    sidebar items with glued page numbers ("Bilan Hors bilan7") and note
    headings that merely mention a statement mid-line.
    """
    if "..." in line or len(line) > 100:
        return None                            # table-of-contents leaders
    stripped = line.lstrip(" •-\t")
    folded = _fold(stripped)
    if not folded:
        return None
    first_alpha = next((c for c in stripped if c.isalpha()), "")
    if first_alpha and first_alpha != first_alpha.upper():
        return None                            # sentence fragments start lowercase
    # folded matches "31 décembre 2025"; raw matches slash dates "31/12/2025"
    # which folding collapses into year-less number groups
    has_date = (bool(_PAGE_DATE_RE.search(folded))
                or bool(_PAGE_DATE_RE.search(stripped))
                or len(_TITLE_YEAR_RE.findall(stripped)) >= 2)
    if not has_date and any(c.isdigit() for c in stripped):
        return None                            # "Bilan Hors bilan7", "3. NOTES ..."
    for kind in _SECTION_ORDER:
        for pattern in _SECTIONS[kind]:
            if folded.startswith(pattern) or (has_date and pattern in folded):
                if kind == "balance" and folded.startswith("hors"):
                    continue                   # off-balance-sheet is not the bilan
                return kind
    return None


def _parse_section(pages: list, title_year: Optional[int],
                   start_top: Optional[float]) -> Optional[pd.DataFrame]:
    """One statement's rows from its pdfplumber pages (coordinate parsing)."""
    rows: Dict[str, Dict[int, float]] = {}
    anchors: Optional[list] = None
    pending: Optional[str] = None
    for i, page in enumerate(pages):
        page_rows, anchors, pending = _page_rows(
            page, anchors, title_year, start_top if i == 0 else None, pending)
        for label, values in page_rows:
            slot = rows.setdefault(label, {})
            for year_value, value in values.items():
                slot.setdefault(year_value, value)

    if not rows:
        return None
    years = sorted({y for values in rows.values() for y in values}, reverse=True)
    data = {y: {label: values.get(y, float("nan"))
                for label, values in rows.items()} for y in years}
    frame = pd.DataFrame(data)
    frame.index.name = "Item"
    return frame


def _title_top(lines: list, kind: str) -> Optional[float]:
    """Vertical position of the statement title line on its page."""
    for line_words in lines:
        if _section_on_line(" ".join(w["text"] for w in line_words)) == kind:
            return min(w["top"] for w in line_words) - 2.0
    return None


def _page_rows(page, carried: Optional[list], title_year: Optional[int],
               min_top: Optional[float] = None, pending: Optional[str] = None):
    """Rows of one page via word coordinates; returns ``(rows, anchors, pending)``.

    Columns are identified from the date header (``31/12/2025  31/12/2024``):
    each column's right edge anchors value assignment, which survives labels
    overflowing into the table area — a common layout in Moroccan filings
    where whitespace-based splitting merges columns into giant numbers.

    ``pending`` carries an unfinished wrapped label from the previous page:
    label-only lines (text wrapping around a value box) are merged into the
    following row, and reset at the header line so column notes ("Note") and
    titles never leak into row labels.
    """
    words = page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False)
    if min_top is not None:      # skip everything above the statement title
        words = [w for w in words if w["top"] >= min_top]
    lines = _group_lines(words)
    anchors, header_idx = _line_anchors(lines)
    if anchors is None:
        anchors = carried
    char_w = _char_width(words)
    rows: list = []
    for i, line_words in enumerate(lines):
        if i == header_idx:
            pending = None        # never carry a column note into row labels
            continue
        line_rows, orphan = _line_rows(
            line_words, anchors, char_w, title_year, pending)
        if line_rows:
            pending = None
        elif orphan:
            pending = f"{pending} {orphan}".strip() if pending else orphan
        else:
            pending = None        # footer / furniture / blank-value line
        rows.extend(line_rows)
    return rows, anchors, pending


def _group_lines(words: list) -> list:
    """Cluster words into text lines by vertical position."""
    lines: list = []
    current: list = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if current and abs(word["top"] - current[0]["top"]) > 3.0:
            lines.append(sorted(current, key=lambda w: w["x0"]))
            current = []
        current.append(word)
    if current:
        lines.append(sorted(current, key=lambda w: w["x0"]))
    return lines


def _column_year(text: str) -> Optional[int]:
    """Year of a column-header token: ``31/12/2025``, ``31-déc.-23``, ``2025``."""
    parts = _fold(text).split()
    if len(parts) == 1 and _BARE_YEAR_RE.match(text.strip()):
        return int(parts[0])
    if (len(parts) == 3 and parts[0].isdigit() and parts[2].isdigit()
            and (parts[1].isdigit() or parts[1][:3] in _MONTHS3)):
        year = parts[2]
        if len(year) == 4:
            return int(year)
        if len(year) == 2:
            return 2000 + int(year)
    return None


def _line_anchors(lines: list) -> Tuple[Optional[list], Optional[int]]:
    """``([(x1, year)], header_line_idx)`` of the column-header line.

    The trailing words must be date/year tokens in monotone order (either
    direction — some issuers run oldest-first) and at least two, or a single
    full date with a short lead-in. This keeps page footers
    ("... Rapport financier 2025 97") and prose years out.
    """
    for idx, words in enumerate(lines):
        run = []
        for word in reversed(words):
            year = _column_year(word["text"])
            if year is None:
                break
            run.append((word["x1"], year))
        run.reverse()
        if not run:
            continue
        years = [y for _, y in run]
        if len(set(years)) != len(years):
            continue
        if not (years == sorted(years) or years == sorted(years, reverse=True)):
            continue
        if len(run) >= 2:
            return sorted(run), idx   # ordered by x, left to right
        full_date = len(_fold(words[-1]["text"]).split()) == 3
        if full_date and len(words) <= 9:
            return sorted(run), idx
    return None, None


def _line_rows(words: list, anchors: Optional[list], char_w: float,
               title_year: Optional[int],
               pending: Optional[str] = None) -> Tuple[list, Optional[str]]:
    """``([(label, {year: value})], orphan_label)`` of one text line.

    A label-only line (text wrapping around a value box) is returned as the
    orphan so the caller can merge it into the following row.
    """
    if not words:
        return [], None
    # numeric words -> value groups, split where the x-gap exceeds word spacing
    groups: List[list] = []
    prev = None
    for word in words:
        if not _NUM_CELL_RE.match(word["text"]):
            prev = None
            continue
        if prev is not None and word["x0"] - prev["x1"] <= 1.8 * char_w:
            groups[-1].append(word)
        else:
            groups.append([word])
        prev = word
    if not groups:
        label, _ = _strip_marker(" ".join(w["text"] for w in words).strip())
        if re.search(r"[A-Za-zÀ-ÿ]", label):
            return [], label          # label-only line: a wrapped fragment
        return [], None               # title/header/number-only line
    in_groups = {id(word) for group in groups for word in group}
    label, negate = _strip_marker(
        " ".join(w["text"] for w in words if id(w) not in in_groups).strip())
    if pending:
        if not label:
            label = pending           # values-only line: its label wrapped above
        elif _fold(pending).split()[-1:] and \
                _fold(pending).split()[-1] in _FUNCTION_WORDS:
            label = f"{pending} {label}".strip()   # "... et aux" + continuation
    if not label or not re.search(r"[A-Za-zÀ-ÿ]", label):
        return [], None               # page furniture or bare numbers
    folded_label = _fold(label)
    if ("rapport financier" in folded_label
            or "rapport annuel" in folded_label
            or "document d enregistrement" in folded_label):
        return [], None               # page footer ("Attijariwafa ... 2025 97")

    assigned: Dict[int, Optional[float]] = {}
    if anchors:
        tol = (min(abs(anchors[i + 1][0] - anchors[i][0])
                   for i in range(len(anchors) - 1)) / 2
               if len(anchors) > 1 else 4.0 * char_w)
        for group in groups:
            right = group[-1]["x1"]
            k = min(range(len(anchors)), key=lambda i: abs(anchors[i][0] - right))
            if abs(anchors[k][0] - right) <= tol:
                assigned[anchors[k][1]] = _parse_num(
                    " ".join(w["text"] for w in group))
    elif title_year is not None:
        for i, group in enumerate(groups):   # unknown columns: Y, Y-1, ...
            assigned[title_year - i] = _parse_num(
                " ".join(w["text"] for w in group))
    if not assigned:
        return [], None
    if negate:
        assigned = {y: (-v if v is not None and v > 0 else v)
                    for y, v in assigned.items()}
    return [(label, assigned)], None


def _char_width(words: list) -> float:
    """Median glyph advance — a proxy for the space width on the page."""
    widths = [(w["x1"] - w["x0"]) / max(len(w["text"]), 1) for w in words]
    return sorted(widths)[len(widths) // 2] if widths else 2.5


def _title_year(lines: List[str]) -> Optional[int]:
    for line in lines[:6]:
        if _section_on_line(line):
            match = _TITLE_YEAR_RE.search(line)
            if match:
                return int(match.group(0))
    return None


def _strip_marker(label: str) -> Tuple[str, bool]:
    """Leading ``+``/``-``/bullet markers; ``-`` means the line is a charge."""
    negate = False
    stripped = label.lstrip()
    while stripped and stripped[0] in _MARKERS:
        if stripped[0] in "-–—−":
            negate = True
        stripped = stripped[1:].lstrip()
    return stripped, negate


def _parse_num(cell: str) -> Optional[float]:
    """Numeric cell in French format; ``(...)`` is negative, ``.`` thousands."""
    text = cell.strip()
    paren = text.startswith("(") and text.endswith(")")
    if paren:
        text = text[1:-1].strip()
    if text.count(".") >= 2:
        text = text.replace(".", "")       # 1.234.567 — dotted thousands
    elif "." in text:
        text = text.replace(".", ",")      # decimal dot -> FR comma
    value = parse_fr_number(text)
    if value is not None and paren:
        value = -value
    return value


__all__ = [
    "search", "statements", "pdf_url", "fetch_pdf", "parse_report",
    "parse_list_html", "BASE",
]
