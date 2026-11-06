#!/usr/bin/env python3
"""
categorizer.py — one place that decides what a transaction is.

Every other script imports this. Before it existed the taxonomy was defined in
four files with four different category lists, so an LLM label of "Coffee" or
"Lodging" arrived downstream as an unknown.

    from finances.categorizer import categorize, Categorizer, TAXONOMY

Command line (a source is a .db or a .csv; it defaults to finances.db):
    finances categorize apply --db finances.db        # re-categorise in place
    finances categorize apply finances.db -o categorized.csv
    finances categorize stats finances.db             # coverage by DOLLARS
    finances categorize review finances.db            # teach it, biggest money first
    finances categorize eval                          # score against the benchmark
    finances categorize explain "SQ *BLUE BOTTLE COFFEE OAKLAND CA"

How it decides, in order. First hit wins, and every answer records which rule
fired and how sure it is:

  1. what you taught it          (learned_rules.json — always wins)
  2. bank transaction type       read off the raw line: a Zelle, an ATM deposit,
                                 a book transfer, a card payment. These are the
                                 rows that must never be counted as spending.
  3. unambiguous phrases         "CASH ADVANCE FEE" is a fee, not a cash advance
  4. delivery platforms          you paid the platform, not the restaurant
  5. context rules               COSTCO+GAS is fuel, UBER+EATS is delivery
  6. brand table                 exact token/phrase match, never substring
  7. processor hint              TST* is a restaurant POS, IC* is Instacart
  8. keyword rules               word-boundary regex on the cleaned name
  9. glued brand                 "CinemarkDaytonOH" — city welded to the name
 10. issuer category label       Capital One and Discover print the MCC in words
 11. behaviour                   cadence and amount, when the name says nothing
 12. Uncategorized               with the reason it couldn't tell

Standard library only.
"""

import argparse
import csv
import json
import os
import re
import statistics as stats
import sys
from collections import Counter, defaultdict
from datetime import datetime

LEARNED = "learned_rules.json"
DEFAULT_SOURCE = os.environ.get("FINANCES_DB") or "finances.db"   # as database.DEFAULT_DB

# --------------------------------------------------------------------------
# One taxonomy. Groups let other scripts ask questions without their own lists.
# --------------------------------------------------------------------------

TAXONOMY = {
    # category: group
    "Groceries": "essential", "Dining": "discretionary", "Coffee": "discretionary",
    "Delivery": "discretionary", "Alcohol/Bars": "discretionary",
    "Transport": "essential", "Gas/Auto": "essential", "Travel": "discretionary",
    "Lodging": "discretionary", "Shopping": "discretionary", "Big Box": "discretionary",
    "Electronics": "discretionary", "Clothing": "discretionary", "Home": "discretionary",
    "Subscriptions": "discretionary", "Software/Tools": "discretionary",
    "Gaming": "discretionary", "Entertainment": "discretionary",
    "Utilities": "essential", "Phone/Internet": "essential",
    "Rent/Housing": "essential", "Insurance": "essential",
    "Health": "essential", "Pharmacy": "essential", "Fitness": "discretionary",
    "Personal Care": "discretionary", "Education": "essential", "Childcare": "essential",
    "Pets": "discretionary", "Charity": "discretionary", "Gifts": "discretionary",
    "Fees/Interest": "waste", "Taxes": "essential",
    "Debt Payment": "debt",
    "Transfers/Cash": "non-spend", "Payment": "non-spend",
    "Credits/Refunds": "non-spend", "Income": "non-spend",
    "Uncategorized": "unknown",
}
CATEGORIES = list(TAXONOMY)
NON_SPEND = {c for c, g in TAXONOMY.items() if g == "non-spend"}
DISCRETIONARY = {c for c, g in TAXONOMY.items() if g == "discretionary"}
ESSENTIAL = {c for c, g in TAXONOMY.items() if g == "essential"}
WASTE = {c for c, g in TAXONOMY.items() if g == "waste"}


# --------------------------------------------------------------------------
# Normalisation — the single biggest win. Banks bury the merchant in noise.
# --------------------------------------------------------------------------

# Preambles the issuer bolts on the front. Chase checking is the worst offender:
# every line starts with the transaction type and the posting date, so the
# merchant lands three or four tokens in and every rule downstream misses it.
# "Card Purchase Return" is deliberately excluded — that word is the only thing
# telling us the row is a refund, and it is read before this ever runs.
PREAMBLE = re.compile(
    r"^\s*(?:"
    r"purchase\s+authorized\s+on\s+\d{1,2}/\d{1,2}|"
    r"recurring\s+payment\s+authorized\s+on\s+\d{1,2}/\d{1,2}|"
    r"pos\s+(?:debit|purchase|withdrawal)|debit\s+card\s+purchase|"
    r"checkcard\s+\d{2,4}|check\s?card|visa\s+(?:debit|purchase)|"
    r"ach\s+(?:debit|payment)|electronic\s+(?:debit|payment)|"
    r"recurring\s+card\s+purchase(?:\s+\d{1,2}/\d{1,2})?|"
    r"card\s+purchase(?!\s+return)(?:\s+with\s+pin)?(?:\s+\d{1,2}/\d{1,2})?|"
    r"pay\s+in\s+4\s+deposit\s*-?\s*(?:pos\s+debit)?|"
    r"purchase\s+at|payment\s+to|"
    r"recurring\s+(?:debit\s+card|payment)|preauthorized\s+(?:debit|wd)"
    r")\s*",
    re.I)

# Phone wallets prefix the real merchant and mean nothing about what was bought.
WALLET = re.compile(r"^\s*(?:aplpay|apple\s*pay|googlepay|google\s*pay|gpay|"
                    r"samsung\s*pay|tap\s*&?\s*pay)\s*", re.I)

# Capital One and Discover print their own merchant-category label at the end of
# the line. It is the issuer's own classification of the merchant, which beats
# guessing from a name we could not read — so it is kept as a last-resort hint
# rather than thrown away with the rest of the trailing noise.
MCC_LABELS = [
    ("travel/entertainment", "Travel"),
    ("government services", None),
    ("professional services", None),
    ("department stores", "Shopping"),
    ("home improvement", "Home"),
    ("medical services", "Health"),
    ("warehouse clubs", "Big Box"),
    ("grocery stores", "Groceries"),
    ("gas stations", "Gas/Auto"),
    ("fast food", "Dining"),
    ("supermarkets", "Groceries"),
    ("restaurants", "Dining"),
    ("merchandise", "Shopping"),
    ("gasoline", "Gas/Auto"),
    ("automotive", "Gas/Auto"),
    ("education", "Education"),
    ("utilities", "Utilities"),
    ("insurance", "Insurance"),
    ("services", None),
]
_MCC_RX = re.compile(r"\s+(" + "|".join(re.escape(p) for p, _ in MCC_LABELS) + r")\s*$",
                     re.I)
_MCC_INDEX = {p: c for p, c in MCC_LABELS}

# "SQ *MERCHANT" — the processor, then the merchant we actually want
PROCESSORS = [
    ("square", r"^sq\s*\*+\s*"), ("toast", r"^tst\s*\*+\s*"),
    ("paypal", r"^(?:pp|paypal)\s*\*+\s*"), ("shopify", r"^sp\s+(?=\w)"),
    ("instacart", r"^ic\s*\*+\s*"), ("wepay", r"^wpy\s*\*+\s*"),
    ("eventbrite", r"^eb\s*\*+\s*"), ("stripe", r"^(?:stripe|str)\s*\*+\s*"),
    ("clover", r"^clv\s*\*+\s*"), ("2checkout", r"^2co\s*\*+\s*"),
    ("ticketmaster", r"^tm\s*\*+\s*"),
]
PROCESSORS = [(n, re.compile(p, re.I)) for n, p in PROCESSORS]

