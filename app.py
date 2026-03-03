from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

BASE_URL = "https://azure.microsoft.com"
DEFAULT_CULTURE = "en-in"
DEFAULT_REGION = "us-east"

HTTP = requests.Session()
HTTP.headers.update(
    {
        "User-Agent": "vm-flask-live/1.0 (+https://azure.microsoft.com/pricing/calculator/)",
        "Accept": "application/json",
    }
)

PRICE_TYPE_BY_BILLING = {
    "payg": "perhour",
    "one-year": "perhouroneyearreserved",
    "three-year": "perhourthreeyearreserved",
    "five-year": "perhourfiveyearreserved",
    "sv-one-year": "perunitoneyearsavings",
    "sv-three-year": "perunitthreeyearsavings",
    "sv-five-year": "perunitfiveyearsavings",
}


def _url(path: str) -> str:
    return f"{BASE_URL}{path}"


@lru_cache(maxsize=128)
def fetch_json(path: str) -> Dict[str, Any]:
    response = HTTP.get(_url(path), timeout=30)
    response.raise_for_status()
    return response.json()


@lru_cache(maxsize=16)
def get_categories(culture: str) -> List[Dict[str, Any]]:
    return fetch_json(f"/api/v2/pricing/categories/calculator/?culture={culture}")


@lru_cache(maxsize=16)
def get_vm_metadata() -> Dict[str, Any]:
    return fetch_json("/api/v4/pricing/virtual-machines/metadata/")


@lru_cache(maxsize=64)
def get_vm_calculator(region: str, culture: str) -> Dict[str, Any]:
    return fetch_json(f"/api/v4/pricing/virtual-machines/calculator/{region}/?culture={culture}")


def _build_size_options(metadata: Dict[str, Any]) -> List[Dict[str, str]]:
    keys = [
        "sizesPayGo",
        "sizesOneYear",
        "sizesThreeYear",
        "sizesFiveYear",
        "sizesSavingsOneYear",
        "sizesSavingsThreeYear",
    ]
    seen = set()
    size_options: List[Dict[str, str]] = []
    for key in keys:
        for item in metadata.get(key, []):
            slug = item.get("slug")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            size_options.append(
                {
                    "slug": slug,
                    "displayName": item.get("displayName") or slug.upper(),
                }
            )
    return size_options


def _valid_slugs(items: List[Dict[str, str]]) -> set[str]:
    return {item.get("slug", "") for item in items if item.get("slug")}


def _validated_choice(
    value: str, items: List[Dict[str, str]], fallback: str, field_label: str, notes: List[str]
) -> str:
    allowed = _valid_slugs(items)
    if value in allowed:
        return value
    if fallback in allowed:
        notes.append(f"'{value}' is not a valid {field_label}. Used '{fallback}'.")
        return fallback
    if items and items[0].get("slug"):
        first = items[0]["slug"]
        notes.append(f"'{value}' is not a valid {field_label}. Used '{first}'.")
        return first
    notes.append(f"No valid options were found for {field_label}.")
    return value


def _form_options(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "billingOptions": [
            x
            for x in metadata.get("billingOptions", [])
            if x.get("slug") and x.get("displayName")
        ],
        "operatingSystems": metadata.get("operatingSystems", []),
        "regions": metadata.get("regions", []),
        "tiers": metadata.get("tiers", []),
        "sizes": _build_size_options(metadata),
    }


def _default_state(metadata: Dict[str, Any]) -> Dict[str, Any]:
    schema = metadata.get("schema", {})
    return {
        "culture": DEFAULT_CULTURE,
        "region": schema.get("region", DEFAULT_REGION),
        "operatingSystem": schema.get("operatingSystem", "windows"),
        "billingOption": schema.get("billingOption", "payg"),
        "tier": schema.get("tier", "standard"),
        "size": schema.get("size", "d2v3"),
        "hours": float(schema.get("hours", 730)),
        "count": int(schema.get("count", 1)),
    }


def _extract_numeric_price(node: Any) -> Optional[float]:
    if isinstance(node, (int, float)):
        return float(node)
    if isinstance(node, dict) and isinstance(node.get("value"), (int, float)):
        return float(node["value"])
    return None


def _find_offer_key(
    offers: Dict[str, Any], operating_system: str, size: str, tier: str
) -> Optional[str]:
    candidates = [
        f"{operating_system}-{size}-{tier}",
        f"{operating_system}-{size}",
    ]
    for key in candidates:
        if key in offers:
            return key

    prefix = f"{operating_system}-{size}-"
    for key in offers.keys():
        if key.startswith(prefix):
            return key
    return None


