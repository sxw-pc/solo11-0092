"""Inspect checked local GeoJSON and lake shipment bytes without zone assignment."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from .manifest import load_manifest
from .util import sha256_bytes


def inspect_geofence(contract_path: str | Path) -> dict[str, object]:
    contract_file=Path(contract_path).resolve()
    root=contract_file.parents[2]
    contract=json.loads(contract_file.read_text(encoding="utf-8"))
    if contract.get("schema_version")!=1 or contract.get("contracts")!=["geodesic_edges","rhumb_edges"]:
        raise ValueError("unknown geofence contract")
    manifest_file=(root/contract["shipments_manifest"]).resolve()
    zones_file=(root/contract["zones"]).resolve()
    manifest_file.relative_to(root);zones_file.relative_to(root)
    manifest=load_manifest(manifest_file,strict=True)
    if manifest.table!="shipments" or len(manifest.files)!=1:
        raise ValueError("unexpected shipment manifest")
    entry=manifest.files[0]
    data_file=(manifest_file.parent/entry.path).resolve()
    data_file.relative_to(root)
    data=data_file.read_bytes()
    if len(data)!=entry.size or sha256_bytes(data)!=entry.sha256:
        raise ValueError("shipment bytes differ from manifest")
    rows=list(csv.DictReader(data.decode("utf-8").splitlines()))
    if len(rows)!=entry.row_count or len({r["shipment_id"] for r in rows})!=len(rows):
        raise ValueError("invalid shipment identity or row count")
    shipments=[]
    for row in rows:
        lon=float(row["longitude"]);lat=float(row["latitude"])
        if not -180<=lon<=180 or not -90<=lat<=90:
            raise ValueError("invalid lon/lat")
        shipments.append({"id":row["shipment_id"],"longitude":lon,"latitude":lat})
    zones=json.loads(zones_file.read_text(encoding="utf-8"))
    if zones.get("type")!="FeatureCollection" or len(zones.get("features",[]))!=2:
        raise ValueError("unexpected zone geometry")
    zone_rows=[]
    for feature in zones["features"]:
        geom=feature["geometry"]
        if geom.get("type")!="Polygon":
            raise ValueError("only local polygon fixtures supported")
        rings=geom["coordinates"]
        if any(len(ring)<4 or ring[0]!=ring[-1] for ring in rings):
            raise ValueError("polygon ring is not closed")
        zone_rows.append({"zone_id":feature["properties"]["zone_id"],"rings":rings})
    def midpoint_latitude(lat: float,half_delta: float) -> float:
        return math.degrees(math.atan(math.tan(math.radians(lat))/math.cos(math.radians(half_delta))))
    return {"snapshot_id":manifest.snapshot_id,"manifest_sha256":sha256_bytes(manifest_file.read_bytes()),"zones_sha256":sha256_bytes(zones_file.read_bytes()),"contracts":contract["contracts"],"boundary_rule":contract["boundary_rule"],"shipments":shipments,"zones":zone_rows,"polar_geodesic_midpoint_witness":{"bottom_lat":midpoint_latitude(60,60),"top_lat":midpoint_latitude(80,60)},"zone_memberships":None}
