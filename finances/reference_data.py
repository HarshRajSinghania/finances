#!/usr/bin/env python3
"""
Cited national reference figures, frozen into the repository.

cpi.py needs two things it cannot derive from a pile of card statements: what
other households at the same income actually spend, and how much prices moved
nationally over the same window. Both are published by the U.S. Bureau of Labor
Statistics. Neither is fetched at runtime.

That is deliberate. The rest of this toolchain is local-first and offline, and a
benchmark that silently changes underneath you is worse than no benchmark: two
runs a month apart would disagree and neither would say why. So the numbers live
here as plain dicts with the survey year, the table number and the release date
attached, and they change only when somebody edits this file.

Two sources, both primary:

1. Consumer Expenditure Surveys, Table 1101, "Quintiles of income before taxes:
   Annual expenditure means, shares, standard errors, and relative standard
   errors, Consumer Expenditure Surveys, 2024". Survey year 2024, released
   19 December 2025 (news release USDL-25-1586).
   https://www.bls.gov/cex/tables/calendar-year/mean-item-share-average-standard-error/cu-income-quintiles-before-taxes-2024.xlsx
   https://www.bls.gov/news.release/cesan.nr0.htm

2. CPI-U, U.S. city average, all urban consumers, not seasonally adjusted.
   Annual average index levels 2016-2025 and monthly index levels 2023-2025,
   pulled from the BLS public data API (LABSTAT), series listed inline below.
   https://api.bls.gov/publicAPI/v2/timeseries/data/

Nothing here is estimated, interpolated or filled in. Where BLS does not publish
a figure it is absent, and the lookup helpers return None rather than a guess:

    >>> quintile_for_income(72000)
    3
    >>> QUINTILES[3]["label"]
    'Third 20 percent'
    >>> ce_annual("Food away from home", 3)
    3277.0
    >>> round(cpi_rate("Food away from home", 2025), 1)
    3.7
    >>> cpi_rate("Alcoholic beverages", 2025) is None
    True

Read CAVEATS before putting any of these comparisons in front of a person. The
CE measures a whole household's spending through every payment method; a card
statement measures one person's card. They are not the same quantity and the
ratio between them is not a verdict.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

SOURCE = {
    "ce": {
        "publisher": "U.S. Bureau of Labor Statistics",
        "survey": "Consumer Expenditure Surveys",
        "table": "Table 1101. Quintiles of income before taxes",
        "survey_year": 2024,
        "released": "2025-12-19",
        "release_id": "USDL-25-1586",
        "url": ("https://www.bls.gov/cex/tables/calendar-year/"
                "mean-item-share-average-standard-error/"
                "cu-income-quintiles-before-taxes-2024.xlsx"),
        "units": "mean dollars per consumer unit per year",
    },
    "cpi": {
        "publisher": "U.S. Bureau of Labor Statistics",
        "survey": "Consumer Price Index for All Urban Consumers (CPI-U)",
        "area": "U.S. city average",
        "adjustment": "not seasonally adjusted",
        "annual_coverage": "2016-2025 annual averages",
        "monthly_coverage": "2023-01 to 2025-12 (2025-10 not published)",
        "retrieved": "2026-07-28",
        "url": "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        "units": "index, 1982-84 = 100 unless the series says otherwise",
    },
}

CE_YEAR = SOURCE["ce"]["survey_year"]
CPI_LATEST_YEAR = 2025

# CPI-U for October 2025 was never published: the BLS collection lapsed during
# the 2025 appropriations gap. The month is missing from CPI_MONTHLY_INDEX and
# any span that needs it returns None instead of interpolating across the hole.
CPI_MISSING_MONTHS = ("2025-10",)


# --------------------------------------------------------------------------
# Income brackets
# --------------------------------------------------------------------------
#
# BLS publishes the CE by quintile of income before taxes, not by round dollar
# bands, so these are the brackets and there is no finer grain to offer. The
# lower bounds are the ones stated in the methodology section of USDL-25-1586.
# Index 0 is "all consumer units" and has no bounds; it is the fallback the
# caller gets only if it asks for it, never by inference from a missing income.

QUINTILES = (
    {"index": 0, "label": "All consumer units", "lower": None, "upper": None,
     "mean_income": 104207, "total_expenditure": 78535, "units_thousands": 135760,
     "persons": 2.4, "renter_pct": 35},
    {"index": 1, "label": "Lowest 20 percent", "lower": None, "upper": 29932,
     "mean_income": 16658, "total_expenditure": 35046, "units_thousands": 27139,
     "persons": 1.6, "renter_pct": 57},
    {"index": 2, "label": "Second 20 percent", "lower": 29932, "upper": 57452,
     "mean_income": 42925, "total_expenditure": 50054, "units_thousands": 27186,
     "persons": 2.1, "renter_pct": 42},
    {"index": 3, "label": "Third 20 percent", "lower": 57452, "upper": 94511,
     "mean_income": 74474, "total_expenditure": 66900, "units_thousands": 26959,
     "persons": 2.5, "renter_pct": 38},
    {"index": 4, "label": "Fourth 20 percent", "lower": 94511, "upper": 155925,
     "mean_income": 121548, "total_expenditure": 89972, "units_thousands": 27205,
     "persons": 2.9, "renter_pct": 26},
    {"index": 5, "label": "Highest 20 percent", "lower": 155925, "upper": None,
     "mean_income": 264510, "total_expenditure": 150342, "units_thousands": 27272,
     "persons": 3.2, "renter_pct": 12},
)


def quintile_for_income(income: float) -> int:
    """Which CE income quintile a pre-tax annual income falls in, 1..5.

    Raises on a missing or non-positive income rather than defaulting to the
    all-consumer-units column: an unstated income is not the average income.
    """
    if income is None:
        raise ValueError("income is required to pick a bracket; there is no default")
    income = float(income)
    if income <= 0:
        raise ValueError(f"income must be positive, got {income}")
    for q in QUINTILES[1:]:
        if q["upper"] is None or income < q["upper"]:
            return q["index"]
    return 5


# --------------------------------------------------------------------------
# CE 2024 — mean annual dollars per consumer unit
# --------------------------------------------------------------------------
#
# Tuple order matches the table columns:
#   (all consumer units, lowest 20%, second 20%, third 20%, fourth 20%, highest 20%)
# so CE_2024[item][q] indexes straight off quintile_for_income().
#
# Only the major components and the subcategories the benchmark actually uses
# are kept. The full table has roughly twice as many rows; the rest is meat and
# poultry detail that no card statement can resolve.

CE_2024 = {
    "Average annual expenditures":
        (78535, 35046, 50054, 66900, 89972, 150342),
    "Food":
        (10169, 5498, 7400, 9097, 11845, 16989),
    "Food at home":
        (6224, 3843, 4952, 5820, 7162, 9336),
    "Food away from home":
        (3945, 1655, 2448, 3277, 4682, 7652),
    "Alcoholic beverages":
        (643, 244, 342, 504, 726, 1395),
    "Housing":
        (26266, 14563, 19174, 24093, 29374, 44033),
    "Shelter":
        (16317, 9178, 11902, 15257, 17851, 27338),
    "Owned dwellings":
        (9310, 2989, 5153, 7657, 10557, 20133),
    "Rented dwellings":
        (5660, 5781, 6225, 6720, 5697, 3894),
    "Other lodging":
        (1347, 409, 523, 880, 1597, 3312),
    "Utilities, fuels, and public services":
        (4736, 2934, 4065, 4635, 5431, 6607),
    "Natural gas":
        (493, 287, 410, 458, 555, 751),
    "Electricity":
        (1833, 1276, 1674, 1813, 2023, 2376),
    "Telephone services":
        (1460, 834, 1204, 1465, 1770, 2024),
    "Cellular phone service":
        (1359, 720, 1084, 1376, 1678, 1935),
    "Water and other public services":
        (826, 474, 670, 803, 945, 1236),
    "Household operations":
        (1921, 851, 1128, 1511, 2009, 4093),
    "Personal services":
        (547, 185, 118, 320, 542, 1565),
    "Other household expenses":
        (1374, 666, 1009, 1191, 1467, 2529),
    "Housekeeping supplies":
        (877, 513, 681, 798, 1067, 1327),
    "Household furnishings and equipment":
        (2414, 1086, 1399, 1893, 3015, 4668),
    "Apparel and services":
        (2001, 1124, 1328, 1642, 2034, 3872),
    "Footwear":
        (461, 302, 299, 415, 450, 840),
    "Transportation":
        (13318, 5105, 8430, 11657, 15952, 25378),
    "Vehicle purchases (net outlay)":
        (5337, 1718, 2925, 4121, 5945, 11938),
    "Gasoline and other fuels":
        (2645, 1262, 2031, 2643, 3350, 3932),
    "Gasoline":
        (2411, 1177, 1893, 2442, 3058, 3477),
    "Other vehicle expenses":
        (4206, 1653, 3019, 4018, 5407, 6916),
    "Vehicle finance charges":
        (418, 101, 239, 376, 632, 739),
    "Maintenance and repairs":
        (984, 394, 693, 950, 1174, 1707),
    "Vehicle rental, leases, licenses, and other charges":
        (811, 263, 408, 664, 1083, 1630),
    "Vehicle insurance":
        (1993, 895, 1679, 2028, 2518, 2840),
    "Public and other transportation":
        (1131, 472, 456, 875, 1250, 2593),
    "Healthcare":
        (6197, 3445, 4826, 5676, 7247, 9771),
    "Health insurance":
        (4055, 2153, 3194, 3842, 4913, 6160),
    "Medical services":
        (1252, 661, 848, 1063, 1384, 2297),
    "Drugs":
        (658, 416, 644, 572, 700, 957),
    "Medical supplies":
        (233, 215, 140, 200, 251, 359),
    "Entertainment":
        (3609, 1316, 2156, 2764, 4133, 7660),
    "Fees and admissions":
        (935, 197, 324, 545, 1054, 2546),
    "Audio and visual equipment and services":
        (965, 538, 870, 889, 1084, 1444),
    "Pets, toys, hobbies, and playground equipment":
        (1069, 439, 645, 843, 1164, 2250),
    "Pets":
        (880, 368, 529, 673, 967, 1861),
    "Toys, hobbies, and playground equipment":
        (189, 70, 116, 169, 197, 390),
    "Other entertainment supplies, equipment, and services":
        (640, 142, 317, 487, 831, 1419),
    "Personal care products and services":
        (978, 427, 659, 852, 1145, 1802),
    "Reading":
        (125, 47, 75, 82, 122, 300),
    "Education":
        (1569, 828, 407, 749, 1353, 4492),
    "Tobacco products and smoking supplies":
        (352, 340, 359, 437, 360, 267),
    "Miscellaneous":
        (1218, 619, 767, 1173, 1458, 2072),
    "Cash contributions":
        (2292, 778, 1420, 1924, 2291, 5031),
    "Personal insurance and pensions":
        (9797, 711, 2712, 6248, 11934, 27279),
    "Life and other personal insurance":
        (575, 174, 305, 435, 618, 1339),
    "Retirement, pensions, and Social Security":
        (9222, 537, 2407, 5813, 11316, 25940),
}

# Cells BLS itself flags (footnote b) as having a relative standard error of 25
# percent or more. BLS considers those unreliable. They are kept because
# dropping them would silently shrink a group total, but anything built on one
# should be labelled, which is what ce_annual() reports back.
CE_HIGH_RSE = frozenset({
    ("Personal services", 1),
    ("Personal services", 2),
    ("Medical supplies", 1),
    ("Other entertainment supplies, equipment, and services", 1),
    ("Other entertainment supplies, equipment, and services", 4),
})


def ce_annual(item: str, quintile: int = 0) -> float | None:
    """Mean annual dollars for one CE line item in one quintile, or None."""
    row = CE_2024.get(item)
    return None if row is None else float(row[quintile])


def ce_is_unreliable(item: str, quintile: int = 0) -> bool:
    """True when BLS flags this cell as having an RSE of 25 percent or more."""
    return (item, quintile) in CE_HIGH_RSE


# --------------------------------------------------------------------------
# Mapping the CE onto this project's taxonomy
# --------------------------------------------------------------------------
#
# categorizer.TAXONOMY is mirrored here as literal strings rather than imported.
# reference_data must stay importable on its own — it is data, it should not
# drag in the 40kB rule engine — and a stale name here shows up immediately as
# an unmapped category in the benchmark rather than as a silent zero.
#
# The mapping is deliberately many-to-one and group-level. Three facts force it:
#
#   * A statement line cannot tell a restaurant from a coffee shop from a
#     delivery app in the way BLS can. All three are "Food away from home".
#   * Several project categories have no CE line at all. General retail is
#     spread by BLS across apparel, household furnishings and entertainment
#     supplies according to what was in the bag, which a statement total does
#     not record. Those categories are listed in UNMAPPED and excluded, not
#     forced into the nearest-looking bucket.
#   * Each CE line is used by at most one group, so group totals never double
#     count. They also do not sum to CE total expenditures, because the lines
#     in CE_OUT_OF_SCOPE are left out on purpose.

BENCHMARK_GROUPS = {
    "Food at home": {
        "categories": ("Groceries",),
        "ce_items": ("Food at home",),
        "note": "CE counts every grocery run including cash; a card total will read low.",
    },
    "Food away from home": {
        "categories": ("Dining", "Coffee", "Delivery"),
        "ce_items": ("Food away from home",),
        "note": "One CE line covers restaurants, coffee and delivery. Only the "
                "combined figure is comparable, never Delivery on its own.",
    },
    "Alcohol": {
        "categories": ("Alcohol/Bars",),
        "ce_items": ("Alcoholic beverages",),
        "note": "CE counts alcohol bought at home and out; it is excluded from "
                "food away from home, so there is no overlap.",
    },
    "Shelter": {
        "categories": ("Rent/Housing",),
        "ce_items": ("Shelter",),
        "note": "CE shelter blends renters and owners: mortgage interest, property "
                "taxes and upkeep are in it. 35 percent of consumer units rent. "
                "A renter paying by transfer will look far below this line.",
    },
    "Utilities and communications": {
        "categories": ("Utilities", "Phone/Internet"),
        "ce_items": ("Utilities, fuels, and public services",),
        "note": "Includes electricity, gas, water, refuse and phone service. "
                "Rent-inclusive utilities are invisible to a card statement.",
    },
    "Household goods and services": {
        "categories": ("Home",),
        "ce_items": ("Household furnishings and equipment", "Housekeeping supplies",
                     "Other household expenses"),
        "note": "Furniture, appliances, cleaning products and household services.",
    },
    "Childcare and personal services": {
        "categories": ("Childcare",),
        "ce_items": ("Personal services",),
        "note": "CE personal services is childcare and elder care. The lower two "
                "quintiles carry a high standard error here.",
    },
    "Clothing": {
        "categories": ("Clothing",),
        "ce_items": ("Apparel and services",),
        "note": "Footwear and alterations are inside this line.",
    },
    "Running a car": {
        "categories": ("Gas/Auto",),
        "ce_items": ("Gasoline and other fuels", "Maintenance and repairs",
                     "Vehicle rental, leases, licenses, and other charges",
                     "Vehicle finance charges"),
        "note": "Fuel and upkeep only. Buying the car is out of scope; see "
                "CE_OUT_OF_SCOPE.",
    },
    "Insurance": {
        "categories": ("Insurance",),
        "ce_items": ("Vehicle insurance", "Life and other personal insurance"),
        "note": "Health insurance sits under healthcare in the CE, so it is "
                "counted there and not here. A generic 'Insurance' category in "
                "this project may hold renters or health premiums, which makes "
                "this the loosest mapping in the table.",
    },
    "Fares and public transport": {
        "categories": ("Transport", "Travel"),
        "ce_items": ("Public and other transportation",),
        "note": "Airfares live here in the CE, which is why Travel is grouped "
                "with local transport rather than with Lodging.",
    },
    "Lodging": {
        "categories": ("Lodging",),
        "ce_items": ("Other lodging",),
        "note": "Hotels and rentals away from home.",
    },
    "Healthcare": {
        "categories": ("Health", "Pharmacy"),
        "ce_items": ("Healthcare",),
        "note": "Includes health insurance premiums, which for most people are "
                "payroll-deducted and never touch a card.",
    },
    "Recreation": {
        "categories": ("Entertainment", "Gaming", "Subscriptions", "Fitness"),
        "ce_items": ("Fees and admissions", "Audio and visual equipment and services",
                     "Toys, hobbies, and playground equipment",
                     "Other entertainment supplies, equipment, and services"),
        "note": "Streaming and gym memberships fall in fees and admissions and "
                "audio-visual services. Pets are broken out separately below.",
    },
    "Pets": {
        "categories": ("Pets",),
        "ce_items": ("Pets",),
        "note": "",
    },
    "Personal care": {
        "categories": ("Personal Care",),
        "ce_items": ("Personal care products and services",),
        "note": "",
    },
    "Education": {
        "categories": ("Education",),
        "ce_items": ("Education",),
        "note": "Tuition dominates and is extremely uneven across households; the "
                "third quintile mean is below the second.",
    },
    "Giving": {
        "categories": ("Charity", "Gifts"),
        "ce_items": ("Cash contributions",),
        "note": "CE cash contributions covers charity, alimony and support sent "
                "to people outside the household.",
    },
}

# Project categories with no honest CE line. Listed so the benchmark can say
# what it left out instead of quietly dropping the spend.
UNMAPPED = {
    "Shopping": "BLS allocates general retail by what was bought; a statement total cannot.",
    "Big Box": "Same problem, and a single Target run spans four CE categories.",
    "Electronics": "Split by BLS across household equipment and entertainment.",
    "Software/Tools": "No CE line; partly business expense, which the CE excludes.",
    "Fees/Interest": "Finance charges are not consumption and are not in CE expenditures.",
    "Taxes": "CE reports expenditures net of income tax; there is nothing to compare.",
    "Debt Payment": "Principal repayment is not an expenditure in the CE.",
    "Transfers/Cash": "Not spending.",
    "Payment": "Not spending.",
    "Credits/Refunds": "Not spending.",
    "Income": "Not spending.",
    "Uncategorized": "Unknown by definition; it is the size of this bucket that "
                     "decides whether any of the rest is worth reading.",
}

# CE lines deliberately outside every group, so group totals do not add up to
# CE total expenditures and nobody should expect them to.
CE_OUT_OF_SCOPE = {
    "Vehicle purchases (net outlay)": "A car is rarely bought on a credit card.",
    "Personal insurance and pensions": "Retirement and Social Security deductions.",
    "Reading": "Too small and too blurred with Subscriptions to be worth a group.",
    "Tobacco products and smoking supplies": "No corresponding project category.",
    "Miscellaneous": "A residual by construction; comparing against it means nothing.",
}


def group_for_category(category: str) -> str | None:
    """Which benchmark group a project category belongs to, if any."""
    return _CATEGORY_GROUP.get(category)


def group_annual(group: str, quintile: int) -> tuple[float, bool] | None:
    """(national annual dollars, any_cell_unreliable) for a benchmark group."""
    spec = BENCHMARK_GROUPS.get(group)
    if spec is None:
        return None
    total = 0.0
    shaky = False
    for item in spec["ce_items"]:
        v = ce_annual(item, quintile)
        if v is None:
            return None
        total += v
        shaky = shaky or ce_is_unreliable(item, quintile)
    return total, shaky


_CATEGORY_GROUP = {c: g for g, spec in BENCHMARK_GROUPS.items()
                   for c in spec["categories"]}


# --------------------------------------------------------------------------
# CPI-U index levels
# --------------------------------------------------------------------------
#
# Annual averages (BLS period M13) for 2016-2025. Storing index levels rather
# than percentage changes means any span can be computed exactly and the stored
# numbers can never disagree with a derived rate. The series ID above each row
# is the LABSTAT identifier, so every figure is re-checkable.

CPI_ANNUAL_INDEX = {
    # CUUR0000SA0
    "All items": {"2016": 240.007, "2017": 245.12, "2018": 251.107, "2019": 255.657, "2020": 258.811, "2021": 270.97, "2022": 292.655, "2023": 304.702, "2024": 313.689, "2025": 321.943},
    # CUUR0000SAF1
    "Food": {"2016": 247.931, "2017": 250.065, "2018": 253.558, "2019": 258.316, "2020": 267.219, "2021": 277.756, "2022": 305.388, "2023": 322.965, "2024": 330.263, "2025": 339.512},
    # CUUR0000SAF11
    "Food at home": {"2016": 239.065, "2017": 238.589, "2018": 239.661, "2019": 241.775, "2020": 250.233, "2021": 258.892, "2022": 288.451, "2023": 302.929, "2024": 306.536, "2025": 313.266},
    # CUUR0000SEFV
    "Food away from home": {"2016": 262.695, "2017": 268.826, "2018": 275.893, "2019": 284.41, "2020": 293.944, "2021": 307.296, "2022": 330.822, "2023": 354.256, "2024": 368.743, "2025": 382.378},
    # CUUR0000SAH
    "Housing": {"2016": 243.973, "2017": 251.229, "2018": 258.478, "2019": 265.952, "2020": 271.773, "2021": 280.72, "2022": 300.803, "2023": 320.172, "2024": 334.157, "2025": 346.675},
    # CUUR0000SAH1
    "Shelter": {"2016": 288.23, "2017": 297.803, "2018": 307.663, "2019": 318.053, "2020": 325.886, "2021": 334.575, "2022": 354.235, "2023": 380.643, "2024": 400.561, "2025": 415.164},
    # CUUR0000SEHA
    "Rent of primary residence": {"2016": 296.82, "2017": 308.143, "2018": 319.289, "2019": 331.124, "2020": 341.468, "2021": 349.135, "2022": 370.199, "2023": 399.643, "2024": 420.06, "2025": 434.889},
    # CUUR0000SAH21
    "Household furnishings and operations": {"2016": 191.05, "2017": 198.308, "2018": 200.826, "2019": 200.042, "2020": 199.488, "2021": 215.222, "2022": 253.306, "2023": 257.234, "2024": 263.5, "2025": 278.592},
    # CUUR0000SEHF01
    "Electricity": {"2016": 206.983, "2017": 211.437, "2018": 212.932, "2019": 213.362, "2020": 214.615, "2021": 223.892, "2022": 253.127, "2023": 267.804, "2024": 279.087, "2025": 292.027},
    # CUUR0000SEHF02
    "Utility (piped) gas service": {"2016": 160.628, "2017": 173.433, "2018": 173.609, "2019": 171.038, "2020": 171.387, "2021": 200.084, "2022": 251.763, "2023": 233.671, "2024": 229.554, "2025": 255.63},
    # CUUR0000SETB01
    "Gasoline (all types)": {"2016": 187.602, "2017": 211.77, "2018": 240.599, "2019": 232.003, "2020": 194.13, "2021": 264.017, "2022": 347.747, "2023": 311.574, "2024": 295.73, "2025": 278.773},
    # CUUR0000SAT
    "Transportation": {"2016": 194.894, "2017": 201.565, "2018": 210.664, "2019": 210.068, "2020": 201.334, "2021": 230.657, "2022": 266.33, "2023": 266.978, "2024": 270.685, "2025": 272.112},
    # CUUR0000SETA01
    "New vehicles": {"2016": 147.358, "2017": 146.992, "2018": 146.287, "2019": 146.834, "2020": 147.6, "2021": 156.24, "2022": 172.48, "2023": 178.899, "2024": 177.887, "2025": 178.462},
    # CUUR0000SETA02
    "Used cars and trucks": {"2016": 143.488, "2017": 138.259, "2018": 138.385, "2019": 139.763, "2020": 144.221, "2021": 182.628, "2022": 205.908, "2023": 191.222, "2024": 179.823, "2025": 184.931},
    # CUUR0000SETD
    "Motor vehicle maintenance and repair": {"2016": 275.351, "2017": 280.827, "2018": 286.358, "2019": 296.0, "2020": 305.99, "2021": 317.945, "2022": 344.139, "2023": 383.829, "2024": 407.275, "2025": 431.435},
    # CUUR0000SETE
    "Motor vehicle insurance": {"2016": 489.149, "2017": 526.911, "2018": 565.957, "2019": 571.032, "2020": 544.633, "2021": 565.313, "2022": 609.84, "2023": 716.004, "2024": 843.098, "2025": 893.544},
    # CUUR0000SAM
    "Medical care": {"2016": 463.675, "2017": 475.322, "2018": 484.707, "2019": 498.413, "2020": 518.876, "2021": 525.276, "2022": 546.554, "2023": 549.084, "2024": 563.841, "2025": 580.102},
    # CUUR0000SEMF
    "Prescription drugs": {"2016": 119.989, "2017": 123.444, "2018": 124.905, "2019": 124.772, "2020": 125.555, "2021": 123.646, "2022": 127.081, "2023": 132.058, "2024": 134.934, "2025": 136.309},
    # CUUR0000SAR
    "Recreation": {"2016": 116.984, "2017": 118.504, "2018": 119.091, "2019": 120.615, "2020": 122.242, "2021": 125.18, "2022": 130.846, "2023": 136.105, "2024": 138.158, "2025": 141.085},
    # CUUR0000SAA
    "Apparel": {"2016": 126.045, "2017": 125.612, "2018": 125.654, "2019": 124.052, "2020": 118.079, "2021": 120.993, "2022": 127.081, "2023": 130.579, "2024": 131.483, "2025": 131.311},
    # CUUR0000SAE1
    "Education": {"2016": 247.526, "2017": 253.211, "2018": 258.84, "2019": 265.84, "2020": 270.731, "2021": 274.555, "2022": 282.07, "2023": 290.707, "2024": 299.677, "2025": 309.644},
    # CUUR0000SEEE
    "Information technology commodities and services": {"2016": 116.14, "2017": 113.358, "2018": 111.096, "2019": 109.891, "2020": 108.939, "2021": 109.594, "2022": 107.704, "2023": 105.517, "2024": 103.791, "2025": 100.905},
    # CUUR0000SAG1
    "Other goods and services": {"2016": 224.273, "2017": 227.023, "2018": 231.062, "2019": 234.027, "2020": 238.329, "2021": 243.464, "2022": 257.904, "2023": 273.359, "2024": 282.454, "2025": 290.831},
    # CUUR0000SERA
    "Video and audio": {"2016": 100.946, "2017": 104.168, "2018": 104.201, "2019": 104.657, "2020": 107.078, "2021": 110.375, "2022": 112.655, "2023": 115.909, "2024": 117.718, "2025": 119.895},
    # CUUR0000SEGD
    "Personal care products": {"2016": 411.794, "2017": 423.96, "2018": 439.76, "2019": 448.898, "2020": 456.671, "2021": 465.353, "2022": 495.203, "2023": 530.364, "2024": 556.049, "2025": 578.842},
}

# Monthly index levels for the headline groupings only, so a personal window
# that does not line up with a calendar year can still be matched against the
# national number over exactly the same months.
CPI_MONTHLY_INDEX = {
    "All items": {
        "2023-01": 299.17, "2023-02": 300.84, "2023-03": 301.836, "2023-04": 303.363,
        "2023-05": 304.127, "2023-06": 305.109, "2023-07": 305.691, "2023-08": 307.026,
        "2023-09": 307.789, "2023-10": 307.671, "2023-11": 307.051, "2023-12": 306.746,
        "2024-01": 308.417, "2024-02": 310.326, "2024-03": 312.332, "2024-04": 313.548,
        "2024-05": 314.069, "2024-06": 314.175, "2024-07": 314.54, "2024-08": 314.796,
        "2024-09": 315.301, "2024-10": 315.664, "2024-11": 315.493, "2024-12": 315.605,
        "2025-01": 317.671, "2025-02": 319.082, "2025-03": 319.799, "2025-04": 320.795,
        "2025-05": 321.465, "2025-06": 322.561, "2025-07": 323.048, "2025-08": 323.976,
        "2025-09": 324.8, "2025-11": 324.122, "2025-12": 324.054,
    },
    "Food at home": {
        "2023-01": 301.435, "2023-02": 302.483, "2023-03": 301.918, "2023-04": 302.328,
        "2023-05": 302.535, "2023-06": 302.335, "2023-07": 303.455, "2023-08": 303.716,
        "2023-09": 303.925, "2023-10": 304.788, "2023-11": 303.224, "2023-12": 303.005,
        "2024-01": 305.037, "2024-02": 305.469, "2024-03": 305.426, "2024-04": 305.707,
        "2024-05": 305.679, "2024-06": 305.752, "2024-07": 306.643, "2024-08": 306.402,
        "2024-09": 307.767, "2024-10": 308.2, "2024-11": 307.972, "2024-12": 308.38,
        "2025-01": 310.936, "2025-02": 311.284, "2025-03": 312.815, "2025-04": 311.84,
        "2025-05": 312.491, "2025-06": 313.028, "2025-07": 313.263, "2025-08": 314.608,
        "2025-09": 316.042, "2025-11": 313.954, "2025-12": 315.664,
    },
    "Food away from home": {
        "2023-01": 345.677, "2023-02": 347.869, "2023-03": 349.944, "2023-04": 351.237,
        "2023-05": 352.892, "2023-06": 354.245, "2023-07": 354.862, "2023-08": 356.083,
        "2023-09": 357.488, "2023-10": 358.824, "2023-11": 360.383, "2023-12": 361.564,
        "2024-01": 363.249, "2024-02": 363.596, "2024-03": 364.546, "2024-04": 365.813,
        "2024-05": 367.099, "2024-06": 368.616, "2024-07": 369.383, "2024-08": 370.348,
        "2024-09": 371.604, "2024-10": 372.486, "2024-11": 373.53, "2024-12": 374.644,
        "2025-01": 375.532, "2025-02": 376.991, "2025-03": 378.363, "2025-04": 380.039,
        "2025-05": 381.228, "2025-06": 382.75, "2025-07": 383.808, "2025-08": 384.909,
        "2025-09": 385.451, "2025-11": 387.202, "2025-12": 389.889,
    },
    "Housing": {
        "2023-01": 313.747, "2023-02": 315.431, "2023-03": 316.514, "2023-04": 317.278,
        "2023-05": 318.205, "2023-06": 320.002, "2023-07": 321.087, "2023-08": 321.894,
        "2023-09": 323.563, "2023-10": 323.964, "2023-11": 324.735, "2023-12": 325.64,
        "2024-01": 328.222, "2024-02": 329.704, "2024-03": 331.247, "2024-04": 331.688,
        "2024-05": 332.777, "2024-06": 334.087, "2024-07": 335.056, "2024-08": 335.931,
        "2024-09": 336.776, "2024-10": 337.47, "2024-11": 338.048, "2024-12": 338.883,
        "2025-01": 340.875, "2025-02": 342.398, "2025-03": 343.512, "2025-04": 344.801,
        "2025-05": 345.938, "2025-06": 347.593, "2025-07": 348.21, "2025-08": 349.277,
        "2025-09": 349.773, "2025-11": 349.973, "2025-12": 351.072,
    },
    "Shelter": {
        "2023-01": 369.585, "2023-02": 372.51, "2023-03": 374.971, "2023-04": 376.636,
        "2023-05": 378.776, "2023-06": 380.594, "2023-07": 382.226, "2023-08": 383.221,
        "2023-09": 385.433, "2023-10": 386.435, "2023-11": 387.892, "2023-12": 389.433,
        "2024-01": 391.896, "2024-02": 393.906, "2024-03": 396.174, "2024-04": 397.539,
        "2024-05": 399.274, "2024-06": 400.234, "2024-07": 401.545, "2024-08": 403.257,
        "2024-09": 404.133, "2024-10": 405.287, "2024-11": 406.25, "2024-12": 407.242,
        "2025-01": 409.153, "2025-02": 410.635, "2025-03": 411.99, "2025-04": 413.389,
        "2025-05": 414.689, "2025-06": 415.455, "2025-07": 416.271, "2025-08": 417.902,
        "2025-09": 418.598, "2025-11": 418.593, "2025-12": 420.134,
    },
    "Transportation": {
        "2023-01": 257.874, "2023-02": 259.712, "2023-03": 261.969, "2023-04": 267.402,
        "2023-05": 268.862, "2023-06": 270.146, "2023-07": 270.602, "2023-08": 274.22,
        "2023-09": 272.517, "2023-10": 270.027, "2023-11": 267.035, "2023-12": 263.375,
        "2024-01": 262.11, "2024-02": 266.638, "2024-03": 272.485, "2024-04": 276.687,
        "2024-05": 276.623, "2024-06": 273.579, "2024-07": 273.326, "2024-08": 271.391,
        "2024-09": 269.604, "2024-10": 269.724, "2024-11": 268.45, "2024-12": 267.606,
        "2025-01": 270.384, "2025-02": 271.04, "2025-03": 270.061, "2025-04": 272.503,
        "2025-05": 272.946, "2025-06": 273.391, "2025-07": 273.452, "2025-08": 273.91,
        "2025-09": 274.301, "2025-11": 272.615, "2025-12": 268.63,
    },
    "Medical care": {
        "2023-01": 551.422, "2023-02": 549.487, "2023-03": 547.805, "2023-04": 547.219,
        "2023-05": 547.42, "2023-06": 547.432, "2023-07": 546.698, "2023-08": 548.082,
        "2023-09": 548.431, "2023-10": 549.762, "2023-11": 551.769, "2023-12": 553.485,
        "2024-01": 557.215, "2024-02": 557.236, "2024-03": 559.935, "2024-04": 561.612,
        "2024-05": 564.249, "2024-06": 565.301, "2024-07": 564.039, "2024-08": 564.407,
        "2024-09": 566.266, "2024-10": 567.87, "2024-11": 568.773, "2024-12": 569.189,
        "2025-01": 571.899, "2025-02": 573.32, "2025-03": 574.739, "2025-04": 576.988,
        "2025-05": 578.25, "2025-06": 580.978, "2025-07": 583.856, "2025-08": 583.875,
        "2025-09": 584.858, "2025-11": 585.213, "2025-12": 587.144,
    },
    "Recreation": {
        "2023-01": 134.077, "2023-02": 135.519, "2023-03": 135.693, "2023-04": 136.375,
        "2023-05": 136.28, "2023-06": 136.3, "2023-07": 136.401, "2023-08": 136.102,
        "2023-09": 136.687, "2023-10": 136.726, "2023-11": 136.292, "2023-12": 136.811,
        "2024-01": 137.8, "2024-02": 138.322, "2024-03": 138.183, "2024-04": 138.412,
        "2024-05": 138.118, "2024-06": 138.115, "2024-07": 138.277, "2024-08": 138.214,
        "2024-09": 137.651, "2024-10": 138.12, "2024-11": 138.341, "2024-12": 138.343,
        "2025-01": 140.059, "2025-02": 140.788, "2025-03": 140.74, "2025-04": 140.627,
        "2025-05": 140.63, "2025-06": 140.961, "2025-07": 141.587, "2025-08": 141.394,
        "2025-09": 141.827, "2025-11": 140.821, "2025-12": 142.505,
    },
}

# The six major components the brief asks for, in the order a report reads them.
HEADLINE_SERIES = ("Food at home", "Food away from home", "Shelter",
                   "Transportation", "Medical care", "Recreation")

# Project category -> CPI series. Some of these are frank proxies: a phone bill
# is not the IT commodities index and a gym is not the recreation index, they
# are just the closest published thing. Proxies are named in CPI_PROXY so a
# report can mark them rather than pass them off as exact.
CATEGORY_TO_CPI = {
    "Groceries": "Food at home",
    "Dining": "Food away from home",
    "Coffee": "Food away from home",
    "Delivery": "Food away from home",
    "Rent/Housing": "Rent of primary residence",
    "Utilities": "Electricity",
    "Phone/Internet": "Information technology commodities and services",
    "Transport": "Transportation",
    "Gas/Auto": "Gasoline (all types)",
    "Clothing": "Apparel",
    "Home": "Household furnishings and operations",
    "Electronics": "Information technology commodities and services",
    "Software/Tools": "Information technology commodities and services",
    "Subscriptions": "Video and audio",
    "Entertainment": "Recreation",
    "Gaming": "Recreation",
    "Fitness": "Recreation",
    "Health": "Medical care",
    "Pharmacy": "Prescription drugs",
    "Insurance": "Motor vehicle insurance",
    "Personal Care": "Personal care products",
    "Education": "Education",
}

CPI_PROXY = frozenset({
    "Utilities", "Phone/Internet", "Software/Tools", "Fitness", "Gaming",
    "Insurance", "Electronics",
})

# Categories with no CPI series worth naming. Alcohol, lodging, travel, pets,
# giving and general retail all have published indexes that were not pulled
# into this file; rather than substitute a neighbouring series they are absent,
# and cpi_rate() returns None for them.
CPI_UNMAPPED = frozenset({
    "Alcohol/Bars", "Travel", "Lodging", "Shopping", "Big Box", "Pets",
    "Charity", "Gifts", "Childcare", "Fees/Interest", "Taxes",
})


def cpi_rate(series: str, year: int = CPI_LATEST_YEAR) -> float | None:
    """Annual-average CPI-U inflation for a series, in percent, or None."""
    idx = CPI_ANNUAL_INDEX.get(series)
    if idx is None:
        return None
    now, prev = idx.get(str(year)), idx.get(str(year - 1))
    if not now or not prev:
        return None
    return (now / prev - 1.0) * 100.0


def cpi_rate_for_category(category: str, year: int = CPI_LATEST_YEAR) -> float | None:
    """Annual CPI-U inflation for the series this project's category maps to."""
    series = CATEGORY_TO_CPI.get(category)
    return None if series is None else cpi_rate(series, year)


