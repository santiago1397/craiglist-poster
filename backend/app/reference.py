"""Canonical location data for the South Florida service area.

Extracted from data/20260630_ads_100_rows.xlsx (100 rows, 42 county/city pairs).
This is reference data, not user content, so it lives in code rather than the
database — the composer needs it before any draft exists.

Why it is a fixed list rather than free text: `poster._select_subarea()` picks
the Craigslist subarea radio by substring-matching the county string. A typo, or
a county it does not recognise, falls through to "pick the first radio" — the ad
still posts, just under the wrong subarea, silently. Constraining the input at
the point of authoring is the cheapest place to stop that.

`subarea_supported` mirrors what `_select_subarea` can actually route today.
Monroe (the Keys) is in the workbook but has no branch there, so it is flagged
rather than quietly offered as if it were safe.
"""
from __future__ import annotations

# county -> ordered list of (city, default zip)
LOCATIONS: dict[str, list[tuple[str, str]]] = {
    "Miami-Dade": [
        ("Coral Gables", "33134"),
        ("Doral", "33166"),
        ("Hialeah", "33010"),
        ("Homestead", "33030"),
        ("Key Biscayne", "33149"),
        ("Miami", "33133"),
        ("Miami Beach", "33139"),
        ("Miami Lakes", "33014"),
        ("Miami Springs", "33166"),
        ("South Miami", "33143"),
    ],
    "Broward": [
        ("Coconut Creek", "33073"),
        ("Cooper City", "33328"),
        ("Coral Springs", "33065"),
        ("Dania Beach", "33004"),
        ("Davie", "33314"),
        ("Deerfield Beach", "33441"),
        ("Fort Lauderdale", "33312"),
        ("Hallandale Beach", "33009"),
        ("Hollywood", "33020"),
        ("Lauderdale Lakes", "33311"),
        ("Lauderdale-By-The-Sea", "33308"),
        ("Lauderhill", "33313"),
        ("Lighthouse Point", "33064"),
        ("Margate", "33063"),
        ("North Lauderdale", "33068"),
        ("Oakland Park", "33334"),
        ("Parkland", "33076"),
        ("Pembroke Pines", "33028"),
        ("Plantation", "33317"),
        ("Pompano Beach", "33060"),
        ("Southwest Ranches", "33330"),
        ("Sunrise", "33322"),
        ("Tamarac", "33321"),
        ("Weston", "33326"),
        ("Wilton Manors", "33305"),
    ],
    "Palm Beach": [
        ("Boca Raton", "33432"),
        ("Boynton Beach", "33435"),
        ("Delray Beach", "33444"),
        ("Lake Worth Beach", "33460"),
        ("West Palm Beach", "33401"),
    ],
    "Monroe": [
        ("Key Largo", "33037"),
        ("Key West", "33040"),
    ],
}

# Substrings `poster._select_subarea` matches on. Kept here so the dashboard can
# warn about a county the poster cannot route, instead of the operator finding
# out from a misfiled ad.
_ROUTABLE_SUBSTRINGS = ("palm", "broward", "miami", "dade")


def subarea_supported(county: str) -> bool:
    c = (county or "").lower()
    return any(s in c for s in _ROUTABLE_SUBSTRINGS)


# All three rotate across the workbook, roughly evenly.
PHONE_NUMBERS = ["(954) 634-7370", "(954) 634-7420", "(954) 634-7360"]

# Constant across all 100 rows.
LICENSE_NUMBER = "CCC1334317"
SERVICE_OFFERED = "skilled trade services"


def as_payload() -> dict:
    """Shape the composer consumes."""
    return {
        "counties": [
            {
                "name": county,
                "subarea_supported": subarea_supported(county),
                "cities": [{"city": c, "zip": z} for c, z in cities],
            }
            for county, cities in LOCATIONS.items()
        ],
        "phone_numbers": PHONE_NUMBERS,
        "license_number": LICENSE_NUMBER,
        "service_offered": SERVICE_OFFERED,
    }
