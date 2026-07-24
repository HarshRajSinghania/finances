#!/usr/bin/env python3
"""
parse_statements.py (v2) — extract transactions from credit card / bank statement
PDFs into one clean CSV.

v2 changes
----------
* Column-aware amounts. Reads each line's number positions instead of taking the
  last number, so a running-balance column no longer gets mistaken for the amount.
* Detects Debit/Credit/Balance/Withdrawal/Deposit column headers and uses them
  to set both the amount and its sign.
* Warns about duplicate statements (same account + same period parsed twice).
* Prints the 10 largest charges and per-file reconciliation so bad extractions
  are obvious immediately.
* --amount-col to override column choice, --dump-columns to inspect what it saw.

Usage
-----
    pip install pdfplumber
    python parse_statements.py ./statements -o transactions.csv
    python parse_statements.py ./statements -o transactions.csv --password 1234
    python parse_statements.py one_bad_file.pdf --dump-columns      # diagnose

Sign convention: money OUT (purchases, fees) is POSITIVE.
Money IN (payments, refunds, credits) is NEGATIVE.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

try:
    import pdfplumber
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pdfplumber")

# The shared engine decides categories. If it isn't next to this script the
# built-in rules below are used instead, so nothing hard-fails.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from categorizer import Categorizer as _Categorizer
    _CZ = _Categorizer()
    SHARED_CATEGORIZER = True
except Exception:
    _CZ = None
    SHARED_CATEGORIZER = False


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

DATE_NUM = r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
DATE_ABBR = r"(?:%s)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?" % "|".join(MONTHS)
DATE_DMY = r"\d{1,2}\s+(?:%s)[a-z]*\.?(?:\s+\d{2,4})?" % "|".join(MONTHS)
DATE_ANY = r"(?:%s|%s|%s)" % (DATE_NUM, DATE_ABBR, DATE_DMY)

# A single word that is a money value
MONEY_TOKEN = re.compile(r"^[-+]?[\$\u20b9\u00a3\u20ac]?\(?-?\d{1,3}(?:,\d{3})*\.\d{2}\)?-?$"
                         r"|^[-+]?[\$\u20b9\u00a3\u20ac]?\(?-?\d+\.\d{2}\)?-?$")
MONEY_INLINE = r"[-+]?\$?\(?-?\d{1,3}(?:,\d{3})*\.\d{2}\)?-?"

NOISE = re.compile(
    r"previous balance|new balance|minimum payment|payment due|credit limit"
    r"|available credit|cash advance limit|annual percentage rate|balance subject"
    r"|interest charge calculation|total fees charged|total interest charged"
    r"|year[- ]to[- ]date|totals? for this period|days in billing|apr\b"
    r"|account summary|page \d+ of \d+|rewards? (balance|summary|earned)"
    r"|purchases?\s+balance|corresponding apr|opening balance|closing balance"
    r"|brought forward|carried forward|statement balance|total debits|total credits",
    re.IGNORECASE)

SEC_CREDIT = re.compile(
    r"payments?\s*(and|&)?\s*(other\s*)?credits|payments?\s*/\s*credits"
    r"|credits\s*(and|&)\s*adjustments|refunds?\s*(and|&)\s*credits",
    re.IGNORECASE)
SEC_DEBIT = re.compile(
    r"^\s*(new\s+)?charges?\s*$|^\s*detail\s*$"
    r"|purchases?\s*(and|&)?\s*(other\s*)?(debits|adjustments)?$|transactions?$"
    r"|fees\s*charged|interest\s*charged|cash\s*advances|new\s+charges",
    re.IGNORECASE)

# Column header keywords -> role
HEADER_ROLES = [
    ("balance", r"^balance$|^bal$|^running\s*balance$|^closing\s*balance$"),
    ("credit",  r"^credit$|^credits$|^deposits?$|^deposit\s*\(cr\)$|^cr$|^payments?$|^money\s*in$"),
    ("debit",   r"^debit$|^debits$|^withdrawals?$|^withdrawal\s*\(dr\)$|^dr$|^charges?$|^money\s*out$"),
    ("amount",  r"^amount$|^amt$|^transaction\s*amount$|^value$"),
]
HEADER_ROLES = [(r, re.compile(p, re.I)) for r, p in HEADER_ROLES]

ISSUERS = [
    ("Chase",        r"chase\.com|jpmorgan|chase sapphire|freedom unlimited|chase ink"),
    ("Amex",         r"american express|americanexpress\.com|amex"),
    ("Citi",         r"citibank|citi\.com|citi cards|costco anywhere"),
    ("Capital One",  r"capital ?one|capitalone\.com"),
    ("Discover",     r"discover\b|discover\.com"),
    ("Bank of America", r"bank of america|bankofamerica\.com|bofa"),
    ("Wells Fargo",  r"wells ?fargo"),
    ("Apple Card",   r"apple card|goldman sachs bank"),
    ("Barclays",     r"barclay"),
    ("US Bank",      r"u\.?s\.? bank|usbank\.com"),
    ("Synchrony",    r"synchrony"),
    ("PNC",          r"pnc bank"),
    ("TD Bank",      r"td bank"),
    ("Ally",         r"ally bank"),
    ("Schwab",       r"charles schwab"),
    ("HSBC",         r"hsbc"),
    ("Amazon/Chase", r"amazon\.com (rewards|visa)"),
]

PERIOD_PATTERNS = [
    r"opening/closing date\s*:?\s*(?P<a>%s)\s*[-\u2013]\s*(?P<b>%s)" % (DATE_ANY, DATE_ANY),
    r"billing (?:period|cycle)\s*:?\s*(?P<a>%s)\s*(?:[-\u2013]|to|through)\s*(?P<b>%s)" % (DATE_ANY, DATE_ANY),
    r"statement period\s*:?\s*(?P<a>%s)\s*(?:[-\u2013]|to|through)\s*(?P<b>%s)" % (DATE_ANY, DATE_ANY),
    r"(?:for the period|period)\s*:?\s*(?P<a>%s)\s*(?:[-\u2013]|to|through)\s*(?P<b>%s)" % (DATE_ANY, DATE_ANY),
    r"(?P<a>%s)\s*[-\u2013]\s*(?P<b>%s)\s*(?:statement period|billing period)" % (DATE_ANY, DATE_ANY),
    r"closing date\s*:?\s*(?P<b>%s)" % DATE_ANY,
    r"statement (?:closing )?date\s*:?\s*(?P<b>%s)" % DATE_ANY,
]

LAST4_PATTERNS = [
    r"(?:account|card)\s*(?:number|#)?\s*ending\s*(?:in)?\s*[:#]?\s*([\dxX*\u2022-]{4,})",
    r"(?:account|card)\s*(?:number|#)\s*[:#]?\s*([\dxX*\u2022 -]{8,})",
    r"((?:x{4,}|\*{4,}|\u2022{4,})\s*\d{4})",
    r"ending\s*(?:in)?\s*([\d-]{4,})",
]

# --------------------------------------------------------------------------
# Categorization — edit freely, first match wins
# --------------------------------------------------------------------------

CATEGORIES = [
    ("Income",         r"direct dep(osit)?\b|payroll|ach credit|\bsalary\b|wages|"
                       r"irs treas|tax ref(und)?|state refund|dividend|"
                       r"interest (paid|earned)|mobile deposit|remote deposit|"
                       r"atm deposit|deposit from|zelle from|venmo from|"
                       r"cash ?app from|reimbursement|rebate"),
    ("Payment",        r"payment[\s\-–—]*thank[\s\-–—]*you|thank you for your payment|"
                       r"autopay|auto[\s\-]?pay|online payment|payment received|"
                       r"electronic payment|pymt|direct debit|payment - web|"
                       r"mobile payment|payment posted"),
    ("Credits/Refunds", r"statement credit|reward(?:s)? credit|cash ?back (?:credit|bonus)|"
                        r"credit adjustment|chargeback"),
    ("Groceries",      r"whole ?foods|trader ?joe|safeway|kroger|publix|wegmans|aldi|"
                       r"stop ?& ?shop|shoprite|h-?e-?b|sprouts|food ?lion|giant|ralphs|albertsons|"
                       r"fairway|key ?food|c-?town|grocer|supermarket|instacart|market"),
    ("Dining",         r"restaurant|cafe|coffee|starbucks|dunkin|chipotle|mcdonald|burger|pizza|"
                       r"sushi|taco|bar ?& ?grill|kitchen|bakery|deli|diner|steakhouse|panera|"
                       r"shake shack|subway|wendy|popeyes|chick-?fil|five guys|noodle|ramen|bbq|"
                       r"sweetgreen|cava |dig inn|halal|shawarma|poke|thai|indian|"
                       r"doordash|uber ?eats|grubhub|seamless|postmates|caviar|resy|opentable"),
    ("Big Box",        r"target|walmart|costco|sam'?s club|bj'?s wholesale"),
    ("Transport",      r"uber(?! ?eats)|lyft|taxi|mta|njt|nj transit|path |amtrak|metro|transit|"
                       r"parking|e-?zpass|toll|via |curb|revel"),
    ("Gas/Auto",       r"shell|exxon|mobil|chevron|bp\b|sunoco|wawa|speedway|citgo|gulf|76 |"
                       r"valero|texaco|auto ?zone|jiffy lube|midas|pep boys|geico|progressive|"
                       r"state farm|allstate|dmv"),
    ("Travel",         r"airlines?|delta air|united air|american air|jetblue|southwest|spirit air|"
                       r"alaska air|frontier|hotel|marriott|hilton|hyatt|airbnb|vrbo|booking\.com|"
                       r"expedia|priceline|kayak|hostel|resort|inn\b|tsa|clear|global entry|"
                       r"rental car|hertz|avis|enterprise rent|budget rent|turo"),
    ("Subscriptions",  r"netflix|spotify|hulu|disney ?\+|disneyplus|hbo|max\.com|paramount|peacock|"
                       r"apple\.com/bill|apple music|itunes|icloud|google storage|youtube ?premium|"
                       r"amazon prime|prime video|audible|kindle|patreon|substack|nyt|new york times|"
                       r"wsj|washington post|medium|dropbox|notion|adobe|microsoft 365|chatgpt|openai|"
                       r"anthropic|claude|midjourney|github|linkedin premium|duolingo|masterclass|"
                       r"peloton|strava|calm|headspace|xbox|playstation|nintendo|steam|twitch"),
    ("Utilities",      r"con ?ed|pse&?g|national grid|electric|utility|water dept|verizon|at&?t|"
                       r"t-?mobile|sprint|xfinity|comcast|spectrum|optimum|fios|internet|wireless|"
                       r"mint mobile|google fi"),
    ("Rent/Housing",   r"rent|landlord|property mgmt|hoa|mortgage|zillow rental|apartments"),
    ("Health",         r"pharmacy|cvs|walgreens|rite ?aid|duane reade|doctor|dental|dentist|medical|"
                       r"hospital|clinic|urgent care|labcorp|quest diag|optical|vision|therapy|"
                       r"psycholog|one ?medical|zocdoc|goodrx|health"),
    ("Fitness",        r"gym|fitness|equinox|planet fit|crunch|blink|soulcycle|yoga|pilates|"
                       r"climbing|crossfit|lifetime ath"),
    ("Shopping",       r"amazon|amzn|ebay|etsy|best ?buy|apple store|home depot|lowe'?s|ikea|"
                       r"wayfair|macy|nordstrom|uniqlo|zara|h&m|gap\b|old navy|nike|adidas|"
                       r"lululemon|sephora|ulta|shein|temu|wish\.com|newegg|b&h photo|"
                       r"micro ?center|staples|office depot|container store|marshalls|tj ?maxx|"
                       r"ross stores|nordstrom rack"),
    ("Entertainment",  r"cinema|theatre|theater|amc |regal|movie|ticketmaster|stubhub|"
                       r"live nation|eventbrite|museum|zoo|aquarium|bowling|arcade|golf|concert|dice\.fm"),
    ("Alcohol/Bars",   r"liquor|wine|brewery|brewing|tavern|pub\b|lounge|cocktail|spirits|beer"),
    ("Fees/Interest",  r"annual fee|late fee|interest charge|finance charge|foreign transaction|"
                       r"cash advance fee|returned payment|over ?limit|service charge|atm fee|"
                       r"balance transfer fee|membership fee|maintenance fee|overdraft"),
    ("Transfers/Cash", r"venmo|cash ?app|zelle|paypal|wise|remitly|western union|atm withdrawal|"
                       r"cash advance|coinbase|robinhood|fidelity|vanguard|schwab|betterment|"
                       r"wealthfront|acorns|transfer to|transfer from|wire "),
    ("Education",      r"tuition|university|college|coursera|udemy|edx|school|bookstore|student loan"),
    ("Pets",           r"petco|petsmart|chewy|veterinar|vet clinic|pet "),
    ("Personal Care",  r"barber|salon|spa\b|nails|haircut|massage|supercuts"),
    ("Insurance",      r"insurance|lemonade|policy premium"),
    ("Charity",        r"donation|charit|gofundme|red cross|unicef|foundation"),
]
CATEGORIES = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in CATEGORIES]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def parse_money(raw):
    s = str(raw).strip()
    neg = s.startswith("(") or s.endswith(")") or s.endswith("-") or s.startswith("-")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def parse_date(raw, default_year=None):
    s = re.sub(r"\s+", " ", str(raw).strip().replace(",", " ")).strip()
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$", s)
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = re.match(r"^([A-Za-z]{3})[a-z]*\.? (\d{1,2})(?: (\d{2,4}))?$", s)
        if m:
            mo = MONTHS.get(m.group(1).lower())
            dy, yr = int(m.group(2)), m.group(3)
        else:
            m = re.match(r"^(\d{1,2}) ([A-Za-z]{3})[a-z]*\.?(?: (\d{2,4}))?$", s)
            if not m:
                return None
            dy, mo, yr = int(m.group(1)), MONTHS.get(m.group(2).lower()), m.group(3)
    if not mo:
        return None
    if yr:
        yr = int(yr)
        if yr < 100:
            yr += 2000
    else:
        yr = default_year
    if yr is None:
        return None
    try:
        return date(yr, mo, dy)
    except ValueError:
        return None


def clean_merchant(desc):
    if _CZ is not None:
        from categorizer import normalize as _normalize
        cleaned, _ = _normalize(desc)
        if cleaned:
            return cleaned
    s = re.sub(r"\s{2,}", " ", desc).strip()
    s = re.sub(r"^(purchase|pos debit|debit card purchase|recurring|sale)\s+", "", s, flags=re.I)
    s = re.sub(r"\b\d{6,}\b", " ", s)
    s = re.sub(r"\b(?=[A-Z0-9]*\d)[A-Z0-9]{8,}\b", " ", s)
    s = re.sub(r"\b(\d{3}-\d{3}-\d{4}|\(\d{3}\)\s?\d{3}-\d{4})\b", " ", s)
    s = re.sub(r"\s+(?:[A-Z]{2}|[A-Z][a-z]+)\s*$", "", s)
    s = re.sub(r"[#*]{2,}", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -.,*#")
    return s or desc.strip()


def categorize(desc):
    if _CZ is not None:
        return _CZ.categorize(desc).category
    for name, rx in CATEGORIES:          # fallback: legacy built-in rules
        if rx.search(desc):
            return name
    return "Uncategorized"


def detect_issuer(text):
    low = text.lower()
    best, best_pos = "Unknown", len(low) + 1
    for name, pat in ISSUERS:
        m = re.search(pat, low)
        if m and m.start() < best_pos:
            best, best_pos = name, m.start()
    return best


def detect_last4(text):
    for pat in LAST4_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) >= 4:
                return digits[-4:]
    return ""


def detect_period(text, fallback_year):
    for pat in PERIOD_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        gd = m.groupdict()
        b = parse_date(gd.get("b") or "", fallback_year)
        a = parse_date(gd.get("a") or "", (b.year if b else fallback_year)) if gd.get("a") else None
        if a and b and a > b:
            a = parse_date(gd["a"], b.year - 1)
        if b:
            return (f"{a.isoformat()} to {b.isoformat()}" if a else b.isoformat()), b
    return "", None


def year_from_filename(path):
    m = re.search(r"(20\d{2})", os.path.basename(path))
    return int(m.group(1)) if m else None


def cluster(values, gap=14.0):
    """Group right-edge x positions into columns."""
    if not values:
        return []
    vals = sorted(values)
    groups, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= gap:
            cur.append(v)
        else:
            groups.append(cur)
            cur = [v]
    groups.append(cur)
    return [sum(g) / len(g) for g in groups]


def nearest(centers, x):
    return min(range(len(centers)), key=lambda i: abs(centers[i] - x)) if centers else None


# --------------------------------------------------------------------------
# Page / line extraction with positions
# --------------------------------------------------------------------------

def page_lines(page):
    words = page.extract_words(x_tolerance=1.5, y_tolerance=3) or []
    rows = defaultdict(list)
    for w in words:
        rows[round(w["top"] / 3.0)].append(w)
    return [sorted(rows[k], key=lambda w: w["x0"]) for k in sorted(rows)]


def words_text(ws):
    return " ".join(w["text"] for w in ws)


def money_in_line(ws):
    """Return [(index, value, right_edge_x)] for money-looking words."""
    out = []
    for i, w in enumerate(ws):
        t = w["text"]
        if not MONEY_TOKEN.match(t):
            continue
        val = parse_money(t)
        if val is None:
            continue
        nxt = ws[i + 1]["text"] if i + 1 < len(ws) else ""
        if nxt in ("-", "CR", "Cr", "cr"):
            val = -abs(val)
        elif nxt in ("DR", "Dr", "dr"):
            val = abs(val)
        out.append((i, val, w["x1"]))
    return out


def leading_dates(ws):
    """Match one or two dates at the start of a line. Returns (d1, d2, idx_after)."""
    texts = [w["text"] for w in ws]
    d1 = None
    idx = 0
    for n in (3, 2, 1):
        if n > len(texts):
            continue
        cand = " ".join(texts[:n])
        if re.fullmatch(DATE_ANY, cand, re.I):
            d1, idx = cand, n
            break
    if d1 is None:
        return None
    for n in (3, 2, 1):
        if idx + n > len(texts):
            continue
        cand = " ".join(texts[idx:idx + n])
        if re.fullmatch(DATE_ANY, cand, re.I):
            return d1, cand, idx + n
    return d1, None, idx


def column_model(rows, headers, amount_col="auto"):
    """Work out which money column on a page is the transaction amount.

    A running-balance column only exists if most transaction lines actually
    carry two or more amounts. Card statements print one amount per line, so
    nothing is ever dropped there.
    """
    centers = cluster([m[2] for c in rows for m in c["monies"]])
    n = max(len(rows), 1)
    coverage = [0] * len(centers)
    multi = 0
    for c in rows:
        seen = set()
        for _, _, x in c["monies"]:
            k = nearest(centers, x)
            if k is not None:
                seen.add(k)
        for k in seen:
            coverage[k] += 1
        if len(seen) >= 2:
            multi += 1
    coverage = [v / n for v in coverage]
    multi = multi / n

    balance = credit = debit = amount = None
    if len(centers) < 2:
        # One column of figures. There is no separate credit column to read a
        # sign from, so leave sign to the section and keyword rules.
        return {"centers": centers, "coverage": coverage, "multi": multi,
                "balance": None, "credit": None, "debit": None,
                "amount": 0 if centers else None}
    if centers:
        if "balance" in headers:
            balance = nearest(centers, headers["balance"])
        if "credit" in headers:
            credit = nearest(centers, headers["credit"])
        if "debit" in headers:
            debit = nearest(centers, headers["debit"])
        if "amount" in headers:
            amount = nearest(centers, headers["amount"])
        # No header row to go on: infer a balance column only when the lines
        # themselves look like a ledger — two amounts on most rows, the
        # rightmost present on nearly all of them.
        if balance is None and len(centers) >= 2 and multi >= 0.6 \
                and coverage[-1] >= 0.85:
            balance = len(centers) - 1

    if credit is not None and credit == debit:
        credit = debit = None
    if credit is not None and credit == amount:
        credit = None

    if amount_col == "last":
        balance = None
    elif amount_col == "second-last":
        balance = len(centers) - 1 if len(centers) >= 2 else None

    return {"centers": centers, "coverage": coverage, "multi": multi,
            "balance": balance, "credit": credit, "debit": debit, "amount": amount}


# --------------------------------------------------------------------------
# Core parse
# --------------------------------------------------------------------------

def parse_statement(path, password=None, unmatched_sink=None,
                    amount_col="auto", dump_columns=False):
    kwargs = {"password": password} if password else {}
    all_lines, pages = [], 0
    with pdfplumber.open(path, **kwargs) as pdf:
        pages = len(pdf.pages)
        for pi, page in enumerate(pdf.pages):
            for ws in page_lines(page):
                all_lines.append((pi, ws))

    full = "\n".join(words_text(ws) for _, ws in all_lines)
    if not full.strip():
        raise RuntimeError("no text layer (scanned image PDF) -> needs OCR, "
                           "or re-download the statement as a text PDF")

    fallback_year = year_from_filename(path) or datetime.fromtimestamp(
        os.path.getmtime(path)).year
    issuer = detect_issuer(full)
    last4 = detect_last4(full)
    period, closing = detect_period(full, fallback_year)
    ref_year = closing.year if closing else fallback_year
    ref_month = closing.month if closing else 12

    # Column headers are per page: page 1 of a card statement is often a
    # two-column summary while page 3 is the transaction table.
    page_headers = defaultdict(dict)
    for pi, ws in all_lines:
        hits = {}
        for w in ws:
            for role, rx in HEADER_ROLES:
                if rx.match(w["text"].strip()):
                    hits.setdefault(role, w["x1"])
        if len(hits) >= 2:
            for role, x in hits.items():
                page_headers[pi].setdefault(role, x)
    headers = {}
    for pi in page_headers:
        for role, x in page_headers[pi].items():
            headers.setdefault(role, x)

    # ---- pass 1: collect candidate transaction lines ----
    cands = []
    section = None
    last_page = None
    for pi, ws in all_lines:
        text = words_text(ws).strip()
        if not text:
            continue
        if pi != last_page:
            section, last_page = None, pi      # tables restart on a new page
        if len(text) < 80 and not money_in_line(ws):
            if SEC_CREDIT.search(text):
                section = "credit"
            elif SEC_DEBIT.search(text):
                section = "debit"
        if NOISE.search(text):
            continue

        ld = leading_dates(ws)
        if not ld:
            continue
        d1, d2, idx = ld
        monies = money_in_line(ws)
        monies = [m for m in monies if m[0] >= idx]
        if not monies:
            if unmatched_sink is not None:
                unmatched_sink.append((os.path.basename(path), text))
            continue
        desc = " ".join(w["text"] for w in ws[idx:monies[0][0]]).strip()
        if len(desc) < 2 or re.fullmatch(r"[\d\s.,/-]+", desc):
            if unmatched_sink is not None:
                unmatched_sink.append((os.path.basename(path), text))
            continue
        cands.append({"d1": d1, "d2": d2, "desc": desc, "monies": monies,
                      "section": section, "text": text, "page": pi})

    # ---- decide which money column is the amount, one page at a time ----
    models = {}
    for pi in sorted({c["page"] for c in cands}):
        rows = [c for c in cands if c["page"] == pi]
        models[pi] = column_model(rows, page_headers.get(pi) or headers, amount_col)

    if dump_columns:
        print(f"\n  columns for {os.path.basename(path)}:")
        for pi in sorted(models):
            m = models[pi]
            rows = [c for c in cands if c["page"] == pi]
            print(f"    page {pi + 1}: {len(rows)} transaction-like lines, "
                  f"{m['multi'] * 100:.0f}% carry two or more amounts")
            print(f"      headers: {page_headers.get(pi) or 'none on this page'}")
            for i, (cx, cov) in enumerate(zip(m["centers"], m["coverage"])):
                tags = []
                if i == m["balance"]: tags.append("BALANCE(ignored)")
                if i == m["credit"]: tags.append("CREDIT")
                if i == m["debit"]: tags.append("DEBIT")
                if i == m["amount"]: tags.append("AMOUNT")
                print(f"      col {i}: x={cx:7.1f}  on {cov * 100:5.1f}% of lines"
                      f"  {' '.join(tags) or '(used)'}")
            for c in rows[:4]:
                print(f"      e.g. {c['text'][:88]}")
                print(f"           amounts on the line: {[m2[1] for m2 in c['monies']]}")

    # ---- pass 2: emit ----
    txns = []
    for c in cands:
        model = models[c["page"]]
        centers = model["centers"]
        balance_col, credit_col = model["balance"], model["credit"]
        debit_col = model["debit"]
        picks = []
        for _, val, x in c["monies"]:
            k = nearest(centers, x)
            if k is not None and k == balance_col:
                continue
            picks.append((k, val))
        if not picks:
            continue
        k, amount = picks[-1]          # rightmost non-balance value
        if amount is None or amount == 0:
            continue

        d = parse_date(c["d1"], ref_year)
        if d is None:
            continue
        printed_year = re.search(r"[/-]\d{2,4}$|\s\d{4}$", c["d1"].strip())
        if not printed_year and d.month > ref_month:
            d = d.replace(year=ref_year - 1)
        # Statements are never about the future. If the period couldn't be read
        # and we guessed a year that hasn't happened, walk it back.
        if not printed_year and d > date.today() + timedelta(days=45):
            d = d.replace(year=d.year - 1)
        pd_ = parse_date(c["d2"], d.year) if c["d2"] else None

        desc = c["desc"]
        cat = categorize(desc)

        # sign: column first, then section, then keywords
        if credit_col is not None and k == credit_col:
            amount = -abs(amount)
        elif debit_col is not None and k == debit_col:
            amount = abs(amount)
        else:
            if c["section"] == "credit" and amount > 0:
                amount = -amount
            if cat in ("Payment", "Credits/Refunds", "Income") and amount > 0:
                amount = -amount
            if re.search(r"\b(refund|reversal|return|cashback|cash back|"
                         r"statement credit|adjustment)\b", desc, re.I) \
                    and amount > 0 and c["section"] != "debit":
                amount = -amount

        txns.append({
            "date": d.isoformat(),
            "post_date": pd_.isoformat() if pd_ else "",
            "description": desc,
            "merchant": clean_merchant(desc),
            "amount": round(amount, 2),
            "direction": "out" if amount > 0 else "in",
            "category": cat,
            "issuer": issuer,
            "account_last4": last4,
            "statement_period": period,
            "source_file": os.path.basename(path),
        })

    # only consider non-transaction lines when looking for statement totals
    summary_text = "\n".join(
        ln for ln in full.split("\n")
        if not re.match(r"^\s*%s\s" % DATE_ANY, ln, re.I))

    checks = {}
    for label, pat in [
        ("new_balance", r"new balance[^\d\-$(]{0,30}(%s)" % MONEY_INLINE),
        ("total_purchases", r"^[^\n]{0,20}?(?:total )?(?:purchases?(?: and adjustments)?|new charges|"
                            r"transactions)[^\d\-$(\n]{0,30}(%s)" % MONEY_INLINE),
        ("total_payments", r"(?:total )?payments?(?: and (?:other )?credits)?"
                           r"[^\d\-$(\n]{0,30}(%s)" % MONEY_INLINE),
    ]:
        m = re.search(pat, summary_text, re.IGNORECASE | re.MULTILINE)
        if m:
            checks[label] = parse_money(m.group(1))

    fields = {}
    FIELD_PATTERNS = [
        ("previous_balance", r"previous balance\s*:?\s*(%s)"),
        ("new_balance",      r"new balance(?:\s*total)?\s*:?\s*(%s)"),
        ("payments",         r"payments?(?:\s*(?:and|&|/)\s*(?:other\s*)?credits)?\s*:?\s*(%s)"),
        ("purchases",        r"(?:total\s+)?(?:purchases?|new charges)\s*:?\s*(%s)"),
        ("interest_charged", r"(?:total\s+)?interest charge[ds]?(?:\s*for this period)?\s*:?\s*(%s)"),
        ("fees_charged",     r"(?:total\s+)?fees charged(?:\s*for this period)?\s*:?\s*(%s)"),
        ("minimum_payment",  r"minimum payment(?:\s*due)?\s*:?\s*(%s)"),
        ("credit_limit",     r"(?:total\s+)?credit (?:line|limit)\s*:?\s*(%s)"),
        ("available_credit", r"available credit(?:\s*line)?\s*:?\s*(%s)"),
        ("cash_advances",    r"(?:total\s+)?cash advances?\s*:?\s*(%s)"),
    ]
    for key, pat in FIELD_PATTERNS:
        m = re.search(pat % MONEY_INLINE, summary_text, re.IGNORECASE)
        if m:
            v = parse_money(m.group(1))
            if v is not None:
                fields[key] = abs(v) if key != "payments" else -abs(v)

    m = re.search(r"purchases?[^\n%]{0,70}?(\d{1,2}\.\d{2})\s*%", summary_text, re.I)
    if m:
        fields["purchase_apr"] = float(m.group(1))
    m = re.search(r"interest charged in \d{4}\s*:?\s*(%s)" % MONEY_INLINE, summary_text, re.I)
    if m:
        fields["ytd_interest"] = abs(parse_money(m.group(1)) or 0)
    m = re.search(r"fees charged in \d{4}\s*:?\s*(%s)" % MONEY_INLINE, summary_text, re.I)
    if m:
        fields["ytd_fees"] = abs(parse_money(m.group(1)) or 0)
    m = re.search(r"(?:rewards?|cash ?back|points?|miles)\s*(?:balance|available|earned)"
                  r"\s*:?\s*\$?([\d,]+(?:\.\d{2})?)", summary_text, re.I)
    if m:
        fields["rewards_balance"] = parse_money(m.group(1))

    if fields.get("credit_limit") and fields.get("new_balance") is not None:
        fields["utilization_pct"] = round(
            fields["new_balance"] / fields["credit_limit"] * 100, 1)

    n_in = sum(1 for t in txns if t["amount"] < 0)
    credit_share = n_in / len(txns) if txns else 0.0

    n_cols = max((len(m["centers"]) for m in models.values()), default=0)
    dropped = any(m["balance"] is not None for m in models.values())
    meta = {"fields": fields,
            "pages": pages, "issuer": issuer, "last4": last4, "period": period,
            "checks": checks, "columns": n_cols,
            "credit_share": credit_share,
            "balance_col_dropped": dropped}
    return txns, meta


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Extract transactions from statement PDFs into a CSV.")
    ap.add_argument("input", help="PDF file or folder of PDFs")
    ap.add_argument("-o", "--output", default="transactions.csv")
    ap.add_argument("--password", help="password for encrypted PDFs")
    ap.add_argument("--dump-text", metavar="DIR", help="write raw extracted text per PDF")
    ap.add_argument("--dump-columns", action="store_true",
                    help="show detected money columns per file (diagnostic)")
    ap.add_argument("--amount-col", choices=["auto", "last", "second-last"], default="auto",
                    help="override which money column is the transaction amount")
    ap.add_argument("--statements", metavar="CSV",
                    help="where to write per-statement summaries "
                         "(balances, interest, APR, credit limit)")
    ap.add_argument("--keep-duplicates", action="store_true")
    args = ap.parse_args()

    if os.path.isdir(args.input):
        pdfs = sorted(os.path.join(dp, f)
                      for dp, _, fs in os.walk(args.input)
                      for f in fs if f.lower().endswith(".pdf"))
    else:
        pdfs = [args.input]
    if not pdfs:
        sys.exit(f"No PDFs found in {args.input}")

    if args.dump_text:
        os.makedirs(args.dump_text, exist_ok=True)

    for target in (args.output, args.statements):
        if not target:
            continue
        d = os.path.dirname(os.path.abspath(target))
        if os.path.isdir(d):
            continue
        try:
            os.makedirs(d, exist_ok=True)
            print(f"Created output folder {d}")
        except OSError as e:
            hint = ""
            if target.startswith("/"):
                hint = (f"\n  '{target}' starts with a slash, so it points at the top of "
                        f"the filesystem.\n  For a folder next to the script, drop the "
                        f"leading slash: -o {target.lstrip('/')}")
            sys.exit(f"Can't write to {target}: {e}{hint}")

    all_txns, unmatched, seen, periods = [], [], set(), defaultdict(list)
    stmt_rows = []
    print(f"Found {len(pdfs)} PDF(s)"
          + ("" if SHARED_CATEGORIZER else
             "  (categorizer.py not found — using built-in rules)") + "\n")

    for path in pdfs:
        name = os.path.basename(path)
        try:
            txns, meta = parse_statement(path, args.password, unmatched,
                                         args.amount_col, args.dump_columns)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}".strip().rstrip(":")
            if "password" in msg.lower() or "encrypt" in msg.lower() or "pdfminer" in msg.lower():
                msg += "  -> looks encrypted; try --password YOURPASSWORD"
            print(f"  !! {name}: {msg}")
            continue

        if args.dump_text:
            try:
                with open(os.path.join(args.dump_text, name + ".txt"), "w") as fh:
                    with pdfplumber.open(path, **({"password": args.password} if args.password else {})) as pdf:
                        fh.write("\n".join((p.extract_text() or "") for p in pdf.pages))
            except Exception:
                pass

        kept = 0
        for t in txns:
            key = (t["date"], t["description"], t["amount"], t["account_last4"])
            if not args.keep_duplicates and key in seen:
                continue
            seen.add(key)
            all_txns.append(t)
            kept += 1

        out = sum(t["amount"] for t in txns if t["amount"] > 0)
        inn = sum(-t["amount"] for t in txns if t["amount"] < 0)
        periods[(meta["issuer"], meta["last4"], meta["period"])].append(name)
        row = {"source_file": name, "issuer": meta["issuer"],
               "account_last4": meta["last4"], "statement_period": meta["period"],
               "charges_extracted": round(out, 2), "credits_extracted": round(inn, 2)}
        row.update(meta["fields"])
        stmt_rows.append(row)

        print(f"  {name}")
        print(f"    {meta['issuer']} ****{meta['last4'] or '????'} | {meta['period'] or 'period ?'}"
              f" | {meta['pages']}p | {meta['columns']} money column(s)"
              f"{' | balance column dropped' if meta['balance_col_dropped'] else ''}")
        print(f"    {kept} transactions | charges ${out:,.2f} | credits ${inn:,.2f}")
        if meta["credit_share"] > 0.6 and kept >= 5:
            print(f"    !! {meta['credit_share'] * 100:.0f}% of these are credits — on a card "
                  f"statement that is almost always a sign error, not a real month.")
            print(f"       Check --dump-columns for this file before trusting the output.")
        chk = meta["checks"]
        if chk.get("total_purchases"):
            diff = abs(abs(chk["total_purchases"]) - out)
            if diff < 0.02:
                print("    purchases check: OK")
            else:
                print(f"    purchases check: MISMATCH — statement says "
                      f"${abs(chk['total_purchases']):,.2f}, extracted ${out:,.2f} "
                      f"(off by ${diff:,.2f})")
                print(f"      re-run with --dump-columns for just this file to see why")
        else:
            print("    purchases check: no statement total found to compare against")
        print()

    if not all_txns:
        sys.exit("No transactions extracted. Try --dump-text ./raw to inspect the text layer.")

    for key, files in periods.items():
        if len(files) > 1 and key[2]:
            print(f"  ! duplicate statements for {key[0]} ****{key[1]} {key[2]}: {', '.join(files)}")

    all_txns.sort(key=lambda t: (t["date"], t["source_file"]))
    cols = ["date", "post_date", "description", "merchant", "amount", "direction",
            "category", "issuer", "account_last4", "statement_period", "source_file"]
    with open(args.output, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(all_txns)

    if stmt_rows:
        scols = ["source_file", "issuer", "account_last4", "statement_period",
                 "previous_balance", "new_balance", "purchases", "payments",
                 "interest_charged", "fees_charged", "minimum_payment", "credit_limit",
                 "available_credit", "utilization_pct", "purchase_apr", "cash_advances",
                 "ytd_interest", "ytd_fees", "rewards_balance",
                 "charges_extracted", "credits_extracted"]
        spath = args.statements or (os.path.splitext(args.output)[0].replace(
            "transactions", "statements") + "_statements.csv"
            if "transactions" not in args.output else
            os.path.join(os.path.dirname(args.output) or ".", "statements.csv"))
        with open(spath, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=scols, extrasaction="ignore")
            w.writeheader()
            w.writerows(stmt_rows)
        print(f"Wrote {len(stmt_rows)} statement summaries -> {spath}")

    if unmatched:
        log = os.path.splitext(args.output)[0] + "_unparsed.txt"
        with open(log, "w") as fh:
            for src, line in unmatched:
                fh.write(f"{src}\t{line}\n")
        print(f"{len(unmatched)} date-leading line(s) didn't parse -> {log}")

    spend = sum(t["amount"] for t in all_txns if t["amount"] > 0)
    credit = sum(-t["amount"] for t in all_txns if t["amount"] < 0)
    by_cat = defaultdict(float)
    for t in all_txns:
        if t["amount"] > 0:
            by_cat[t["category"]] += t["amount"]

    print(f"\nWrote {len(all_txns)} transactions -> {args.output}")
    print(f"Total charges ${spend:,.2f} | total credits/payments ${credit:,.2f}")
    n_out = sum(1 for t in all_txns if t["amount"] > 0)
    if n_out:
        print(f"Average charge ${spend / n_out:,.2f}  "
              f"(if this looks too high, run with --dump-columns)")

    print("\n10 largest charges — sanity check these against your statements:")
    for t in sorted((t for t in all_txns if t["amount"] > 0),
                    key=lambda t: -t["amount"])[:10]:
        print(f"  {t['date']}  ${t['amount']:>10,.2f}  {t['merchant'][:42]:<42} {t['source_file']}")

    print("\nSpend by category:")
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat:<18} ${amt:>12,.2f}  ({amt / spend * 100:4.1f}%)")


if __name__ == "__main__":
    main()