TRAILING_JUNK = [
    r"\bS\d{12,}\b",                       # Wells Fargo auth strings
    r"\bcard\s*(?:ending\s*in\s*)?\d{4}\b",
    r"\bref\s*#?\s*\d+\b",
    r"\b(?:ppd|ccd|web|arc|tel)\s*id\s*:?\s*\d*",
    r"\btransaction\s*#\s*:?\s*\d*",
    r"\bid\s*:\s*\S+",
    r"\baba/?contr\s*bnk-?\d*",            # Chase real-time-payment routing noise
    r"\bfrom\s*:\s*$",
    r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b",      # phone
    r"\b\d{10,}\b",                        # long reference numbers
    r"\bhttps?://\S+", r"\bwww\.",
    r"#\s*\d+\b", r"\bstore\s*\d+\b", r"\b\d{2}/\d{2}\b",
    r"\.{2,}\d+",                          # Chase account stubs: ...4321
    r"\b[A-Z0-9]{6,}\d[A-Z0-9]*\b",        # auth codes: letters AND digits
]
TRAILING_JUNK = [re.compile(p, re.I) for p in TRAILING_JUNK]

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR",
}
# Only stripped when they trail a state, so "WASHINGTON POST" survives
CITY_WORDS = {
    "NEW","YORK","SAN","FRANCISCO","LOS","ANGELES","JERSEY","CITY","BROOKLYN",
    "SEATTLE","CHICAGO","BOSTON","AUSTIN","DENVER","MIAMI","ATLANTA","HOBOKEN",
    "NEWARK","SECAUCUS","OAKLAND","QUEENS","BRONX","HOUSTON","DALLAS","PHOENIX",
    "PORTLAND","NASHVILLE","ELIZABETH","MONTVALE","LUXEMBOURG","MIDTOWN","FLATIRON",
}
NOISE_WORDS = {
    "INC","LLC","LTD","CORP","CO","THE","USA","US","COM","INTL","INTERNATIONAL",
    "PURCHASE","PAYMENT","POS","DEBIT","CREDIT","CARD","TRANSACTION","AUTH",
}


def normalize(descriptor):
    """Cleaned merchant name plus whatever hints the noise gave up.

    Hints are facts recovered from the parts we then delete: which payment
    processor fronted the charge, and what the issuer itself called the
    merchant. Both are worth more than the reference numbers they sit next to.
    """
    s = str(descriptor or "").strip()
    hints = set()

    m = _MCC_RX.search(s)
    if m:
        s = s[:m.start()]
        hints.add("mcc:" + m.group(1).lower())

    s = PREAMBLE.sub("", s)
    s = WALLET.sub("", s)
    # Strip the TLD but keep the name: AMAZON.COM -> AMAZON, APPLE.COM/BILL -> APPLE BILL
    s = re.sub(r"\.(?:com|net|org|io|co|ai|app|dev|tv|xyz|cloud|tech|gov|edu|us|biz)\b",
               " ", s, flags=re.I)
    for name, rx in PROCESSORS:
        if rx.search(s):
            s = rx.sub("", s)
            hints.add(name)
            break

    for rx in TRAILING_JUNK:
        s = rx.sub(" ", s)

    s = re.sub(r"[*_/\\|]+", " ", s)
    s = s.replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^A-Za-z0-9&\-. ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().upper()

    toks = [t.strip(".-") for t in s.split()]
    toks = [t for t in toks if t]
    # Reference and store numbers first: otherwise a trailing "CARD 1234" stops
    # the tail-stripping below dead, and every Chase line keeps the word CARD.
    toks = [t for t in toks if not (t.isdigit() and len(t) >= 4)]

    # Peel the tail: a state, the city words that led up to it, issuer noise,
    # and any leftover number. Repeated because Chase prints city, state and
    # card number in that order and one pass only removes the last of them.
    peeled = True
    while peeled and toks:
        peeled = False
        if toks[-1] in US_STATES:
            toks.pop()
            peeled = True
            while toks and toks[-1] in CITY_WORDS:
                toks.pop()
        elif toks[-1] in NOISE_WORDS or toks[-1].isdigit():
            toks.pop()
            peeled = True

    return " ".join(toks[:6]), hints


# --------------------------------------------------------------------------
# Brand table: exact token/phrase matches. Never substrings.
# --------------------------------------------------------------------------

