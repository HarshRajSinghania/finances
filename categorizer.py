#!/usr/bin/env python3
"""
categorizer.py — one place that decides what a transaction is.

Every other script imports this. Before it existed the taxonomy was defined in
four files with four different category lists, so an LLM label of "Coffee" or
"Lodging" arrived downstream as an unknown.

    from categorizer import categorize, Categorizer, TAXONOMY

Command line:
    python categorizer.py apply transactions.csv -o categorized.csv
    python categorizer.py stats transactions.csv        # coverage by DOLLARS
    python categorizer.py review transactions.csv       # teach it, biggest money first
    python categorizer.py eval                          # score against the benchmark
    python categorizer.py explain "SQ *BLUE BOTTLE COFFEE OAKLAND CA"

How it decides, in order. First hit wins, and every answer records which rule
fired and how sure it is:

  1. what you taught it          (learned_rules.json — always wins)
  2. context rules               COSTCO+GAS is fuel, UBER+EATS is delivery
  3. brand table                 exact token/phrase match, never substring
  4. keyword rules               word-boundary regex on the cleaned name
  5. behaviour                   cadence and amount, when the name says nothing
  6. Uncategorized               with the reason it couldn't tell

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

# Preambles the issuer bolts on the front
PREAMBLE = re.compile(
    r"^\s*(?:"
    r"purchase\s+authorized\s+on\s+\d{1,2}/\d{1,2}|"
    r"recurring\s+payment\s+authorized\s+on\s+\d{1,2}/\d{1,2}|"
    r"pos\s+(?:debit|purchase|withdrawal)|debit\s+card\s+purchase|"
    r"checkcard\s+\d{2,4}|check\s?card|visa\s+(?:debit|purchase)|"
    r"ach\s+(?:debit|payment)|electronic\s+(?:debit|payment)|"
    r"card\s+purchase(?:\s+\d{2}/\d{2})?|purchase\s+at|payment\s+to|"
    r"recurring\s+(?:debit\s+card|payment)|preauthorized\s+(?:debit|wd)"
    r")\s*",
    re.I)

# "SQ *MERCHANT" — the processor, then the merchant we actually want
PROCESSORS = [
    ("square", r"^sq\s*\*+\s*"), ("toast", r"^tst\s*\*+\s*"),
    ("paypal", r"^(?:pp|paypal)\s*\*+\s*"), ("shopify", r"^sp\s+(?=\w)"),
    ("instacart", r"^ic\s*\*+\s*"), ("wepay", r"^wpy\s*\*+\s*"),
    ("eventbrite", r"^eb\s*\*+\s*"), ("stripe", r"^(?:stripe|str)\s*\*+\s*"),
    ("clover", r"^clv\s*\*+\s*"), ("2checkout", r"^2co\s*\*+\s*"),
]
PROCESSORS = [(n, re.compile(p, re.I)) for n, p in PROCESSORS]

TRAILING_JUNK = [
    r"\bS\d{12,}\b",                       # Wells Fargo auth strings
    r"\bcard\s*\d{4}\b",
    r"\bref\s*#?\s*\d+\b",
    r"\bppd\s*id\s*:?\s*\d*",
    r"\bid\s*:\s*\S+",
    r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b",      # phone
    r"\b\d{10,}\b",                        # long reference numbers
    r"\bhttps?://\S+", r"\bwww\.",
    r"#\s*\d+\b", r"\bstore\s*\d+\b", r"\b\d{2}/\d{2}\b",
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
    """Cleaned merchant name plus whatever hints the noise gave up."""
    s = str(descriptor or "").strip()
    hints = set()

    s = PREAMBLE.sub("", s)
    # Strip the TLD but keep the name: AMAZON.COM -> AMAZON, APPLE.COM/BILL -> APPLE BILL
    s = re.sub(r"\.(?:com|net|org|io|co|gov|edu|us|biz)\b", " ", s, flags=re.I)
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

    # Drop a trailing state, then city words that led up to it
    while toks and toks[-1] in US_STATES:
        toks.pop()
        while toks and toks[-1] in CITY_WORDS:
            toks.pop()
    while toks and toks[-1] in NOISE_WORDS:
        toks.pop()
    toks = [t for t in toks if not (t.isdigit() and len(t) >= 4)]

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
    ],
    "Gas/Auto": [
        "SHELL", "SHELL OIL", "EXXON", "EXXONMOBIL", "MOBIL", "CHEVRON", "SUNOCO",
        "WAWA", "SPEEDWAY", "CITGO", "VALERO", "TEXACO", "BP", "GULF", "QUIKTRIP",
        "SHEETZ", "RACETRAC", "AUTOZONE", "JIFFY LUBE", "MIDAS", "PEP BOYS",
        "FIRESTONE", "MAVIS", "PETROLEUM", "DMV", "MOTOR VEHICLE",
    ],
    "Travel": [
        "DELTA AIR", "DELTA AIRLINES", "UNITED AIRLINES", "AMERICAN AIRLINES",
        "JETBLUE", "SOUTHWEST AIR", "SPIRIT AIRLINES", "ALASKA AIRLINES",
        "FRONTIER AIR", "EXPEDIA", "PRICELINE", "KAYAK", "HOPPER", "TSA",
        "GLOBAL ENTRY", "CLEAR", "HERTZ", "AVIS", "ENTERPRISE RENT", "BUDGET RENT",
        "TURO", "SIXT", "EUROSTAR", "TRAINLINE",
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
    ],
    "Gaming": [
        "STEAM", "STEAMGAMES", "XBOX", "PLAYSTATION", "NINTENDO", "EPIC GAMES",
        "EA PLAY", "GAME PASS", "ROBLOX", "TWITCH", "BLIZZARD", "RIOT GAMES",
    ],
    "Entertainment": [
        "TICKETMASTER", "STUBHUB", "LIVE NATION", "EVENTBRITE", "AMC", "REGAL",
        "CINEMARK", "ALAMO DRAFTHOUSE", "DICE", "AXS", "MUSEUM", "AQUARIUM",
        "BROOKLYN STEEL", "MADISON SQUARE GARDEN",
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
        "YOGA", "PILATES", "GYM",
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
    ],
    "Rent/Housing": [
        "RENT", "LANDLORD", "PROPERTY MGMT", "PROPERTY MANAGEMENT", "HOA",
        "MORTGAGE", "APARTMENTS", "REALTY", "LEASING",
    ],
}

# Longest phrases first so "WHOLE FOODS" wins over "FOODS"
_BRAND_INDEX = {}
for _cat, _names in BRANDS.items():
    for _n in _names:
        _BRAND_INDEX.setdefault(_n.upper(), _cat)
_BRAND_PHRASES = sorted(_BRAND_INDEX, key=lambda p: -len(p.split()))
_MAX_NGRAM = max(len(p.split()) for p in _BRAND_PHRASES)


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

# Hints from a payment processor prefix
PROCESSOR_CATEGORY = {"instacart": "Groceries", "eventbrite": "Entertainment"}

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
                     r"winery|liquor|spirits|beer|wine)\b"),
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
    ("Rent/Housing", r"\b(?:rent|lease|landlord|property|apartment|realty|"
                     r"mortgage|hoa)\b"),
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
        if not clean:
            return Result("Uncategorized", "empty", 0.0, clean,
                          "nothing left after cleaning")

        if clean in self.learned:
            return Result(self.learned[clean], "learned", 1.0, clean, "you told me")

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
# CSV helpers
# --------------------------------------------------------------------------

def read_rows(path):
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"{path} is empty")
    return rows


def descriptor_of(r):
    return (r.get("description") or r.get("merchant") or "").strip() or \
           (r.get("merchant") or "").strip()


def amount_of(r):
    try:
        return float(r.get("amount", 0) or 0)
    except ValueError:
        return 0.0


def date_of(r):
    try:
        return datetime.strptime(r["date"][:10], "%Y-%m-%d").date()
    except (ValueError, KeyError):
        return None


def categorize_rows(rows, cz, use_behaviour=True):
    """Categorise every row, then take a second pass at the leftovers."""
    results = []
    for r in rows:
        results.append(cz.categorize(descriptor_of(r)))

    if use_behaviour:
        groups = defaultdict(list)
        for r, res in zip(rows, results):
            if res.category == "Uncategorized":
                d = date_of(r)
                if d:
                    groups[res.clean].append((d, abs(amount_of(r))))
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
    rows = read_rows(args.csv)
    results = categorize_rows(rows, cz, not args.no_behaviour)

    kept = 0
    for r, res in zip(rows, results):
        old = r.get("category", "")
        if args.keep_existing and old and old != "Uncategorized":
            r["category_engine"] = res.category
            continue
        if res.category != "Uncategorized" or not old:
            if r.get("category") != res.category:
                kept += 1
            r["category"] = res.category
        r["category_source"] = res.source
        r["category_confidence"] = f"{res.confidence:.2f}"
        r["merchant_clean"] = res.clean

    cols = list(rows[0].keys())
    for extra in ("category_source", "category_confidence", "merchant_clean",
                  "category_engine"):
        if any(extra in r for r in rows) and extra not in cols:
            cols.append(extra)
    with open(args.output, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})

    print(f"{len(rows):,} rows, {kept:,} categories changed")
    _coverage(rows, results)
    print(f"\nWrote {args.output}")


def _coverage(rows, results):
    """Coverage that matters is dollars, not row counts."""
    total = unknown = 0.0
    by_source = defaultdict(float)
    n_by_source = Counter()
    for r, res in zip(rows, results):
        a = abs(amount_of(r))
        if amount_of(r) <= 0 or res.category in NON_SPEND:
            continue
        total += a
        by_source[res.source] += a
        n_by_source[res.source] += 1
        if res.category == "Uncategorized":
            unknown += a
    if not total:
        print("no spending rows to measure")
        return
    print(f"\n  {'how it was decided':<22}{'rows':>7}{'dollars':>14}{'share':>8}")
    for s, v in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {s:<22}{n_by_source[s]:>7}{v:>14,.0f}{v/total*100:>7.1f}%")
    cov = (total - unknown) / total * 100
    print(f"  categorised: {cov:.1f}% of spending  "
          f"({'$%,.0f' % unknown if False else f'${unknown:,.0f}'} still unknown)")


def cmd_stats(args):
    cz = Categorizer(args.learned)
    rows = read_rows(args.csv)
    results = categorize_rows(rows, cz, True)
    _coverage(rows, results)

    unknown = defaultdict(lambda: [0.0, 0])
    for r, res in zip(rows, results):
        if res.category == "Uncategorized" and amount_of(r) > 0:
            unknown[res.clean][0] += amount_of(r)
            unknown[res.clean][1] += 1
    if unknown:
        print(f"\n  Biggest unknowns — teaching it these 10 covers the most money:")
        for name, (amt, n) in sorted(unknown.items(), key=lambda kv: -kv[1][0])[:10]:
            print(f"    ${amt:>10,.0f}  {n:>3}x  {name[:44]}")
        print(f"\n  Fix them interactively:  python categorizer.py review {args.csv}")


def cmd_review(args):
    """Ask about unknowns in order of how much money they represent."""
    cz = Categorizer(args.learned)
    rows = read_rows(args.csv)
    results = categorize_rows(rows, cz, True)

    unknown = defaultdict(lambda: [0.0, 0, ""])
    unreadable = [0.0, 0]
    for r, res in zip(rows, results):
        if amount_of(r) <= 0:
            continue
        low = res.confidence < args.min_confidence
        if not res.clean:
            unreadable[0] += amount_of(r)
            unreadable[1] += 1
            continue
        if res.category == "Uncategorized" or (args.include_low and low):
            unknown[res.clean][0] += amount_of(r)
            unknown[res.clean][1] += 1
            unknown[res.clean][2] = descriptor_of(r)

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
        print(f"Re-run to apply:  python categorizer.py apply {args.csv} "
              f"-o categorized.csv")


def cmd_eval(args):
    try:
        from benchmark_descriptors import CASES
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
    if hints:
        print(f"  processor:  {', '.join(sorted(hints))}")
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

    a = sub.add_parser("apply", help="categorise a CSV")
    a.add_argument("csv")
    a.add_argument("-o", "--output", default="categorized.csv")
    a.add_argument("--keep-existing", action="store_true",
                   help="don't overwrite categories already in the file")
    a.add_argument("--no-behaviour", action="store_true",
                   help="skip cadence-based inference")
    a.set_defaults(func=cmd_apply)

    s = sub.add_parser("stats", help="coverage by dollars, and the biggest unknowns")
    s.add_argument("csv")
    s.set_defaults(func=cmd_stats)

    r = sub.add_parser("review", help="teach it, biggest money first")
    r.add_argument("csv")
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