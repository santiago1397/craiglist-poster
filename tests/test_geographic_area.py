"""geographic_area must reach the Craigslist form, and fall back to city."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from craigslist_auto.content import Ad, ad_from_draft  # noqa: E402

ok = []

# Blank falls back to the structured city, so nothing changes for old drafts.
a = ad_from_draft({"id": 1, "title": "t", "body": "b", "city": "Davie"})
assert a.geo_text == "Davie", a.geo_text
ok.append("blank geographic_area falls back to the city")

# A NULL column (existing rows before the migration) behaves the same.
a = ad_from_draft({"id": 1, "title": "t", "body": "b", "city": "Davie",
                   "geographic_area": None})
assert a.geo_text == "Davie", a.geo_text
ok.append("a NULL geographic_area falls back to the city")

# A custom value wins, including a multi-town list.
a = ad_from_draft({"id": 1, "title": "t", "body": "b", "city": "Davie",
                   "geographic_area": "Davie, Plantation, Cooper City"})
assert a.geo_text == "Davie, Plantation, Cooper City", a.geo_text
assert a.city == "Davie", "the structured city must not be overwritten"
ok.append("a custom area wins and does not disturb the structured city")

# Whitespace-only is treated as blank rather than typed as spaces.
a = ad_from_draft({"id": 1, "title": "t", "body": "b", "city": "Doral",
                   "geographic_area": "   "})
assert a.geo_text == "Doral", a.geo_text
ok.append("a whitespace-only area is treated as blank")

# The dataclass still constructs positionally, so nothing else broke.
direct = Ad(title="t", body="b", county="Broward", city="Davie",
            service_offered="s", postal_code="33314", license_number="L",
            phone_number="P", photos=[], source_row=0)
assert direct.geo_text == "Davie"
ok.append("Ad still constructs without the new field")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
