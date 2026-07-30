"""Turn the ads workbook into seed_ads rows + the shared keyword tail.

Generation runs on the VPS, where data/*.xlsx deliberately does not exist (it is
gitignored live operator content). So the workbook is converted to SQL here and
loaded there:

    python scripts/import_seed_ads.py data/20260630_ads_100_rows.xlsx -o seed.sql
    scp seed.sql dispatch:/tmp/
    ssh dispatch 'sudo -u postgres psql -d craigslist -f /tmp/seed.sql'

Parsing uses only the standard library — an .xlsx is a zip of XML — so this runs
inside the API container or anywhere else without openpyxl.

Each description splits into a per-city head and a tail that is byte-identical
across every row (dot padding, keyword block, zip list, city list). The head
becomes the row's fallback copy; the tail is stored once in
generation_settings.tail_template and appended to whatever the model writes.
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

CELL_RE = re.compile(
    r'(?s)<c r="([A-Z]+)\d+"[^>]*>(?:<is><t[^>]*>(.*?)</t></is>|<v>(.*?)</v>)?</c>'
)
ROW_RE = re.compile(r"(?s)<row[^>]*>.*?</row>")
# The tail always begins at the first line that is nothing but a dot.
TAIL_RE = re.compile(r"\n[ \t]*\.[ \t]*\n")


def _unescape(s: str) -> str:
    return (
        s.replace("&#10;", "\n")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def _cells(row_xml: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in CELL_RE.finditer(row_xml):
        raw = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        out[m.group(1)] = _unescape(raw)
    return out


def _sq(v: str) -> str:
    """Single-quote a value for SQL."""
    return "'" + (v or "").replace("'", "''") + "'"


def parse(path: Path) -> tuple[list[dict], str]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "xl/sharedStrings.xml" in names:
            raise SystemExit(
                "This workbook uses a shared-strings table; expected inline strings. "
                "Re-export it, or extend this parser."
            )
        xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")

    rows = ROW_RE.findall(xml)
    if len(rows) < 2:
        raise SystemExit("No data rows found.")

    seeds: list[dict] = []
    tails: set[str] = set()
    for row_xml in rows[1:]:
        c = _cells(row_xml)
        county, city, desc = c.get("A", "").strip(), c.get("B", "").strip(), c.get("F", "")
        title = c.get("D", "").strip()
        if not (county and city and title and desc):
            continue

        split = TAIL_RE.search(desc)
        if split:
            head, tail = desc[: split.start()].rstrip(), desc[split.start():].lstrip("\n")
        else:
            head, tail = desc.rstrip(), ""
        tails.add(tail)

        seeds.append({
            "county": county,
            "city": city,
            "postal_code": c.get("E", "").strip(),
            "service_offered": c.get("C", "").strip(),
            "license_number": c.get("G", "").strip(),
            "phone_number": c.get("H", "").strip(),
            "fallback_title": title,
            "fallback_head": head,
        })

    non_empty = [t for t in tails if t.strip()]
    if len(set(non_empty)) > 1:
        print(
            f"warning: {len(set(non_empty))} distinct tails found; using the longest",
            file=sys.stderr,
        )
    tail = max(non_empty, key=len) if non_empty else ""
    return seeds, tail


def to_sql(seeds: list[dict], tail: str) -> str:
    out = ["BEGIN;", "-- Idempotent: replaces the seed pool wholesale.", "DELETE FROM seed_ads;"]
    for s in seeds:
        out.append(
            "INSERT INTO seed_ads (county, city, postal_code, service_offered, "
            "license_number, phone_number, fallback_title, fallback_head) VALUES ("
            + ", ".join(
                _sq(s[k]) for k in (
                    "county", "city", "postal_code", "service_offered",
                    "license_number", "phone_number", "fallback_title", "fallback_head",
                )
            )
            + ");"
        )
    out.append(
        "UPDATE generation_settings SET tail_template = "
        + _sq(tail)
        + ", updated_at = NOW() WHERE singleton;"
    )
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("seed_ads.sql"))
    args = ap.parse_args()

    seeds, tail = parse(args.xlsx)
    args.out.write_text(to_sql(seeds, tail), encoding="utf-8")

    counties: dict[str, int] = {}
    for s in seeds:
        counties[s["county"]] = counties.get(s["county"], 0) + 1
    print(f"parsed {len(seeds)} seed ads from {args.xlsx.name}")
    for county, n in sorted(counties.items()):
        print(f"  {county:<14} {n}")
    print(f"shared tail: {len(tail):,} chars")
    print(f"wrote {args.out}  ({args.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
