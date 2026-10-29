"""Synthetic laboratory fixtures; not operating recommendations or defaults."""
from radio_plan import validate_catalog, catalog_digest, default_plan, pair_plans

CATALOG = validate_catalog({"schema": 1, "default_pair_id": "default", "pairs": [
    {"id": identifier, "label": f"Laboratory pair {index + 1}", "rx_hz": receive,
     "tx_hz": transmit, "tone_hz": 88.5}
    for index, (identifier, receive, transmit) in enumerate((
        ("default", 145300000, 433500000), ("rpt1", 145330000, 433525000),
        ("rpt2", 145360000, 433550000), ("rpt3", 145390000, 433575000),
        ("rpt4", 145420000, 433600000), ("rpt5", 145450000, 433625000),
    ))
]})
DIGEST = catalog_digest(CATALOG)
PLAN = default_plan(CATALOG)
PAIR_PLANS = pair_plans(CATALOG)
