#!/usr/bin/env python3
"""
Daily refresh of fuel-prices-world.json from GlobalPetrolPrices.com.

Run by .github/workflows/refresh-fuel-prices.yml every day at 06:15 UTC.
The script:
  1. Fetches the gasoline_prices/ and diesel_prices/ HTML pages.
  2. Extracts every "<country>: $<value>" line (or whatever DOM table they
     render) via a tolerant regex over the rendered text.
  3. Maps each scraped country name to the ISO code used by the app.
  4. Applies the same multi-grade overlay table the app uses, with each
     overlay anchored to that country's refreshed primary price using
     industry-standard premium-uplift ratios.
  5. Writes fuel-prices-world.json with lastUpdated = the source's date.

Failure modes are LOUD on purpose — if parsing breaks (e.g. GPP changes
their HTML), the workflow logs the error and skips committing. The app
keeps serving the previous JSON until we patch the script.

Idempotent: produces byte-identical output for identical input, so the
workflow's "commit only if changed" guard suppresses no-op commits.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

GASOLINE_URL = "https://www.globalpetrolprices.com/gasoline_prices/"
DIESEL_URL = "https://www.globalpetrolprices.com/diesel_prices/"

USER_AGENT = (
    "Mozilla/5.0 (compatible; FuelPricesWorld-Refresh/1.0; "
    "+https://github.com/moebahlawan87-commits/moe)"
)

# Country name → ISO 3166-1 alpha-2. Keep in sync with FuelPrices.kt
# BASE list. Lower-case keys; we lower-case the scraped name too.
NAME_TO_ISO: dict[str, str] = {
    # Africa
    "algeria": "DZ", "angola": "AO", "egypt": "EG", "ethiopia": "ET",
    "ghana": "GH", "kenya": "KE", "libya": "LY", "morocco": "MA",
    "nigeria": "NG", "senegal": "SN", "south africa": "ZA",
    "tunisia": "TN", "uganda": "UG",
    # Americas
    "argentina": "AR", "brazil": "BR", "canada": "CA", "chile": "CL",
    "colombia": "CO", "costa rica": "CR", "cuba": "CU",
    "dominican republic": "DO", "ecuador": "EC", "guatemala": "GT",
    "honduras": "HN", "mexico": "MX", "panama": "PA", "peru": "PE",
    "paraguay": "PY", "united states": "US", "usa": "US",
    "uruguay": "UY", "venezuela": "VE",
    # Asia
    "bangladesh": "BD", "china": "CN", "hong kong": "HK", "india": "IN",
    "indonesia": "ID", "japan": "JP", "kazakhstan": "KZ",
    "south korea": "KR", "malaysia": "MY", "nepal": "NP",
    "pakistan": "PK", "philippines": "PH", "singapore": "SG",
    "sri lanka": "LK", "taiwan": "TW", "thailand": "TH",
    "uzbekistan": "UZ", "vietnam": "VN",
    # Europe
    "austria": "AT", "belgium": "BE", "bulgaria": "BG", "croatia": "HR",
    "czech republic": "CZ", "czechia": "CZ", "denmark": "DK",
    "estonia": "EE", "finland": "FI", "france": "FR", "germany": "DE",
    "greece": "GR", "hungary": "HU", "iceland": "IS", "ireland": "IE",
    "italy": "IT", "latvia": "LV", "lithuania": "LT",
    "luxembourg": "LU", "netherlands": "NL", "norway": "NO",
    "poland": "PL", "portugal": "PT", "romania": "RO", "russia": "RU",
    "serbia": "RS", "slovakia": "SK", "slovenia": "SI", "spain": "ES",
    "sweden": "SE", "switzerland": "CH", "turkey": "TR",
    "ukraine": "UA", "united kingdom": "GB", "uk": "GB",
    # Middle East
    "bahrain": "BH", "iran": "IR", "iraq": "IQ", "israel": "IL",
    "jordan": "JO", "kuwait": "KW", "lebanon": "LB", "oman": "OM",
    "qatar": "QA", "saudi arabia": "SA",
    "united arab emirates": "AE", "uae": "AE",
    # Oceania
    "australia": "AU", "new zealand": "NZ", "fiji": "FJ",
    "papua new guinea": "PG",
}

# Static descriptor for each ISO code (flag emoji, display name, region,
# currency). Mirrors the BASE list in FuelPrices.kt so the JSON shape
# stays stable regardless of source.
META: dict[str, tuple[str, str, str, str]] = {
    # ISO: (flag, name, region, currency)
    "DZ": ("🇩🇿", "Algeria", "Africa", "DZD"),
    "AO": ("🇦🇴", "Angola", "Africa", "AOA"),
    "EG": ("🇪🇬", "Egypt", "Africa", "EGP"),
    "ET": ("🇪🇹", "Ethiopia", "Africa", "ETB"),
    "GH": ("🇬🇭", "Ghana", "Africa", "GHS"),
    "KE": ("🇰🇪", "Kenya", "Africa", "KES"),
    "LY": ("🇱🇾", "Libya", "Africa", "LYD"),
    "MA": ("🇲🇦", "Morocco", "Africa", "MAD"),
    "NG": ("🇳🇬", "Nigeria", "Africa", "NGN"),
    "SN": ("🇸🇳", "Senegal", "Africa", "XOF"),
    "ZA": ("🇿🇦", "South Africa", "Africa", "ZAR"),
    "TN": ("🇹🇳", "Tunisia", "Africa", "TND"),
    "UG": ("🇺🇬", "Uganda", "Africa", "UGX"),
    "AR": ("🇦🇷", "Argentina", "Americas", "ARS"),
    "BR": ("🇧🇷", "Brazil", "Americas", "BRL"),
    "CA": ("🇨🇦", "Canada", "Americas", "CAD"),
    "CL": ("🇨🇱", "Chile", "Americas", "CLP"),
    "CO": ("🇨🇴", "Colombia", "Americas", "COP"),
    "CR": ("🇨🇷", "Costa Rica", "Americas", "CRC"),
    "CU": ("🇨🇺", "Cuba", "Americas", "CUP"),
    "DO": ("🇩🇴", "Dominican Republic", "Americas", "DOP"),
    "EC": ("🇪🇨", "Ecuador", "Americas", "USD"),
    "GT": ("🇬🇹", "Guatemala", "Americas", "GTQ"),
    "HN": ("🇭🇳", "Honduras", "Americas", "HNL"),
    "MX": ("🇲🇽", "Mexico", "Americas", "MXN"),
    "PA": ("🇵🇦", "Panama", "Americas", "PAB"),
    "PE": ("🇵🇪", "Peru", "Americas", "PEN"),
    "PY": ("🇵🇾", "Paraguay", "Americas", "PYG"),
    "US": ("🇺🇸", "United States", "Americas", "USD"),
    "UY": ("🇺🇾", "Uruguay", "Americas", "UYU"),
    "VE": ("🇻🇪", "Venezuela", "Americas", "VES"),
    "BD": ("🇧🇩", "Bangladesh", "Asia", "BDT"),
    "CN": ("🇨🇳", "China", "Asia", "CNY"),
    "HK": ("🇭🇰", "Hong Kong", "Asia", "HKD"),
    "IN": ("🇮🇳", "India", "Asia", "INR"),
    "ID": ("🇮🇩", "Indonesia", "Asia", "IDR"),
    "JP": ("🇯🇵", "Japan", "Asia", "JPY"),
    "KZ": ("🇰🇿", "Kazakhstan", "Asia", "KZT"),
    "KR": ("🇰🇷", "South Korea", "Asia", "KRW"),
    "MY": ("🇲🇾", "Malaysia", "Asia", "MYR"),
    "NP": ("🇳🇵", "Nepal", "Asia", "NPR"),
    "PK": ("🇵🇰", "Pakistan", "Asia", "PKR"),
    "PH": ("🇵🇭", "Philippines", "Asia", "PHP"),
    "SG": ("🇸🇬", "Singapore", "Asia", "SGD"),
    "LK": ("🇱🇰", "Sri Lanka", "Asia", "LKR"),
    "TW": ("🇹🇼", "Taiwan", "Asia", "TWD"),
    "TH": ("🇹🇭", "Thailand", "Asia", "THB"),
    "UZ": ("🇺🇿", "Uzbekistan", "Asia", "UZS"),
    "VN": ("🇻🇳", "Vietnam", "Asia", "VND"),
    "AT": ("🇦🇹", "Austria", "Europe", "EUR"),
    "BE": ("🇧🇪", "Belgium", "Europe", "EUR"),
    "BG": ("🇧🇬", "Bulgaria", "Europe", "BGN"),
    "HR": ("🇭🇷", "Croatia", "Europe", "EUR"),
    "CZ": ("🇨🇿", "Czechia", "Europe", "CZK"),
    "DK": ("🇩🇰", "Denmark", "Europe", "DKK"),
    "EE": ("🇪🇪", "Estonia", "Europe", "EUR"),
    "FI": ("🇫🇮", "Finland", "Europe", "EUR"),
    "FR": ("🇫🇷", "France", "Europe", "EUR"),
    "DE": ("🇩🇪", "Germany", "Europe", "EUR"),
    "GR": ("🇬🇷", "Greece", "Europe", "EUR"),
    "HU": ("🇭🇺", "Hungary", "Europe", "HUF"),
    "IS": ("🇮🇸", "Iceland", "Europe", "ISK"),
    "IE": ("🇮🇪", "Ireland", "Europe", "EUR"),
    "IT": ("🇮🇹", "Italy", "Europe", "EUR"),
    "LV": ("🇱🇻", "Latvia", "Europe", "EUR"),
    "LT": ("🇱🇹", "Lithuania", "Europe", "EUR"),
    "LU": ("🇱🇺", "Luxembourg", "Europe", "EUR"),
    "NL": ("🇳🇱", "Netherlands", "Europe", "EUR"),
    "NO": ("🇳🇴", "Norway", "Europe", "NOK"),
    "PL": ("🇵🇱", "Poland", "Europe", "PLN"),
    "PT": ("🇵🇹", "Portugal", "Europe", "EUR"),
    "RO": ("🇷🇴", "Romania", "Europe", "RON"),
    "RU": ("🇷🇺", "Russia", "Europe", "RUB"),
    "RS": ("🇷🇸", "Serbia", "Europe", "RSD"),
    "SK": ("🇸🇰", "Slovakia", "Europe", "EUR"),
    "SI": ("🇸🇮", "Slovenia", "Europe", "EUR"),
    "ES": ("🇪🇸", "Spain", "Europe", "EUR"),
    "SE": ("🇸🇪", "Sweden", "Europe", "SEK"),
    "CH": ("🇨🇭", "Switzerland", "Europe", "CHF"),
    "TR": ("🇹🇷", "Turkey", "Europe", "TRY"),
    "UA": ("🇺🇦", "Ukraine", "Europe", "UAH"),
    "GB": ("🇬🇧", "United Kingdom", "Europe", "GBP"),
    "BH": ("🇧🇭", "Bahrain", "Middle East", "BHD"),
    "IR": ("🇮🇷", "Iran", "Middle East", "IRR"),
    "IQ": ("🇮🇶", "Iraq", "Middle East", "IQD"),
    "IL": ("🇮🇱", "Israel", "Middle East", "ILS"),
    "JO": ("🇯🇴", "Jordan", "Middle East", "JOD"),
    "KW": ("🇰🇼", "Kuwait", "Middle East", "KWD"),
    "LB": ("🇱🇧", "Lebanon", "Middle East", "LBP"),
    "OM": ("🇴🇲", "Oman", "Middle East", "OMR"),
    "QA": ("🇶🇦", "Qatar", "Middle East", "QAR"),
    "SA": ("🇸🇦", "Saudi Arabia", "Middle East", "SAR"),
    "AE": ("🇦🇪", "United Arab Emirates", "Middle East", "AED"),
    "AU": ("🇦🇺", "Australia", "Oceania", "AUD"),
    "NZ": ("🇳🇿", "New Zealand", "Oceania", "NZD"),
    "FJ": ("🇫🇯", "Fiji", "Oceania", "FJD"),
    "PG": ("🇵🇬", "Papua New Guinea", "Oceania", "PGK"),
}

# (grade_name, multiplier_vs_primary). Primary grade has multiplier 1.0
# and matches the scraped GlobalPetrolPrices value. Other grades use
# industry-standard premium uplifts. Edit here to refine.
GRADE_TEMPLATES: dict[str, list[tuple[str, float]]] = {
    "LB": [("95 octane", 1.000), ("98 octane", 1.082)],
    "AE": [("Special 91", 0.970), ("Super 95", 1.000), ("Ultra 98", 1.060)],
    "SA": [("91 octane", 0.902), ("95 octane", 1.000)],
    "KW": [("Regular 91", 0.897), ("Premium 95", 1.000), ("Ultra 98", 1.112)],
    "QA": [("Super 91", 0.932), ("Plus 95", 1.000), ("Premium 98", 1.116)],
    "BH": [("Jayyid 91", 0.912), ("Mumtaz 95", 1.000)],
    "OM": [("M91", 0.962), ("M95", 1.000), ("M98", 1.095)],
    "JO": [("90 octane", 0.925), ("95 octane", 1.000), ("98 octane", 1.110)],
    "IL": [("95 octane", 1.000), ("98 octane", 1.088)],
    "GB": [("Premium 95", 1.000), ("Super 98", 1.088)],
    "DE": [("Super E10 95", 1.000), ("Super Plus 98", 1.090)],
    "FR": [("SP95-E10", 0.982), ("SP95", 1.000), ("SP98", 1.055)],
    "IT": [("Benzina 95", 1.000), ("Super 98", 1.087)],
    "ES": [("Gasolina 95", 1.000), ("Gasolina 98", 1.088)],
    "NL": [("Euro 95", 1.000), ("Super 98", 1.082)],
    "BE": [("Euro 95", 1.000), ("Super 98", 1.086)],
    "AT": [("Eurosuper 95", 1.000), ("Super Plus 98", 1.113)],
    "SE": [("95 oktan", 1.000), ("98 oktan", 1.070)],
    "NO": [("95 oktan", 1.000), ("98 oktan", 1.072)],
    "CH": [("Bleifrei 95", 1.000), ("Bleifrei 98", 1.082)],
    "IE": [("Petrol 95", 1.000), ("Super 98", 1.092)],
    "DK": [("Blyfri 95", 1.000), ("Blyfri 98", 1.071)],
    "FI": [("95E10", 1.000), ("98E5", 1.068)],
    "PL": [("Pb 95", 1.000), ("Pb 98", 1.085)],
    "PT": [("Gasolina 95", 1.000), ("Gasolina 98", 1.088)],
    "GR": [("Amólyvdi 95", 1.000), ("Súper 98", 1.089)],
    "US": [("Regular 87", 1.000), ("Mid-grade 89", 1.055), ("Premium 91-93", 1.147)],
    "CA": [("Regular 87", 1.000), ("Mid-grade 89", 1.042), ("Premium 91-93", 1.127)],
    "MX": [("Magna 87", 1.000), ("Premium 92", 1.081)],
    "BR": [("Comum", 1.000), ("Aditivada", 1.041), ("Premium", 1.116)],
    "AU": [("91 Unleaded", 1.000), ("95 Premium", 1.071), ("98 Ultra", 1.149)],
    "NZ": [("91 Regular", 1.000), ("95 Premium", 1.055), ("98 Super", 1.114)],
    "JP": [("Regular", 1.000), ("High-Octane", 1.073)],
    "CN": [("92#", 1.000), ("95#", 1.058), ("98#", 1.120)],
    "IN": [("Regular", 1.000), ("Premium / Speed", 1.072)],
    "KR": [("Regular 92", 1.000), ("Premium 95+", 1.085)],
    "TH": [("Gasohol 91", 0.971), ("Gasohol 95", 1.000), ("Super 98", 1.078)],
    "MY": [("RON 95", 0.556), ("RON 97", 1.321)],  # subsidised 95 vs market 97
    "SG": [("92 octane", 1.000), ("95 octane", 1.034), ("98 octane", 1.134)],
    "PH": [("Regular 91", 0.940), ("95 Unleaded", 1.000), ("Premium 97", 1.051)],
}

# Fallback values for countries the source occasionally omits. Keep as a
# safety net so we always emit a complete 97-country payload.
FALLBACK = {
    "IQ": (0.684, 0.700),   # Iraq diesel missing from GPP diesel page
    "PG": (1.551, 1.620),   # PNG diesel missing
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_country_prices(html: str, fuel: str) -> tuple[dict[str, float], str | None]:
    """GlobalPetrolPrices renders each row as two separate absolutely-
    positioned divs: a country anchor like <a href='/Lebanon/{fuel}_prices/'>
    and, much later in the document, a price div like
    <div ...color: #000000;">1.142</div>. Both lists are in the same order,
    so we extract each list and zip by index.

    fuel: "gasoline" or "diesel" (used in the anchor URL pattern)."""
    # Date appears multiple times in the page as "18-May-2026".
    date = None
    m = re.search(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b", html)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%d-%b-%Y")
            date = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Country names from the anchor URL slugs (canonical English, no
    # asterisks, no HTML entities).
    name_pat = re.compile(
        rf"href=['\"]/([A-Za-z][A-Za-z_-]*?)/{re.escape(fuel)}_prices/"
    )
    names = name_pat.findall(html)

    # Prices from the absolutely-positioned value divs. There's one per
    # country in the same order as the country names.
    price_pat = re.compile(
        r'color:\s*#000000;">\s*([0-9]+\.[0-9]+)\s*</div>'
    )
    prices = price_pat.findall(html)

    if not names or not prices:
        return {}, date
    n = min(len(names), len(prices))
    out: dict[str, float] = {}
    for i in range(n):
        # URL slugs use hyphens for word boundaries: "Saudi-Arabia",
        # "Hong-Kong", "United-Arab-Emirates". Convert to a lower-case
        # space-delimited key matching NAME_TO_ISO.
        key = names[i].replace("-", " ").replace("_", " ").lower().strip()
        try:
            out[key] = float(prices[i])
        except ValueError:
            continue
    return out, date


def map_to_iso(scraped: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, price in scraped.items():
        iso = NAME_TO_ISO.get(name)
        if iso:
            out[iso] = price
    return out


def build_payload(
    gas_by_iso: dict[str, float],
    diesel_by_iso: dict[str, float],
    src_date: str,
) -> dict:
    countries = []
    missing: list[str] = []
    for iso, (flag, name, region, currency) in META.items():
        gas = gas_by_iso.get(iso)
        diesel = diesel_by_iso.get(iso)
        if gas is None or diesel is None:
            fb = FALLBACK.get(iso)
            if fb is None:
                missing.append(iso)
                continue
            gas = gas if gas is not None else fb[0]
            diesel = diesel if diesel is not None else fb[1]
        row = {
            "code": iso,
            "flag": flag,
            "name": name,
            "region": region,
            "currency": currency,
            "gasolineUsdPerL": round(gas, 3),
            "dieselUsdPerL": round(diesel, 3),
        }
        tmpl = GRADE_TEMPLATES.get(iso)
        if tmpl:
            row["gasolineGrades"] = [
                {"name": g, "usdPerL": round(gas * mult, 3)}
                for (g, mult) in tmpl
            ]
        countries.append(row)
    if missing:
        print(f"WARN: missing data for {missing}", file=sys.stderr)
    return {
        "schema": 2,
        "app": "FuelPricesWorld",
        "lastUpdated": src_date,
        "source": (
            "GlobalPetrolPrices.com weekly retail price index (fetched daily "
            "by the FuelPricesWorld refresh workflow). Multi-grade overlays "
            "are anchored to each country's primary grade with industry-"
            "standard premium uplifts. Indicative figures — actual "
            "pump prices vary by region, brand, and day."
        ),
        "countries": countries,
    }


def main() -> int:
    print("Fetching gasoline page...")
    gas_html = fetch(GASOLINE_URL)
    gas_raw, gas_date = parse_country_prices(gas_html, "gasoline")
    print(f"  parsed {len(gas_raw)} gasoline rows, date={gas_date}")

    print("Fetching diesel page...")
    diesel_html = fetch(DIESEL_URL)
    diesel_raw, diesel_date = parse_country_prices(diesel_html, "diesel")
    print(f"  parsed {len(diesel_raw)} diesel rows, date={diesel_date}")

    if len(gas_raw) < 50 or len(diesel_raw) < 50:
        print("FATAL: parse yielded too few rows — source HTML may have "
              "changed. Aborting without writing JSON.", file=sys.stderr)
        return 2

    gas_by_iso = map_to_iso(gas_raw)
    diesel_by_iso = map_to_iso(diesel_raw)
    src_date = (
        gas_date or diesel_date
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    payload = build_payload(gas_by_iso, diesel_by_iso, src_date)

    out_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fuel-prices-world.json",
    )
    # Pretty-print with the same shape the manual exporter produces so
    # diffs in git stay readable.
    lines = ["{"]
    lines.append(f'  "schema": {payload["schema"]},')
    lines.append(f'  "app": "{payload["app"]}",')
    lines.append(f'  "lastUpdated": "{payload["lastUpdated"]}",')
    lines.append(
        '  "source": ' + json.dumps(payload["source"], ensure_ascii=False) + ","
    )
    lines.append('  "countries": [')
    rows = []
    for c in payload["countries"]:
        compact = json.dumps(c, ensure_ascii=False, separators=(",", ":"))
        rows.append("    " + compact)
    lines.append(",\n".join(rows))
    lines.append("  ]")
    lines.append("}")
    text = "\n".join(lines) + "\n"

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"Wrote {out_path} ({len(payload['countries'])} countries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