BRANDS = {
    "Groceries": [
        "TRADER JOE", "TRADER JOES", "WHOLE FOODS", "WHOLEFDS", "SAFEWAY", "KROGER",
        "PUBLIX", "WEGMANS", "ALDI", "SHOPRITE", "STOP & SHOP", "STOP SHOP", "HEB",
        "SPROUTS", "FOOD LION", "RALPHS", "ALBERTSONS", "FAIRWAY", "KEY FOOD",
        "CTOWN", "H MART", "HMART", "WESTSIDE MARKET", "GRISTEDES", "MORTON WILLIAMS",
        "INSTACART", "FRESHDIRECT", "HARRIS TEETER", "GIANT FOOD", "MEIJER",
        "WINCO", "VONS", "PATHMARK", "ACME MARKETS", "SPROUTS FARMERS",
        # New England, which is where this dataset actually shops
        "BIG Y", "BIG Y WORLD CLASS MARKET", "MARKET BASKET", "STAR MARKET",
        "PRICE CHOPPER", "HANNAFORD", "ROCHE BROS", "RUSSO", "DAIRY MART",
    ],
    "Coffee": [
        "STARBUCKS", "DUNKIN", "BLUE BOTTLE", "PHILZ", "PEETS", "PEET S",
        "CARIBOU COFFEE", "TIM HORTONS", "COSTA COFFEE", "GREGORYS COFFEE",
        "JOE COFFEE", "LA COLOMBE", "INTELLIGENTSIA", "STUMPTOWN",
    ],
    "Delivery": [
        "DOORDASH", "UBER EATS", "UBEREATS", "GRUBHUB", "SEAMLESS", "POSTMATES",
        "CAVIAR", "GOPUFF", "GETIR", "SLICE", "CHOWNOW",
    ],
    "Dining": [
        "CHIPOTLE", "SWEETGREEN", "SHAKE SHACK", "PANERA", "MCDONALD", "MCDONALDS",
        "BURGER KING", "WENDY", "WENDYS", "POPEYES", "CHICK-FIL-A", "CHICKFILA",
        "FIVE GUYS", "SUBWAY", "TACO BELL", "HALAL GUYS", "JOE S PIZZA",
        "JOES PIZZA", "DIG INN", "CAVA", "PRET A MANGER", "NOODLES",
        "OLIVE GARDEN", "CHEESECAKE FACTORY", "IHOP", "DENNYS", "APPLEBEES",
        "DOMINOS", "DOMINO S", "PAPA JOHNS", "PANDA EXPRESS", "WINGSTOP",
        "RAISING CANES", "ANTONIOS", "BERTUCCIS", "BIRYANI", "HALAL",
        "MOES SOUTHWEST", "QDOBA", "JERSEY MIKES", "SBARRO",
    ],
    "Alcohol/Bars": [
        "TOTAL WINE", "BEVMO", "DEAD RABBIT", "BROOKLYN BREWERY", "WINE",
        "LIQUOR", "SPIRITS", "BREWERY", "BREWING", "TAVERN", "ALEHOUSE",
    ],
    "Transport": [
        "UBER", "LYFT", "MTA", "NYCT", "NJ TRANSIT", "NJT", "PATH", "AMTRAK",
        "METRO", "BART", "CALTRAIN", "SEPTA", "E-ZPASS", "EZPASS", "SPOTHERO",
        "PARKMOBILE", "PARKWHIZ", "PORT AUTHORITY", "REVEL", "CITI BIKE",
        "CITIBIKE", "LIME", "BIRD RIDES", "CURB", "VIA",
        "MBTA", "PVTA", "MASSDOT", "EZDRIVEMA", "ERACTOLL",
        "PEAKPASS", "SUNPASS", "FASTLANE", "PATCO",
    ],
    "Gas/Auto": [
        "SHELL", "SHELL OIL", "EXXON", "EXXONMOBIL", "MOBIL", "CHEVRON", "SUNOCO",
        "WAWA", "SPEEDWAY", "CITGO", "VALERO", "TEXACO", "BP", "GULF", "QUIKTRIP",
        "SHEETZ", "RACETRAC", "AUTOZONE", "JIFFY LUBE", "MIDAS", "PEP BOYS",
        "FIRESTONE", "MAVIS", "PETROLEUM", "DMV", "MOTOR VEHICLE",
        "CUMBERLAND FARMS", "CIRCLE K", "IRVING OIL", "PRIDE STATION",
        "ELECTRIFY AMERICA", "ELECTRIFY", "CHARGEPOINT", "EVGO", "SUPERCHARGER",
        "RMV", "REGISTRY OF MOTOR VEHICLES",
    ],
    "Travel": [
        "DELTA AIR", "DELTA AIRLINES", "UNITED AIRLINES", "AMERICAN AIRLINES",
        "JETBLUE", "SOUTHWEST AIR", "SPIRIT AIRLINES", "ALASKA AIRLINES",
        "FRONTIER AIR", "EXPEDIA", "PRICELINE", "KAYAK", "HOPPER", "TSA",
        "GLOBAL ENTRY", "CLEAR", "HERTZ", "AVIS", "ENTERPRISE RENT", "BUDGET RENT",
        # Hourly rental rather than a trip, but it is still renting a car, and
        # both issuers here label it Travel/Entertainment themselves.
        "ZIPCAR",
        "TURO", "SIXT", "EUROSTAR", "TRAINLINE", "CHASE TRAVEL", "TRIPCHRG",
        "WANDERU", "OMIO", "FLIXBUS", "GREYHOUND", "PEAK RESORTS",
    ],
    "Lodging": [
        "MARRIOTT", "HILTON", "HYATT", "AIRBNB", "VRBO", "BOOKING.COM", "BOOKING COM",
        "IHG", "HOLIDAY INN", "HAMPTON INN", "WESTIN", "SHERATON", "FOUR SEASONS",
        "RITZ CARLTON", "MOTEL", "HOSTEL", "INN", "RESORT",
    ],
    "Big Box": [
        "TARGET", "WALMART", "WAL-MART", "COSTCO", "SAMS CLUB", "BJS WHOLESALE",
        "BJ S WHOLESALE",
    ],
    "Shopping": [
        "AMAZON", "AMZN", "EBAY", "ETSY", "TEMU", "SHEIN", "WISH", "ALIEXPRESS",
        "MACYS", "NORDSTROM", "TJ MAXX", "TJMAXX", "MARSHALLS", "ROSS STORES",
        "SEPHORA", "ULTA", "STAPLES", "OFFICE DEPOT", "CONTAINER STORE",
        "JCPENNEY", "JC PENNEY", "KOHLS", "BURLINGTON", "DOLLAR TREE",
        "DOLLAR GENERAL", "FAMILY DOLLAR", "FIVE BELOW", "HUDSON NEWS",
        "HUDSONNEWS", "CVS PHOTO", "MICHAELS", "BARNES & NOBLE", "BARNES NOBLE",
    ],
    "Electronics": [
        "BEST BUY", "BESTBUY", "APPLE STORE", "MICRO CENTER", "MICROCENTER",
        "NEWEGG", "B&H PHOTO", "B H PHOTO", "ADORAMA", "GAMESTOP",
    ],
    "Clothing": [
        "UNIQLO", "ZARA", "H&M", "GAP", "GAPCO", "OLD NAVY", "BANANA REPUBLIC",
        "NIKE", "ADIDAS", "LULULEMON", "PATAGONIA", "NORTH FACE", "MADEWELL",
        "J CREW", "JCREW", "EVERLANE", "ABERCROMBIE", "AMERICAN EAGLE",
        "FOOT LOCKER", "REI",
    ],
    "Home": [
        "HOME DEPOT", "LOWES", "LOWE S", "IKEA", "WAYFAIR", "HOMEGOODS",
        "BED BATH", "CRATE & BARREL", "CRATE BARREL", "WEST ELM", "CB2",
        "PUBLIC STORAGE", "ACE HARDWARE", "TRUE VALUE",
    ],
    "Subscriptions": [
        "NETFLIX", "HULU", "DISNEY PLUS", "DISNEYPLUS", "DISNEY", "HBO", "MAX",
        "PARAMOUNT", "PEACOCK", "APPLE TV", "CRUNCHYROLL", "STARZ", "SHOWTIME",
        "BRITBOX", "MUBI", "SPOTIFY", "APPLE MUSIC", "TIDAL", "PANDORA", "DEEZER",
        "YOUTUBE PREMIUM", "YOUTUBEPREMIUM", "AUDIBLE", "KINDLE", "AMAZON PRIME",
        "PRIME VIDEO", "AMZN DIGITAL", "APPLE BILL", "ITUNES", "ICLOUD",
        "GOOGLE ONE", "GOOGLE STORAGE", "DROPBOX", "PATREON", "SUBSTACK",
        "NYTIMES", "NEW YORK TIMES", "WSJ", "WASHINGTON POST", "ECONOMIST",
        "THE ATHLETIC", "MEDIUM", "DUOLINGO", "MASTERCLASS", "CALM", "HEADSPACE",
        "STRAVA", "BLINKIST",
    ],
    "Software/Tools": [
        "OPENAI", "CHATGPT", "ANTHROPIC", "CLAUDE", "MIDJOURNEY", "PERPLEXITY",
        "CURSOR", "REPLIT", "ELEVENLABS", "RUNWAY", "GITHUB", "GITLAB",
        "JETBRAINS", "ADOBE", "FIGMA", "CANVA", "NOTION", "LINEAR", "SLACK",
        "ZOOM", "MICROSOFT 365", "OFFICE 365", "AWS", "AMAZON WEB SERVICES",
        "GOOGLE CLOUD", "AZURE", "DIGITALOCEAN", "VERCEL", "NETLIFY", "HEROKU",
        "MICROSOFT", "MSBILL", "ATLASSIAN", "ASANA", "MONDAY.COM",
        "LINODE", "CLOUDFLARE", "DOCKER", "1PASSWORD", "LASTPASS", "TWILIO",
        "NAMECHEAP", "NAME-CHEAP", "GODADDY", "PORKBUN", "HOSTINGER",
        "SUPABASE", "PLANETSCALE", "MONGODB", "POSTMAN", "APPLE DEVELOPER",
    ],
    "Gaming": [
        "STEAM", "STEAMGAMES", "XBOX", "PLAYSTATION", "NINTENDO", "EPIC GAMES",
        "EA PLAY", "GAME PASS", "ROBLOX", "TWITCH", "BLIZZARD", "RIOT GAMES",
    ],
    "Entertainment": [
        "TICKETMASTER", "STUBHUB", "LIVE NATION", "EVENTBRITE", "AMC", "REGAL",
        "CINEMARK", "ALAMO DRAFTHOUSE", "DICE", "AXS", "MUSEUM", "AQUARIUM",
        "BROOKLYN STEEL", "MADISON SQUARE GARDEN", "SIX FLAGS", "DAVE & BUSTERS",
    ],
    "Utilities": [
        "CON EDISON", "CONED", "PSEG", "PSE&G", "NATIONAL GRID", "DUKE ENERGY",
        "PG&E", "PGE", "SOCALGAS", "DOMINION ENERGY", "AMERICAN WATER",
        "WATER DEPT", "SUEZ", "VEOLIA",
    ],
    "Phone/Internet": [
        "VERIZON", "AT&T", "ATT", "T-MOBILE", "TMOBILE", "SPRINT", "XFINITY",
        "COMCAST", "SPECTRUM", "OPTIMUM", "FIOS", "MINT MOBILE", "GOOGLE FI",
        "VISIBLE", "STARLINK", "COX COMMUNICATIONS", "RCN",
    ],
    "Health": [
        "ONE MEDICAL", "ONEMEDICAL", "QUEST DIAGNOSTICS", "LABCORP", "CITYMD",
        "ZOCDOC", "TELADOC", "KAISER", "CVS MINUTECLINIC", "URGENT CARE",
        "DENTAL", "DDS", "DENTIST", "ORTHODONT", "OPTOMETR", "DERMATOLOG",
        "PHYSICAL THERAPY", "HOSPITAL", "CLINIC", "MEDICAL",
    ],
    "Pharmacy": [
        "CVS", "CVS PHARMACY", "WALGREENS", "RITE AID", "DUANE READE",
        "GOODRX", "PHARMACY", "APOTHECARY",
    ],
    "Fitness": [
        "EQUINOX", "PLANET FITNESS", "CRUNCH FITNESS", "BLINK FITNESS",
        "SOULCYCLE", "CLASSPASS", "PELOTON", "CROSSFIT", "ORANGETHEORY",
        "LIFE TIME", "24 HOUR FITNESS", "GOLD S GYM", "YMCA", "PURE BARRE",
        "YOGA", "PILATES", "GYM", "ICLUB",
    ],
    "Personal Care": [
        "SUPERCUTS", "GREAT CLIPS", "DRYBAR", "BARBER", "SALON", "SPA",
        "NAIL", "MASSAGE ENVY", "EUROPEAN WAX",
    ],
    "Pets": [
        "PETCO", "PETSMART", "CHEWY", "PET SUPPLIES", "VETERINAR", "BARK BOX",
        "BARKBOX", "ROVER", "WAG",
    ],
    "Education": [
        "COURSERA", "UDEMY", "EDX", "SKILLSHARE", "PLURALSIGHT", "TUITION",
        "BURSAR", "UNIVERSITY", "COLLEGE", "ACADEMY",
    ],
    "Charity": [
        "GOFUNDME", "RED CROSS", "UNICEF", "DONATION", "DONORSCHOOSE",
        "CHARITY", "FOUNDATION", "PLANNED PARENTHOOD", "ACLU", "NPR", "WIKIMEDIA",
    ],
    "Gifts": ["1-800-FLOWERS", "FLOWERS.COM", "EDIBLE ARRANGEMENTS", "HALLMARK"],
    "Insurance": [
        "GEICO", "PROGRESSIVE", "STATE FARM", "ALLSTATE", "LEMONADE", "USAA",
        "NATIONWIDE", "LIBERTY MUTUAL", "INSURANCE", "MARKETPLACE INSURANCE",
    ],
    "Debt Payment": [
        "AFFIRM", "KLARNA", "AFTERPAY", "SEZZLE", "ZIP CO", "QUADPAY", "UPLIFT",
        "SPLITIT", "NAVIENT", "NELNET", "MOHELA", "SALLIE MAE", "GREAT LAKES",
        "AIDVANTAGE", "EARNEST", "SOFI", "UPSTART", "LENDING CLUB", "PROSPER",
        "AVANT", "BEST EGG", "HAPPY MONEY", "AUTO LOAN", "STUDENT LOAN",
        "CAR PAYMENT", "LOAN PAYMENT", "INSTALLMENT",
    ],
    "Transfers/Cash": [
        "VENMO", "CASH APP", "CASHAPP", "ZELLE", "PAYPAL", "WISE", "REMITLY",
        "WESTERN UNION", "MONEYGRAM", "ATM WITHDRAWAL", "CASH ADVANCE",
        "COINBASE", "ROBINHOOD", "FIDELITY", "VANGUARD", "SCHWAB", "BETTERMENT",
        "WEALTHFRONT", "ACORNS", "E TRADE", "ETRADE",
        # How Chase and Amex name their own money movements: not purchases
        "FID BKG SVC", "MONEYLINE", "AMEX SEND", "BOOK TRANSFER",
        "NATIONAL FINANCIAL SERVICES",
    ],
    "Rent/Housing": [
        "RENT", "LANDLORD", "PROPERTY MGMT", "PROPERTY MANAGEMENT", "HOA",
        "MORTGAGE", "APARTMENTS", "REALTY", "LEASING",
        "RPS", "RESIDENT PORTAL", "APAR",
    ],
}

