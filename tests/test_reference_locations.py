"""Reference data must stay in step with what the poster can actually route.

The composer offers these values, so if `subarea_supported` ever disagrees with
`poster._select_subarea`, the UI would present a county as safe that silently
files ads under the wrong Craigslist subarea.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "src"))

from app.reference import (  # noqa: E402
    LICENSE_NUMBER, LOCATIONS, PHONE_NUMBERS, as_payload, subarea_supported,
)

ok = []

# Shape: every county has cities, every city has a 5-digit zip.
total_cities = 0
for county, cities in LOCATIONS.items():
    assert cities, f"{county} has no cities"
    for city, zipc in cities:
        assert city.strip(), f"empty city in {county}"
        assert zipc.isdigit() and len(zipc) == 5, f"bad zip for {city}: {zipc!r}"
        total_cities += 1
assert total_cities == 42, f"expected 42 county/city pairs from the workbook, got {total_cities}"
ok.append(f"42 county/city pairs across {len(LOCATIONS)} counties, all zips well-formed")

# No city may appear under two counties — the composer keys cities off county.
seen: dict[str, str] = {}
for county, cities in LOCATIONS.items():
    for city, _ in cities:
        assert city not in seen, f"{city} listed under both {seen[city]} and {county}"
        seen[city] = county
ok.append("no city is listed under two counties")

# The real check: our routable flag must match the poster's own matching rule,
# reproduced from poster._select_subarea.
def poster_can_route(county: str) -> bool:
    c = (county or "").lower()
    return "palm" in c or "broward" in c or "miami" in c or "dade" in c

for county in LOCATIONS:
    assert subarea_supported(county) == poster_can_route(county), (
        f"{county}: reference says {subarea_supported(county)}, "
        f"poster says {poster_can_route(county)}"
    )
ok.append("subarea_supported agrees with poster._select_subarea for every county")

# Monroe is the known gap; assert it is flagged rather than silently offered.
assert "Monroe" in LOCATIONS
assert not subarea_supported("Monroe"), "Monroe must be flagged as not routable"
assert subarea_supported("Miami-Dade") and subarea_supported("Broward") \
    and subarea_supported("Palm Beach")
ok.append("Monroe flagged as not routable; the other three are routable")

# Constants match the workbook.
assert LICENSE_NUMBER == "CCC1334317"
assert len(PHONE_NUMBERS) == 3 and all(p.startswith("(954)") for p in PHONE_NUMBERS)
ok.append("license and the three rotating phone numbers match the workbook")

# Payload shape the composer consumes.
p = as_payload()
assert {c["name"] for c in p["counties"]} == set(LOCATIONS)
assert all("cities" in c and "subarea_supported" in c for c in p["counties"])
assert p["license_number"] == LICENSE_NUMBER
ok.append("as_payload() exposes counties, cities, zips, phones and license")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