def cpi_change_between(series: str, start_month: str, end_month: str) -> float | None:
    """Percent change in a monthly CPI-U series between two YYYY-MM months.

    Returns None when either endpoint is outside the stored window or falls on
    a month BLS did not publish. The alternative — snapping to the nearest
    available month — would quietly change the span being measured.
    """
    idx = CPI_MONTHLY_INDEX.get(series)
    if idx is None:
        return None
    a, b = idx.get(start_month), idx.get(end_month)
    if not a or not b:
        return None
    return (b / a - 1.0) * 100.0


def cpi_annualised(series: str, start_month: str, end_month: str,
                   months: float) -> float | None:
    """cpi_change_between() restated as an annual rate over ``months`` months."""
    change = cpi_change_between(series, start_month, end_month)
    if change is None or months <= 0:
        return None
    return ((1.0 + change / 100.0) ** (12.0 / months) - 1.0) * 100.0


ANNUAL_RATES = {name: {y: round((idx[y] / idx[str(int(y) - 1)] - 1.0) * 100.0, 1)
                       for y in sorted(idx) if str(int(y) - 1) in idx}
                for name, idx in CPI_ANNUAL_INDEX.items()}


# --------------------------------------------------------------------------
# What these numbers are not
# --------------------------------------------------------------------------