# Longest phrases first so "WHOLE FOODS" wins over "FOODS"
_BRAND_INDEX = {}
for _cat, _names in BRANDS.items():
    for _n in _names:
        _BRAND_INDEX.setdefault(_n.upper(), _cat)
_BRAND_PHRASES = sorted(_BRAND_INDEX, key=lambda p: -len(p.split()))
_MAX_NGRAM = max(len(p.split()) for p in _BRAND_PHRASES)

# Some issuers weld the city and state straight onto the merchant name, with no
# space: "CinemarkDaytonOH", "DOLLAR TREEDAYTONOH", "MCDONALDSSAN FRANCISCOCA".
# Nothing token-based can see those, so a brand is also allowed to match as a
# *prefix* of a longer run of letters. Seven characters minimum: shorter names
# collide with ordinary words, and an exact match would have fired already.
_BRAND_GLUED = sorted(
    ((p.replace(" ", "").replace("-", ""), c) for p, c in _BRAND_INDEX.items()
     if len(p.replace(" ", "").replace("-", "")) >= 7),
    key=lambda pc: -len(pc[0]))


# --------------------------------------------------------------------------
# Context rules: two tokens together mean something the brand alone doesn't
# --------------------------------------------------------------------------

CONTEXT = [
    # (must contain all of these, must not contain any of these, category)
    (("COSTCO", "GAS"), (), "Gas/Auto"),
    (("SAMS", "GAS"), (), "Gas/Auto"),
    (("BJS", "GAS"), (), "Gas/Auto"),
    (("UBER", "EATS"), (), "Delivery"),
    (("AMZN", "DIGITAL"), (), "Subscriptions"),
    (("AMAZON", "PRIME"), (), "Subscriptions"),
    (("AMAZON", "WEB"), (), "Software/Tools"),
    (("AWS",), (), "Software/Tools"),
    (("GOOGLE", "CLOUD"), (), "Software/Tools"),
    (("GOOGLE", "FI"), (), "Phone/Internet"),
    (("APPLE", "MUSIC"), (), "Subscriptions"),
    (("APPLE", "STORE"), (), "Electronics"),
    (("TARGET", "PHARMACY"), (), "Pharmacy"),
    (("WALMART", "PHARMACY"), (), "Pharmacy"),
    (("CVS", "PHARMACY"), (), "Pharmacy"),
    (("STUDENT", "LOAN"), (), "Debt Payment"),
    (("AUTO", "LOAN"), (), "Debt Payment"),
    (("MARKETPLACE", "INSURANCE"), (), "Insurance"),
    (("STOCK", "MARKET"), (), "Subscriptions"),
    (("WHOLE", "FOODS"), (), "Groceries"),
    (("SHELL", "OIL"), (), "Gas/Auto"),
    (("GITHUB", "COPILOT"), (), "Software/Tools"),
]

# Hints from a payment processor prefix. Only processors that sell one kind of
# thing qualify: Toast is a restaurant point-of-sale and nothing else, whereas
# Square is used by barbers and bakeries alike and so says nothing.
PROCESSOR_CATEGORY = {"instacart": "Groceries", "eventbrite": "Entertainment",
                      "toast": "Dining", "ticketmaster": "Entertainment"}

