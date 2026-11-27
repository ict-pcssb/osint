"""Verify the public map feature used by the M9 indoor candidate, without credentials."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

import httpx2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from travel_patch_indoor import MAPPED_MUSEUM
from travel_state import BEIJING

API = "https://api.openstreetmap.org/api/0.6"
NODE = 13273591111


async def verify():
    async with httpx2.AsyncClient(timeout=15, follow_redirects=False, headers={
        "User-Agent": "GuizhouTravel/1.0 read-only M9 verification",
    }) as client:
        collected = datetime.now(BEIJING).isoformat(timespec="seconds")
        node_url = f"{API}/node/{NODE}.json"
        response = await client.get(node_url)
        response.raise_for_status()
        node, = response.json()["elements"]
        tags = node.get("tags", {})
        expected = json.loads(MAPPED_MUSEUM.read_text(encoding="utf-8"))
        assert (node["type"], node["id"], tags.get("name"), tags.get("tourism")) == (
            "node", NODE, "贵州地质博物馆", "museum")
        assert (node["lat"], node["lon"]) == (expected["latitude"], expected["longitude"])
        assert expected["source_url"] == f"https://www.openstreetmap.org/node/{NODE}"
        latitude, longitude = node["lat"], node["lon"]
        box = f"{longitude-0.0025:.7f},{latitude-0.0025:.7f},{longitude+0.0025:.7f},{latitude+0.0025:.7f}"
        map_url = API + "/map.json"
        area = await client.get(map_url, params={"bbox": box})
        area.raise_for_status()
        entrances = [row for row in area.json()["elements"] if row.get("tags", {}).get("entrance")]
        related = [row["id"] for row in entrances if "博物馆" in row["tags"].get("name", "")]
        map_status = "unverified" if not tags.get("entrance") and not related else "requires_review"
        return dict(status="PASS", layer="real_provider_read_only_map", data_mode="live",
                    collected_at=collected,
                    requests=[dict(method="GET", url=node_url, http_status=response.status_code),
                              dict(method="GET", url=map_url, bbox=box, http_status=area.status_code)],
                    poi=dict(node_id=NODE, name=tags["name"], latitude=latitude, longitude=longitude,
                             coordinate_system="WGS84", feature_type=tags["tourism"],
                             node_entrance_tag=tags.get("entrance"), source_url=expected["source_url"]),
                    reviewed_area=dict(entrance_tag_count=len(entrances), names_indicating_museum=related),
                    missing=dict(exact_visitor_entrance=map_status, route_travel_minutes=None, taxi_fare_fen=None,
                                 admission_fen=None, confirmed_opening=None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(verify())
    except (httpx2.TransportError, httpx2.HTTPStatusError) as exc:
        report = dict(status="BLOCKED", layer="real_provider_read_only_map", cause=type(exc).__name__)
    except (AssertionError, KeyError, ValueError) as exc:
        report = dict(status="FAIL", layer="real_provider_read_only_map", cause=type(exc).__name__)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

