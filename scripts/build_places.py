"""Build the local OSM place index from an Overpass JSON export. No network.

  PYTHONPATH=backend .venv/bin/python scripts/build_places.py data/places/overpass-<ts>.json

Also accepts the M0 probe responses, which are Overpass JSON with `out center tags`:

  PYTHONPATH=backend .venv/bin/python scripts/build_places.py \
      data/probes/20260914T041841Z/{harbour,glebe,surry_hills}.response --timestamp 2026-09-14T04:18:41Z
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sidequest.places import DEFAULT_INDEX, build_index

# The Request coordinate clamp (models.Request): south, west, north, east.
SYDNEY_BBOX = (-34.2, 150.8, -33.5, 151.5)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("inputs", nargs="+", type=Path)
    p.add_argument("--out", type=Path, default=DEFAULT_INDEX)
    p.add_argument("--timestamp", help="OSM data time (ISO 8601); defaults to osm3s timestamp")
    args = p.parse_args()

    elements, digests, stamps = [], [], []
    for path in args.inputs:
        if path.name == "manifest.json":
            continue
        raw = path.read_bytes()
        data = json.loads(raw)
        elements += data.get("elements", [])
        digests.append(f"{path.name}:{hashlib.sha256(raw).hexdigest()}")
        if stamp := (data.get("osm3s") or {}).get("timestamp_osm_base"):
            stamps.append(stamp)
    timestamp = args.timestamp or (min(stamps) if stamps else None)
    if not timestamp:
        raise SystemExit("无法确定 OSM 数据时间：请用 --timestamp 指定")
    when = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    count = build_index(
        elements,
        args.out,
        {
            "source": "overpass-json",
            "source_files": " ".join(digests),
            "source_timestamp": when.isoformat(),
            "built_at": datetime.now(timezone.utc).isoformat(),
            "bbox": ",".join(map(str, SYDNEY_BBOX)),
        },
        SYDNEY_BBOX,
    )
    print(f"{args.out}: {count} places from {len(elements)} elements (OSM data {when:%Y-%m-%d %H:%M}Z)")


if __name__ == "__main__":
    main()