def _find_offer_keys(
    offers: Dict[str, Any], operating_system: str, size: str, tier: str
) -> List[str]:
    keys: List[str] = []
    ordered_candidates = [f"{operating_system}-{size}-{tier}", f"{operating_system}-{size}"]
    for candidate in ordered_candidates:
        if candidate in offers and candidate not in keys:
            keys.append(candidate)

    prefix = f"{operating_system}-{size}-"
    for key in offers.keys():
        if key.startswith(prefix) and key not in keys:
            keys.append(key)
    return keys


def _offer_supports_selection(
    offer: Dict[str, Any], billing_option: str, region: str
) -> bool:
    prices = offer.get("prices", {})
    if not isinstance(prices, dict):
        return False

    requested_price_type = PRICE_TYPE_BY_BILLING.get(billing_option, "perhour")
    region_map = prices.get(requested_price_type)
    if not isinstance(region_map, dict):
        return False
    if region not in region_map:
        return False

    return _extract_numeric_price(region_map.get(region)) is not None


def _valid_sizes_for_selection(
    all_sizes: List[Dict[str, str]],
    calculator_data: Dict[str, Any],
    operating_system: str,
    tier: str,
    billing_option: str,
    region: str,
) -> List[Dict[str, str]]:
    offers = calculator_data.get("offers", {})
    if not isinstance(offers, dict) or not offers:
        return []

    valid_sizes: List[Dict[str, str]] = []
    for item in all_sizes:
        slug = item.get("slug")
        if not slug:
            continue
        candidates = _find_offer_keys(offers, operating_system, slug, tier)
        if not candidates:
            continue

        is_supported = any(
            _offer_supports_selection(offers[key], billing_option, region)
            for key in candidates
            if key in offers
        )
        if is_supported:
            valid_sizes.append(item)

    return valid_sizes


def calculate_vm_price(
    calculator_data: Dict[str, Any],
    operating_system: str,
    size: str,
    tier: str,
    billing_option: str,
    region: str,
    hours: float,
    count: int,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    offers = calculator_data.get("offers", {})
    offer_key = _find_offer_key(offers, operating_system, size, tier)

    if not offer_key:
        return None, [
            "No matching offer found for this OS + size + tier in current VM pricing data."
        ]

    offer = offers[offer_key]
    prices = offer.get("prices", {})
    requested_price_type = PRICE_TYPE_BY_BILLING.get(billing_option, "perhour")
    price_type = requested_price_type

    if price_type not in prices:
        available_types = list(prices.keys())
        if not available_types:
            return None, [f"No price types available for offer '{offer_key}'."]
        price_type = available_types[0]
        notes.append(
            f"Requested price type '{requested_price_type}' not present. "
            f"Used '{price_type}' instead."
        )

    region_map = prices.get(price_type, {})
    if not isinstance(region_map, dict) or not region_map:
        return None, [f"No regional prices found for '{offer_key}' / '{price_type}'."]

    selected_region = region if region in region_map else DEFAULT_REGION
    if selected_region not in region_map:
        selected_region = next(iter(region_map))
    if selected_region != region:
        notes.append(
            f"Region '{region}' was unavailable for this offer. Used '{selected_region}'."
        )

    unit_price = _extract_numeric_price(region_map[selected_region])
    if unit_price is None:
        return None, [f"Price value is missing for '{offer_key}' in '{selected_region}'."]

    monthly_cost = unit_price * hours * count
    return (
        {
            "offerKey": offer_key,
            "priceType": price_type,
            "region": selected_region,
            "unitPrice": unit_price,
            "hours": hours,
            "count": count,
            "monthlyCost": monthly_cost,
            "cores": offer.get("cores"),
            "ram": offer.get("ram"),
            "series": offer.get("series"),
            "responseTime": calculator_data.get("responseTime"),
        },
        notes,
    )


@app.route("/", methods=["GET", "POST"])
def index() -> str:
    notes: List[str] = []
    result = None
    vm_service_count = 0

    try:
        categories = get_categories(DEFAULT_CULTURE)
        vm_service_count = sum(
            1
            for category in categories
            for product in category.get("products", [])
            if product.get("slug") == "virtual-machines"
        )
        metadata = get_vm_metadata()
    except requests.RequestException as exc:
        return render_template(
            "index.html",
            options={"billingOptions": [], "operatingSystems": [], "regions": [], "tiers": [], "sizes": []},
            state={
                "culture": DEFAULT_CULTURE,
                "region": DEFAULT_REGION,
                "operatingSystem": "windows",
                "billingOption": "payg",
                "tier": "standard",
                "size": "d2v3",
                "hours": 730,
                "count": 1,
            },
            result=None,
            notes=[f"Failed to load live metadata: {exc}"],
            vm_service_count=0,
        )

    options = _form_options(metadata)
    state = _default_state(metadata)

    if request.method == "POST":
        try:
            state = {
                "culture": request.form.get("culture", DEFAULT_CULTURE).strip()
                or DEFAULT_CULTURE,
                "region": request.form.get("region", state["region"]),
                "operatingSystem": request.form.get(
                    "operatingSystem", state["operatingSystem"]
                ),
                "billingOption": request.form.get("billingOption", state["billingOption"]),
                "tier": request.form.get("tier", state["tier"]),
                "size": request.form.get("size", state["size"]),
                "hours": float(request.form.get("hours", state["hours"])),
                "count": int(request.form.get("count", state["count"])),
            }
        except ValueError:
            notes.append("Hours must be numeric and VM count must be an integer.")

    state["region"] = _validated_choice(
        state["region"], options["regions"], DEFAULT_REGION, "region", notes
    )
    state["operatingSystem"] = _validated_choice(
        state["operatingSystem"], options["operatingSystems"], "windows", "operating system", notes
    )
    state["billingOption"] = _validated_choice(
        state["billingOption"], options["billingOptions"], "payg", "billing option", notes
    )
    state["tier"] = _validated_choice(
        state["tier"], options["tiers"], "standard", "tier", notes
    )

    try:
        calculator_data = get_vm_calculator(state["region"], state["culture"])
        valid_sizes = _valid_sizes_for_selection(
            all_sizes=options["sizes"],
            calculator_data=calculator_data,
            operating_system=state["operatingSystem"],
            tier=state["tier"],
            billing_option=state["billingOption"],
            region=state["region"],
        )
        options["sizes"] = valid_sizes

        valid_size_slugs = {item["slug"] for item in valid_sizes if item.get("slug")}
        if state["size"] not in valid_size_slugs:
            if valid_sizes:
                previous = state["size"]
                state["size"] = valid_sizes[0]["slug"]
                notes.append(
                    f"Size '{previous}' is not supported for the selected OS/tier/billing/region. "
                    f"Used '{state['size']}'."
                )
            else:
                notes.append(
                    "No VM sizes are available for this OS + tier + billing option + region combination."
                )

        if request.method == "POST" and valid_sizes:
            result, calc_notes = calculate_vm_price(
                calculator_data=calculator_data,
                operating_system=state["operatingSystem"],
                size=state["size"],
                tier=state["tier"],
                billing_option=state["billingOption"],
                region=state["region"],
                hours=state["hours"],
                count=state["count"],
            )
            notes.extend(calc_notes)
    except requests.RequestException as exc:
        notes.append(f"Live API request failed: {exc}")

    return render_template(
        "index.html",
        options=options,
        state=state,
        result=result,
        notes=notes,
        vm_service_count=vm_service_count,
    )


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}


