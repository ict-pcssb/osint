"""Versioned public travel directory. Names may resolve to several IDs."""
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import json


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    label: str
    aliases: tuple = ()
    parent_id: str | None = None
    authority_hosts: tuple = ()
    links: tuple = ()
    directions: dict = field(default_factory=dict)
    coordinates: dict | None = None
    weather_area: str | None = None
    station_id: str | None = None
    airport_id: str | None = None


class Catalog:
    def __init__(self, rows):
        entities = [Entity(**{**row, "aliases":tuple(row.get("aliases", ())),
                              "links":tuple(row.get("links", ())),
                              "authority_hosts":tuple(row.get("authority_hosts", ()))}) for row in rows]
        self.entities = {item.id:item for item in entities}
        if len(self.entities) != len(entities):
            raise ValueError("目录 ID 重复")
        for item in entities:
            if item.kind not in {"poi","entrance","station","line_direction","airport","city","parking"} or not item.id or not item.label:
                raise ValueError("目录实体无效")
            if item.kind == "entrance" and (item.parent_id not in self.entities or self.entities[item.parent_id].kind != "poi"):
                raise ValueError("入口须关联实际景点 ID")
            if item.coordinates is not None:
                coords=item.coordinates
                if (coords["system"] != "WGS84" or not -90 <= coords["latitude"] <= 90
                        or not -180 <= coords["longitude"] <= 180 or not coords["source_url"].startswith("https://")):
                    raise ValueError("目录坐标无效")
            for ref, kind in ((item.station_id,"station"),(item.airport_id,"airport")):
                if ref is not None and (item.kind!="city" or ref not in self.entities or self.entities[ref].kind!=kind):
                    raise ValueError("城市交通端点须关联已登记的车站或机场")
            for link in item.links:
                parsed = urlsplit(link["url"])
                if parsed.scheme != "https" or parsed.hostname not in item.authority_hosts or parsed.username or parsed.password:
                    raise ValueError("官方链接与已登记权威域不匹配")

    def get(self, entity_id):
        return self.entities.get(entity_id)

    def resolve(self, text, *, kind=None):
        item = self.get(text)
        if item and (kind is None or item.kind == kind):
            return [item]
        key = text.strip().casefold()
        return [item for item in self.entities.values() if (kind is None or item.kind == kind)
                and key in {name.casefold() for name in (item.label,*item.aliases)}]


def public_catalog():
    filename = Path(__file__).resolve().parent / "data/travel_catalog_v1.json"
    return Catalog(json.loads(filename.read_text(encoding="utf-8"))["entities"])