# Bank statement lines name their own transaction type, and that name is the
# most reliable thing on the row — more reliable than the merchant, which for
# these rows is a person or an account number. Read off the raw descriptor
# before any cleaning, because cleaning is exactly what destroys them.
#
# The line drawn here: money moving between accounts you already own is
# Transfers/Cash; money arriving from someone else is Income. Neither is
# spending, which is the point — these rows were being counted as purchases.
BANK_PHRASES = [
    ("CARD PURCHASE RETURN", "Credits/Refunds", "purchase returned"),
    ("ZELLE PAYMENT FROM", "Income", "Zelle received"),
    ("ZELLE PAYMENT TO", "Transfers/Cash", "Zelle sent"),
    ("REAL TIME PAYMENT CREDIT", "Income", "real-time payment received"),
    ("REMOTE ONLINE DEPOSIT", "Income", "cheque deposited"),
    ("EDI PAYMNT", "Income", "EDI payment received"),
    ("BOOK TRANSFER CREDIT", "Transfers/Cash", "internal book transfer in"),
    ("BOOK TRANSFER DEBIT", "Transfers/Cash", "internal book transfer out"),
    ("ONLINE TRANSFER FROM", "Transfers/Cash", "moved between your accounts"),
    ("ONLINE TRANSFER TO", "Transfers/Cash", "moved between your accounts"),
    ("ATM CASH DEPOSIT", "Transfers/Cash", "cash paid in at an ATM"),
    ("ATM WITHDRAWAL", "Transfers/Cash", "cash taken out"),
    ("AMEX SEND", "Transfers/Cash", "Amex Send transfer"),
    ("FID BKG SVC", "Transfers/Cash", "Fidelity brokerage transfer"),
    ("PAYMENT TO CHASE", "Payment", "card payment"),
    ("AUTOPAY TO CHASE", "Payment", "card payment"),
    ("CHASE CREDIT CRD", "Payment", "card payment"),
    ("AMERICAN EXPRESS ACH PMT", "Payment", "card payment"),
    ("DISCOVER E-PAYMENT", "Payment", "card payment"),
    ("AUTOMATIC PAYMENT", "Payment", "card payment"),
    # Issuers name themselves when you pay the card from a bank account. These
    # have to be caught here, before keywords: "Capital One Mobile Pmt" was
    # reading as Phone/Internet on the word "Mobile", turning a $1,200 card
    # payment into a phone bill and double-counting it against the card
    # statement that already lists the purchases.
    ("CAPITAL ONE MOBILE PMT", "Payment", "card payment"),
    ("CAPITAL ONE ONLINE PMT", "Payment", "card payment"),
    ("CAPITAL ONE CRCARDPMT", "Payment", "card payment"),
    ("CHASE CARD PMT", "Payment", "card payment"),
    ("BK OF AMER CRD", "Payment", "card payment"),
    ("CITI CARD ONLINE", "Payment", "card payment"),
    ("SYNCHRONY BANK CC PMT", "Payment", "card payment"),
    ("WF CREDIT CARD AUTO PAY", "Payment", "card payment"),
]

# Unambiguous wherever they appear, checked before any brand name
PRIORITY_PHRASES = [
    ("CASH ADVANCE FEE", "Fees/Interest", "cash advance fee"),
    ("BALANCE TRANSFER FEE", "Fees/Interest", "balance transfer fee"),
    ("ACH CREDIT", "Income", "ACH credit"),
    ("FOREIGN TRANSACTION FEE", "Fees/Interest", "foreign transaction fee"),
    ("ANNUAL MEMBERSHIP FEE", "Fees/Interest", "annual fee"),
    ("LATE FEE", "Fees/Interest", "late fee"),
    ("INTEREST CHARGE", "Fees/Interest", "interest charge"),
    ("RETURNED PAYMENT FEE", "Fees/Interest", "returned payment"),
    ("OVERDRAFT FEE", "Fees/Interest", "overdraft"),
    ("ZELLE FROM", "Income", "money received"),
    ("VENMO FROM", "Income", "money received"),
    ("CASH APP FROM", "Income", "money received"),
    ("PAYPAL TRANSFER FROM", "Income", "money received"),
    ("DIRECT DEPOSIT", "Income", "direct deposit"),
    ("DIRECT DEP", "Income", "direct deposit"),
    ("PAYROLL", "Income", "payroll"),
    ("TAX REF", "Income", "tax refund"),
    ("INTEREST PAID", "Income", "interest earned"),
    ("MOBILE DEPOSIT", "Income", "deposit"),
    ("PAYMENT THANK YOU", "Payment", "card payment"),
    ("THANK YOU FOR YOUR PAYMENT", "Payment", "card payment"),
    ("AUTOPAY PAYMENT", "Payment", "card payment"),
    ("ONLINE PAYMENT", "Payment", "card payment"),
    ("STUDENT LOAN", "Debt Payment", "student loan"),
    ("AUTO LOAN", "Debt Payment", "auto loan"),
    ("LOAN PAYMENT", "Debt Payment", "loan payment"),
    ("TAXRFD", "Income", "tax refund"),
]

# Platforms that resell someone else's product: you paid the platform
DELIVERY_PLATFORMS = ["DOORDASH", "UBER EATS", "UBEREATS", "GRUBHUB", "SEAMLESS",
                      "POSTMATES", "CAVIAR", "GOPUFF"]

# Buy-now-pay-later brands: a payment method, not a purpose
BNPL_BRANDS = {"AFFIRM", "KLARNA", "AFTERPAY", "SEZZLE", "ZIP CO", "QUADPAY",
               "UPLIFT", "SPLITIT"}


# --------------------------------------------------------------------------
# Keyword rules: word-boundary regex, applied only after brands miss
# --------------------------------------------------------------------------