CAVEATS = (
    "The CE measures a consumer unit — everyone in a household sharing "
    "expenses — over a full year, through cash, cheque, transfer and card "
    "alike. A statement measures one person's card. Comparing the two "
    "understates the personal side by however much is paid another way.",

    "CE means are pulled upward by a minority of large spenders. The median "
    "household spends less than the mean in almost every category, so being "
    "under the national average is the common case, not an achievement.",

    "Quintiles are set on income before taxes for the whole consumer unit. A "
    "single earner on $72,000 and a two-earner household on $72,000 land in "
    "the same bracket while living quite differently.",

    "CE 2024 was collected during a redesign of the interview questionnaire "
    "that changed how housing and medical expenses are asked about, so 2024 "
    "figures are not perfectly comparable with earlier years.",

    "CPI-U is a national urban average across a fixed basket. It is not the "
    "price of anything anyone actually bought, and it makes no allowance for "
    "where you live.",

    "CPI-U for October 2025 does not exist: collection lapsed during the 2025 "
    "appropriations gap. Any window spanning that month is incomplete.",

    "CE 2024 and CPI 2025 are the most recent published figures at the time "
    "this file was written. Both lag the present by design.",
)


def _check() -> list[str]:
    """Internal consistency checks. Run with ``python -m finances.reference_data``."""
    problems = []
    for name, row in CE_2024.items():
        if len(row) != 6:
            problems.append(f"{name}: expected 6 columns, got {len(row)}")

    seen: dict[str, str] = {}
    for group, spec in BENCHMARK_GROUPS.items():
        for item in spec["ce_items"]:
            if item not in CE_2024:
                problems.append(f"{group}: unknown CE item {item!r}")
            if item in seen:
                problems.append(f"{item!r} double counted in {seen[item]} and {group}")
            seen[item] = group

    mapped = set(_CATEGORY_GROUP) | set(UNMAPPED)
    taxonomy = mapped | set(CATEGORY_TO_CPI) | CPI_UNMAPPED
    for c in sorted(taxonomy - mapped):
        problems.append(f"category {c!r} has no benchmark group and is not in UNMAPPED")

    for c, s in CATEGORY_TO_CPI.items():
        if s not in CPI_ANNUAL_INDEX:
            problems.append(f"{c}: unknown CPI series {s!r}")
    for s in HEADLINE_SERIES:
        if s not in CPI_MONTHLY_INDEX:
            problems.append(f"headline series {s!r} has no monthly index")
    for s, idx in CPI_MONTHLY_INDEX.items():
        for m in CPI_MISSING_MONTHS:
            if m in idx:
                problems.append(f"{s}: {m} should not be present")

    for q in QUINTILES[1:]:
        lo = q["lower"] or 0
        if q["upper"] is not None and quintile_for_income((lo + q["upper"]) / 2) != q["index"]:
            problems.append(f"quintile_for_income disagrees with bounds for {q['label']}")
    return problems