@app.get("/api/vm/valid-sizes")
def valid_sizes_api() -> Any:
    culture = (request.args.get("culture") or DEFAULT_CULTURE).strip() or DEFAULT_CULTURE
    region = request.args.get("region") or DEFAULT_REGION
    operating_system = request.args.get("operatingSystem") or "windows"
    billing_option = request.args.get("billingOption") or "payg"
    tier = request.args.get("tier") or "standard"
    selected_size = request.args.get("size") or ""

    try:
        metadata = get_vm_metadata()
        options = _form_options(metadata)

        region = _validated_choice(region, options["regions"], DEFAULT_REGION, "region", [])
        operating_system = _validated_choice(
            operating_system, options["operatingSystems"], "windows", "operating system", []
        )
        billing_option = _validated_choice(
            billing_option, options["billingOptions"], "payg", "billing option", []
        )
        tier = _validated_choice(tier, options["tiers"], "standard", "tier", [])

        calculator_data = get_vm_calculator(region, culture)
        valid_sizes = _valid_sizes_for_selection(
            all_sizes=options["sizes"],
            calculator_data=calculator_data,
            operating_system=operating_system,
            tier=tier,
            billing_option=billing_option,
            region=region,
        )

        valid_slugs = {item["slug"] for item in valid_sizes if item.get("slug")}
        selected = selected_size if selected_size in valid_slugs else None
        if not selected and valid_sizes:
            selected = valid_sizes[0]["slug"]

        return jsonify(
            {
                "sizes": valid_sizes,
                "selectedSize": selected,
                "count": len(valid_sizes),
            }
        )
    except requests.RequestException as exc:
        return jsonify({"error": str(exc), "sizes": [], "selectedSize": None, "count": 0}), 502


if __name__ == "__main__":
    app.run(debug=True)