KEYWORDS = [
    ("Income", r"\b(?:direct\s*dep(?:osit)?|payroll|ach\s*credit|salary|wages|"
               r"irs\s*treas|tax\s*ref(?:und)?|state\s*refund|dividend|"
               r"interest\s*(?:paid|earned)|mobile\s*deposit|remote\s*deposit|"
               r"atm\s*deposit|deposit\s*from|zelle\s*from|venmo\s*from|"
               r"reimbursement|rebate)\b"),
    ("Payment", r"\b(?:payment[\s\-]*thank[\s\-]*you|thank\s*you\s*for\s*your\s*payment|"
                r"autopay|auto[\s\-]?pay|online\s*payment|payment\s*received|"
                r"electronic\s*payment|pymt|mobile\s*payment|payment\s*posted)\b"),
    ("Credits/Refunds", r"\b(?:statement\s*credit|rewards?\s*credit|"
                        r"cash\s?back\s*(?:credit|bonus)|credit\s*adjustment|"
                        r"chargeback|refund)\b"),
    ("Fees/Interest", r"\b(?:annual\s*(?:membership\s*)?fee|late\s*fee|"
                      r"interest\s*charge|finance\s*charge|foreign\s*transaction|"
                      r"cash\s*advance\s*fee|returned\s*payment|over\s?limit|"
                      r"service\s*charge|atm\s*fee|maintenance\s*fee|overdraft|"
                      r"membership\s*fee)\b"),
    ("Coffee", r"\b(?:coffee|espresso|cafe|caf\u00e9|roasters?|coffeehouse)\b"),
    ("Delivery", r"\b(?:delivery|food\s*deliv)\b"),
    ("Dining", r"\b(?:restaurant|pizza|pizzeria|sushi|taco|taqueria|kitchen|"
               r"bakery|deli|diner|steakhouse|grill|bistro|trattoria|ramen|"
               r"noodle|bbq|burger|sandwich|eatery|osteria|cantina)\b"),
    ("Alcohol/Bars", r"\b(?:bar|pub|tavern|lounge|cocktail|brewery|brewing|"
                     r"winery|liquors?|spirits|beer|wine)\b"),
    ("Groceries", r"\b(?:grocer|grocery|supermarket|market|foods|produce|farmers)\b"),
    ("Pharmacy", r"\b(?:pharmacy|drug\s*store|drugstore|apothecary)\b"),
    ("Health", r"\b(?:medical|clinic|hospital|doctor|dental|dentist|physician|"
               r"health|therapy|psycholog|psychiatr|optical|vision|labs?)\b"),
    ("Fitness", r"\b(?:gym|fitness|yoga|pilates|crossfit|climbing|athletic)\b"),
    ("Personal Care", r"\b(?:salon|barber|haircut|spa|nails|massage|grooming)\b"),
    ("Lodging", r"\b(?:hotel|motel|inn|resort|lodge|hostel|suites)\b"),
    ("Travel", r"\b(?:airlines?|airways|air\s*lines|flight|rental\s*car|"
               r"car\s*rental|cruise|railway|amtrak)\b"),
    ("Transport", r"\b(?:transit|parking|toll|taxi|cab|metro|subway|rail|bus)\b"),
    ("Gas/Auto", r"\b(?:fuel|gasoline|petroleum|service\s*station|auto\s*parts|"
                 r"car\s*wash|oil\s*change|tire)\b"),
    ("Utilities", r"\b(?:electric|energy|utility|utilities|water|gas\s*company|"
                  r"sewer|sanitation)\b"),
    ("Phone/Internet", r"\b(?:wireless|mobile|broadband|internet|telecom|fiber)\b"),
    # "rent" is the one housing word that routinely means something else:
    # rent-a-car, car rental, rental car, equipment rental. Those are Travel or
    # Gas/Auto, and a car hire filed as housing quietly inflates the essential
    # spending that the emergency-fund target is sized from.
    ("Rent/Housing", r"\b(?:rent(?!(?:[\s-]*a[\s-]*car|al[\s-]*car))|lease|"
                     r"landlord|property|apartment|realty|mortgage|hoa)\b"
                     r"(?![\s-]*a[\s-]*car)"),
    ("Insurance", r"\b(?:insurance|premium|underwrit|assurance)\b"),
    ("Education", r"\b(?:tuition|university|college|school|academy|bursar|"
                  r"course|bookstore)\b"),
    ("Childcare", r"\b(?:daycare|day\s*care|childcare|preschool|nanny|babysit)\b"),
    ("Pets", r"\b(?:pet|pets|veterinar|vet\s*clinic|kennel|grooming)\b"),
    ("Charity", r"\b(?:donation|donate|charity|charitable|foundation|nonprofit)\b"),
    ("Entertainment", r"\b(?:cinema|theatre|theater|movie|concert|tickets?|"
                      r"museum|zoo|bowling|arcade|golf)\b"),
    ("Subscriptions", r"\b(?:subscription|subscr|membership|monthly\s*plan|premium)\b"),
    ("Software/Tools", r"\b(?:software|hosting|api|saas|developer|cloud)\b"),
    ("Electronics", r"\b(?:electronics|computers?|tech)\b"),
    ("Clothing", r"\b(?:apparel|clothing|outfitters|boutique|shoes|footwear)\b"),
    ("Home", r"\b(?:hardware|furniture|home\s*goods|storage|garden)\b"),
    ("Taxes", r"\b(?:irs|dept\s*of\s*revenue|tax\s*payment|franchise\s*tax)\b"),
    ("Transfers/Cash", r"\b(?:transfer|wire|atm|withdrawal)\b"),
    ("Debt Payment", r"\b(?:loan|instal?ment|financing)\b"),
]
KEYWORDS = [(c, re.compile(p, re.I)) for c, p in KEYWORDS]


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------

class Result:
    __slots__ = ("category", "source", "confidence", "clean", "note")

    def __init__(self, category, source, confidence, clean="", note=""):
        self.category = category
        self.source = source
        self.confidence = confidence
        self.clean = clean
        self.note = note

    def __repr__(self):
        return (f"<{self.category} via {self.source} "
                f"({self.confidence:.2f}) from '{self.clean}'>")


class Categorizer:
    def __init__(self, learned_path=LEARNED):
        self.learned_path = learned_path
        self.learned = {}
        if learned_path and os.path.exists(learned_path):
            try:
                data = json.load(open(learned_path))
                self.learned = data.get("merchants", {})
            except (json.JSONDecodeError, OSError):
                pass

    # -- persistence ------------------------------------------------------
    def teach(self, descriptor, category):
        clean, _ = normalize(descriptor)
        if not clean:
            raise ValueError("nothing left after cleaning — this descriptor has no "
                             "merchant in it, so a rule would match everything")
        self.learned[clean] = category
        return clean

    def forget(self, descriptor):
        clean, _ = normalize(descriptor)
        self.learned.pop(clean, None)
        return clean

    def save(self):
        if not self.learned_path:
            return
        json.dump({"merchants": self.learned}, open(self.learned_path, "w"), indent=1)

    # -- the decision -----------------------------------------------------
    def categorize(self, descriptor):
        clean, hints = normalize(descriptor)
        raw = str(descriptor or "").upper()

        if clean and clean in self.learned:
            return Result(self.learned[clean], "learned", 1.0, clean, "you told me")

        # The bank already told us what kind of row this is, and that beats the
        # merchant name: "Zelle Payment From Jordan Rivera" names a person,
        # not a shop, and every rule below would try to read it as one. Checked
        # against the raw line, because cleaning is what destroys these.
        for phrase, cat, why in BANK_PHRASES:
            if phrase in raw:
                return Result(cat, "bank", 0.95, clean or phrase.title(), why)

        if not clean:
            return Result("Uncategorized", "empty", 0.0, clean,
                          "nothing left after cleaning")

        toks = clean.split()
        tokset = set(toks)

        # Some phrases are unambiguous wherever they appear and must beat a
        # brand name sitting next to them: "CASH ADVANCE FEE" is a fee, not a
        # cash advance; "ZELLE FROM" is money arriving, not a transfer out.
        for phrase, cat, why in PRIORITY_PHRASES:
            if phrase in clean:
                return Result(cat, "phrase", 0.96, clean, why)

        # Delivery platforms wrap another merchant's name. You paid the
        # platform, so "SEAMLESS*JOES PIZZA" is delivery, not a pizzeria.
        for plat in DELIVERY_PLATFORMS:
            if plat in tokset or plat in clean:
                return Result("Delivery", "platform", 0.93, clean, plat)

        for need, avoid, cat in CONTEXT:
            if all(t in tokset for t in need) and not any(a in tokset for a in avoid):
                return Result(cat, "context", 0.95, clean,
                              " + ".join(need))

        bnpl_hit = None
        for n in range(min(_MAX_NGRAM, len(toks)), 0, -1):
            for i in range(len(toks) - n + 1):
                phrase = " ".join(toks[i:i + n])
                cat = _BRAND_INDEX.get(phrase)
                if not cat:
                    continue
                # Buy-now-pay-later is a payment method, not a purpose. If the
                # line also says what was bought, that is the better answer.
                if phrase in BNPL_BRANDS:
                    bnpl_hit = bnpl_hit or (cat, phrase)
                    continue
                return Result(cat, "brand", 0.92 if n > 1 else 0.88, clean, phrase)

        for name in hints:
            if name in PROCESSOR_CATEGORY:
                return Result(PROCESSOR_CATEGORY[name], "processor", 0.8, clean, name)

        for cat, rx in KEYWORDS:
            m = rx.search(clean)
            if m:
                if bnpl_hit and cat in ("Debt Payment", "Transfers/Cash"):
                    continue
                note = m.group(0).lower()
                if bnpl_hit:
                    note += f" (financed via {bnpl_hit[1].title()})"
                return Result(cat, "keyword", 0.7, clean, note)

        # City welded onto the name with no space. Only exact prefixes of a long
        # brand qualify, so this cannot invent a match out of a common word.
        flat = clean.replace(" ", "").replace("-", "")
        for brand, cat in _BRAND_GLUED:
            if flat.startswith(brand) and len(flat) > len(brand):
                return Result(cat, "glued", 0.75, clean,
                              f"{brand} with the location run into it")
            for t in toks:
                if len(t) > len(brand) and t.startswith(brand):
                    return Result(cat, "glued", 0.75, clean,
                                  f"{brand} with the location run into it")

        # Last: whatever the issuer itself called this merchant. Weaker than a
        # name we recognise, stronger than giving up.
        for h in hints:
            if h.startswith("mcc:") and _MCC_INDEX.get(h[4:]):
                return Result(_MCC_INDEX[h[4:]], "issuer", 0.6, clean,
                              f"the statement labels it {h[4:]}")

        if bnpl_hit:
            return Result(bnpl_hit[0], "brand", 0.88, clean, bnpl_hit[1])

        return Result("Uncategorized", "none", 0.0, clean,
                      "no brand or keyword matched")

    # -- behaviour, for the ones the name can't answer --------------------
    def infer_from_behaviour(self, groups):
        """groups: {clean_name: [(date, amount), ...]} -> {clean_name: Result}

        Shape alone is informative. A charge that is identical every month is a
        bill; a small identical charge is a subscription; a large one on the
        first of the month is rent.
        """
        out = {}
        for name, items in groups.items():
            if len(items) < 3:
                continue
            items = sorted(items)
            ds = [d for d, _ in items]
            amts = [a for _, a in items]
            med = stats.median(amts)
            if med <= 0:
                continue
            spread = stats.median([abs(a - med) for a in amts]) / med

            months = {d.strftime("%Y-%m") for d in ds}
            span = ((ds[-1].year - ds[0].year) * 12 + ds[-1].month - ds[0].month) + 1
            monthly = len(months) >= 3 and len(months) >= span * 0.7

            if not (monthly and spread <= 0.15):
                continue
            early = sum(1 for d in ds if d.day <= 5) / len(ds)

            if med >= 600 and early >= 0.6:
                out[name] = Result("Rent/Housing", "behaviour", 0.55, name,
                                   "large, same amount, start of month")
            elif 3 <= med <= 60:
                out[name] = Result("Subscriptions", "behaviour", 0.5, name,
                                   f"{len(items)} identical monthly charges")
            elif 60 < med < 600:
                out[name] = Result("Utilities", "behaviour", 0.35, name,
                                   "monthly bill, stable amount")
        return out