def main():
    print(f"{SOURCE['ce']['survey']} {CE_YEAR}, {SOURCE['ce']['table']}, "
          f"released {SOURCE['ce']['released']} ({SOURCE['ce']['release_id']})")
    print(f"{SOURCE['cpi']['survey']}, {SOURCE['cpi']['area']}, "
          f"{SOURCE['cpi']['adjustment']}\n")

    print("Income brackets (income before taxes, annual):")
    for q in QUINTILES[1:]:
        lo = "under" if q["lower"] is None else f"${q['lower']:,}"
        hi = "and up" if q["upper"] is None else f"${q['upper']:,}"
        rng = f"{lo} {hi}" if q["lower"] is None or q["upper"] is None else f"{lo} - {hi}"
        print(f"  {q['index']}  {q['label']:<20} {rng:<24} "
              f"spends ${q['total_expenditure']:,}/yr")

    print(f"\nBenchmark groups ({len(BENCHMARK_GROUPS)}), annual dollars by quintile:")
    print(f"  {'group':<32}" + "".join(f"{q['label'].split()[0]:>10}" for q in QUINTILES[1:]))
    for g in BENCHMARK_GROUPS:
        cells = []
        for q in QUINTILES[1:]:
            got = group_annual(g, q["index"])
            cells.append(f"{got[0]:>9,.0f}{'*' if got[1] else ' '}" if got else f"{'-':>10}")
        print(f"  {g:<32}" + "".join(cells))
    print("  * at least one component has a relative standard error of 25% or more")

    print(f"\nCPI-U annual inflation, percent, {CPI_LATEST_YEAR - 4}-{CPI_LATEST_YEAR}:")
    years = [str(y) for y in range(CPI_LATEST_YEAR - 4, CPI_LATEST_YEAR + 1)]
    print(f"  {'series':<26}" + "".join(f"{y:>8}" for y in years))
    for name in ("All items",) + HEADLINE_SERIES:
        r = ANNUAL_RATES[name]
        print(f"  {name:<26}" + "".join(f"{r.get(y, float('nan')):>8.1f}" for y in years))

    print(f"\n{len(UNMAPPED)} project categories have no CE counterpart: "
          f"{', '.join(sorted(UNMAPPED))}")

    problems = _check()
    print("\nSelf-check: " + ("ok" if not problems else f"{len(problems)} problem(s)"))
    for p in problems:
        print(f"  {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