_DEFAULT = None


def categorize(descriptor, learned_path=LEARNED):
    """Convenience wrapper so other scripts can call one function."""
    global _DEFAULT
    if _DEFAULT is None or _DEFAULT.learned_path != learned_path:
        _DEFAULT = Categorizer(learned_path)
    return _DEFAULT.categorize(descriptor)


def category_of(descriptor, learned_path=LEARNED):
    return categorize(descriptor, learned_path).category


# --------------------------------------------------------------------------
# Reading and writing the store
#
# Input is a .db or a .csv, read through engine.load_transactions so this tool
# and every sibling see the same rows. Output goes back into the store, because
# improving categories used to mean a CSV round-trip that nothing else read.
# --------------------------------------------------------------------------

def load_source(source):
    """Transactions from a .db or a .csv. Imported lazily: engine imports the
    taxonomy from here, and a module-level import would close the circle."""
    from finances.engine import load_transactions
    txns = load_transactions(source)
    if not txns:
        sys.exit(f"{source} has no transactions")
    return txns


def write_store(source, txns):
    from finances.database import FinanceDB
    with FinanceDB(source) as db:
        db.upsert_transactions(txns)


def write_csv(path, txns, results, extras=True):
    from finances.models import Transaction
    cols = Transaction.columns()
    if extras:
        cols = cols + ["category_source", "category_confidence", "merchant_clean"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t, res in zip(txns, results):
            row = t.to_row()
            if extras:
                row.update({"category_source": res.source,
                            "category_confidence": f"{res.confidence:.2f}",
                            "merchant_clean": res.clean})
            w.writerow(row)


def descriptor_of(t):
    return (t.description or t.merchant or "").strip()


def categorize_rows(txns, cz, use_behaviour=True):
    """Categorise every transaction, then take a second pass at the leftovers."""
    results = [cz.categorize(descriptor_of(t)) for t in txns]

    if use_behaviour:
        groups = defaultdict(list)
        for t, res in zip(txns, results):
            if res.category == "Uncategorized" and t.d:
                groups[res.clean].append((t.d, abs(t.amount)))
        inferred = cz.infer_from_behaviour(groups)
        for res in results:
            if res.category == "Uncategorized" and res.clean in inferred:
                hit = inferred[res.clean]
                res.category, res.source = hit.category, hit.source
                res.confidence, res.note = hit.confidence, hit.note
    return results


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_apply(args):
    cz = Categorizer(args.learned)
    source = args.db or args.source
    txns = load_source(source)
    results = categorize_rows(txns, cz, not args.no_behaviour)

    changed = Counter()
    for t, res in zip(txns, results):
        old = t.category or "Uncategorized"
        if args.keep_existing and old != "Uncategorized":
            continue
        if res.category == "Uncategorized" and old != "Uncategorized":
            continue                       # never downgrade a known category
        if old != res.category:
            changed[f"{old} -> {res.category}"] += 1
            t.category = res.category
        if args.clean_merchants and res.clean:
            t.merchant = res.clean

    n = sum(changed.values())
    print(f"{len(txns):,} transactions, {n:,} categories changed")
    for move, k in changed.most_common(10):
        print(f"  {k:>5}  {move}")
    _coverage(txns, results)

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return
    to_store = source.lower().endswith(".db")
    if to_store:
        write_store(source, txns)
        print(f"\nWrote {n:,} updated categories back into {source}")
    if args.output or not to_store:
        out = args.output or "categorized.csv"
        write_csv(out, txns, results)
        print(f"Wrote {out}")


def _coverage(txns, results):
    """Coverage that matters is dollars, not row counts."""
    total = unknown = 0.0
    by_source = defaultdict(float)
    n_by_source = Counter()
    non_spend = 0.0
    for t, res in zip(txns, results):
        if t.amount <= 0:
            continue
        if res.category in NON_SPEND:
            non_spend += t.amount
            continue
        total += t.amount
        by_source[res.source] += t.amount
        n_by_source[res.source] += 1
        if res.category == "Uncategorized":
            unknown += t.amount
    if not total:
        print("no spending rows to measure")
        return
    print(f"\n  {'how it was decided':<22}{'rows':>7}{'dollars':>14}{'share':>8}")
    for s, v in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {s:<22}{n_by_source[s]:>7}{v:>14,.0f}{v/total*100:>7.1f}%")
    cov = (total - unknown) / total * 100
    print(f"  categorised: {cov:.1f}% of spending  (${unknown:,.0f} still unknown)")
    if non_spend:
        gross = total + non_spend
        print(f"  held out as non-spend: ${non_spend:,.0f} of ${gross:,.0f} gross "
              f"outflow ({non_spend / gross * 100:.1f}%) — transfers, card "
              f"payments, refunds")


def cmd_stats(args):
    cz = Categorizer(args.learned)
    source = args.db or args.source
    txns = load_source(source)
    results = categorize_rows(txns, cz, True)
    _coverage(txns, results)

    unknown = defaultdict(lambda: [0.0, 0])
    for t, res in zip(txns, results):
        if res.category == "Uncategorized" and t.amount > 0:
            unknown[res.clean][0] += t.amount
            unknown[res.clean][1] += 1
    if unknown:
        print(f"\n  Biggest unknowns — teaching it these 10 covers the most money:")
        for name, (amt, n) in sorted(unknown.items(), key=lambda kv: -kv[1][0])[:10]:
            print(f"    ${amt:>10,.0f}  {n:>3}x  {name[:44]}")
        print(f"\n  Fix them interactively:  finances categorize review {source}")


def cmd_review(args):
    """Ask about unknowns in order of how much money they represent."""
    cz = Categorizer(args.learned)
    source = args.db or args.source
    txns = load_source(source)
    results = categorize_rows(txns, cz, True)

    unknown = defaultdict(lambda: [0.0, 0, ""])
    unreadable = [0.0, 0]
    for t, res in zip(txns, results):
        if t.amount <= 0:
            continue
        low = res.confidence < args.min_confidence
        if not res.clean:
            unreadable[0] += t.amount
            unreadable[1] += 1
            continue
        if res.category == "Uncategorized" or (args.include_low and low):
            unknown[res.clean][0] += t.amount
            unknown[res.clean][1] += 1
            unknown[res.clean][2] = descriptor_of(t)

    if unreadable[1]:
        print(f"Note: {unreadable[1]} charge(s) worth ${unreadable[0]:,.0f} have no "
              f"readable merchant at all —")
        print("      just reference numbers. Those need a parser fix, not a category.\n")

    ranked = sorted(unknown.items(), key=lambda kv: -kv[1][0])[:args.limit]
    if not ranked:
        print("Nothing to review. Everything is categorised.")
        return

    total = sum(v[0] for v in unknown.values())
    print(f"{len(unknown)} unknown merchant(s), ${total:,.0f} of spending.")
    print(f"Showing the {len(ranked)} biggest. Enter a number, a category name, "
          f"s to skip, q to stop.\n")
    menu = [c for c in CATEGORIES if c not in ("Uncategorized",)]
    for i, c in enumerate(menu, 1):
        end = "\n" if i % 4 == 0 else ""
        print(f"  {i:>2}. {c:<17}", end=end)
    print("\n")

    taught = 0
    for name, (amt, n, sample) in ranked:
        print(f"  ${amt:,.0f} across {n} charge(s)")
        print(f"  cleaned:  {name}")
        print(f"  as shown: {sample[:70]}")
        try:
            ans = input("  what is it? > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nStopping. What you've answered so far is saved.")
            break
        if not ans or ans.lower() == "s":
            print()
            continue
        if ans.lower() == "q":
            break
        cat = None
        if ans.isdigit() and 1 <= int(ans) <= len(menu):
            cat = menu[int(ans) - 1]
        else:
            hits = [c for c in CATEGORIES if c.lower() == ans.lower()] or \
                   [c for c in CATEGORIES if c.lower().startswith(ans.lower())]
            if len(hits) == 1:
                cat = hits[0]
            elif len(hits) > 1:
                print(f"  ambiguous: {', '.join(hits)}\n")
                continue
        if not cat:
            print(f"  don't know '{ans}'. Use a number or a category name.\n")
            continue
        try:
            cz.teach(name, cat)
        except ValueError as e:
            print(f"  skipped: {e}\n")
            continue
        taught += 1
        print(f"  -> {cat}\n")

    cz.save()
    print(f"\nLearned {taught} merchant(s) -> {args.learned}")
    if taught:
        print(f"Re-run to apply:  finances categorize apply {source}")


def cmd_eval(args):
    try:
        from finances.benchmark_descriptors import CASES
    except ImportError:
        sys.exit("benchmark_descriptors.py not found next to this script")
    cz = Categorizer(args.learned if args.use_learned else None)

    ok = wrong = unknown = 0
    errors = []
    by_source = Counter()
    for desc, truth in CASES:
        res = cz.categorize(desc)
        by_source[res.source] += 1
        if res.category == truth:
            ok += 1
        elif res.category == "Uncategorized":
            unknown += 1
            errors.append(("unknown", desc, truth, res))
        else:
            wrong += 1
            errors.append(("WRONG", desc, truth, res))

    n = len(CASES)
    print(f"{n} labelled descriptors")
    print(f"  correct        {ok:>4}  {ok/n*100:5.1f}%")
    print(f"  wrong          {wrong:>4}  {wrong/n*100:5.1f}%   <- these corrupt analysis")
    print(f"  left unknown   {unknown:>4}  {unknown/n*100:5.1f}%   <- these are honest")
    print(f"\n  decided by: " + ", ".join(f"{s} {c}" for s, c in by_source.most_common()))

    if errors and not args.quiet:
        print()
        for kind, desc, truth, res in errors:
            if kind == "WRONG" or args.show_unknown:
                print(f"  {kind:<8} {desc[:46]:<48} want {truth:<15} "
                      f"got {res.category} ({res.source}: {res.note})")
    return wrong


def cmd_explain(args):
    cz = Categorizer(args.learned)
    clean, hints = normalize(args.descriptor)
    res = cz.categorize(args.descriptor)
    print(f"  as shown:   {args.descriptor}")
    print(f"  cleaned to: {clean}")
    proc = sorted(h for h in hints if not h.startswith("mcc:"))
    mcc = sorted(h[4:] for h in hints if h.startswith("mcc:"))
    if proc:
        print(f"  processor:  {', '.join(proc)}")
    if mcc:
        print(f"  issuer says: {', '.join(mcc)}")
    print(f"  category:   {res.category}")
    print(f"  decided by: {res.source} ({res.note})")
    print(f"  confidence: {res.confidence:.2f}")


def cmd_teach(args):
    cz = Categorizer(args.learned)
    if args.category not in CATEGORIES:
        hits = [c for c in CATEGORIES if c.lower().startswith(args.category.lower())]
        if len(hits) != 1:
            sys.exit(f"Unknown category '{args.category}'. "
                     f"Options: {', '.join(CATEGORIES)}")
        args.category = hits[0]
    clean = cz.teach(args.descriptor, args.category)
    cz.save()
    print(f"Learned: '{clean}' -> {args.category}")


def main():
    ap = argparse.ArgumentParser(description="Decide what transactions are.")
    ap.add_argument("--learned", default=LEARNED,
                    help="where your corrections are stored")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_source(p):
        p.add_argument("source", nargs="?", default=DEFAULT_SOURCE,
                       help=f"a .db or a .csv (default {DEFAULT_SOURCE})")
        p.add_argument("--db", help="the store to read, same as the positional "
                                    "argument; here for consistency with the "
                                    "other tools")

    a = sub.add_parser("apply", help="categorise the store, or a CSV")
    add_source(a)
    a.add_argument("-o", "--output",
                   help="also write a CSV (the default when the source is a CSV)")
    a.add_argument("--keep-existing", action="store_true",
                   help="don't overwrite categories that are already set")
    a.add_argument("--clean-merchants", action="store_true",
                   help="also replace merchant with the cleaned name")
    a.add_argument("--no-behaviour", action="store_true",
                   help="skip cadence-based inference")
    a.add_argument("--dry-run", action="store_true",
                   help="show what would change and write nothing")
    a.set_defaults(func=cmd_apply)

    s = sub.add_parser("stats", help="coverage by dollars, and the biggest unknowns")
    add_source(s)
    s.set_defaults(func=cmd_stats)

    r = sub.add_parser("review", help="teach it, biggest money first")
    add_source(r)
    r.add_argument("--limit", type=int, default=25)
    r.add_argument("--include-low", action="store_true",
                   help="also review low-confidence guesses")
    r.add_argument("--min-confidence", type=float, default=0.7)
    r.set_defaults(func=cmd_review)

    e = sub.add_parser("eval", help="score against the labelled benchmark")
    e.add_argument("--use-learned", action="store_true")
    e.add_argument("--quiet", action="store_true")
    e.add_argument("--show-unknown", action="store_true")
    e.set_defaults(func=cmd_eval)

    x = sub.add_parser("explain", help="show how one descriptor is read")
    x.add_argument("descriptor")
    x.set_defaults(func=cmd_explain)

    t = sub.add_parser("teach", help="record what a merchant is")
    t.add_argument("descriptor")
    t.add_argument("category")
    t.set_defaults(func=cmd_teach)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()