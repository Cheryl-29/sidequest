"""Download an Overpass JSON snapshot of side-quest place kinds in the Sydney box.

A batch job, not part of any request path: run it by hand (weekly is plenty), then feed
the files to scripts/build_places.py. One small query per base tag, sequential with a
pause: a single query over the whole box timed out (HTTP 504) on the public instance.

The public instance has no SLA and fails intermittently, so two courtesies instead of
hammering it: each tag gets at most one retry after RETRY_SECONDS on 429/502/503/504 or a
transport error, and a rerun resumes the newest incomplete snapshot, skipping tags that
already landed. The endpoint and queries are fixed constants built from
sidequest.places.KINDS; nothing user-supplied goes in. A snapshot is only complete when
manifest.json exists.

  PYTHONPATH=backend .venv/bin/python scripts/fetch_places.py            # start or resume
  PYTHONPATH=backend .venv/bin/python scripts/fetch_places.py --fresh    # ignore partial runs
  PYTHONPATH=backend .venv/bin/python scripts/build_places.py data/places/overpass-<ts>/*.json
"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from build_places import SYDNEY_BBOX
from sidequest.places import KINDS

ROOT = Path(__file__).resolve().parents[1]
PLACES = ROOT / "data/places"
OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
PAUSE_SECONDS = 5
RETRY_SECONDS = 30
RETRYABLE = {429, 502, 503, 504}

# One query per base tag. Kinds that refine a base tag (bubble tea is a cafe with a
# cuisine) arrive inside that query and are told apart when the index is built.
BASE_TAGS = tuple(dict.fromkeys(k.osm[0] for k in KINDS))


def query(tag: tuple[str, str]) -> str:
    box = ",".join(map(str, SYDNEY_BBOX))
    # `natural` is dominated by street-tree nodes, and scanning them across the box timed
    # out every time. Beaches are mapped as areas, so ways and relations are enough.
    types = "wr" if tag[0] == "natural" else "nwr"
    return f'[out:json][timeout:180];{types}["{tag[0]}"="{tag[1]}"]["name"]({box});out center tags;'


def record(name: str, raw: bytes) -> dict | None:
    """A usable response, or None. A remark means Overpass gave up part-way."""
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if "elements" not in body or body.get("remark"):
        return None
    return {
        "tag": name,
        "file": f"{name}.json",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "elements": len(body["elements"]),
        "osm_timestamp": (body.get("osm3s") or {}).get("timestamp_osm_base"),
    }


def snapshot_folder(fresh: bool) -> Path:
    if not fresh:
        partial = sorted(p for p in PLACES.glob("overpass-*")
                         if p.is_dir() and not (p / "manifest.json").exists())
        if partial:
            return partial[-1]
    folder = PLACES / f"overpass-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def fetch(client: httpx.Client, name: str, tag: tuple[str, str]) -> tuple[bytes | None, str]:
    """Two tries at most. Returns (body, "") or (None, why it failed)."""
    why = ""
    for attempt in range(2):
        if attempt:
            print(f"{name:22} {why}，{RETRY_SECONDS} 秒后重试一次")
            time.sleep(RETRY_SECONDS)
        try:
            response = client.post(OVERPASS_ENDPOINT, data={"data": query(tag)})
        except httpx.TransportError as exc:
            why = f"网络错误 {type(exc).__name__}"
            continue
        if response.is_success:
            if record(name, response.content) is None:
                return None, "响应不完整（含 remark 或无法解析）"
            return response.content, ""
        why = f"HTTP {response.status_code}"
        if response.status_code not in RETRYABLE:
            break
    return None, why


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="不续抓，新建快照目录")
    args = parser.parse_args()

    folder = snapshot_folder(args.fresh)
    print(f"快照目录：{folder.relative_to(ROOT)}")
    records, failed, requested = [], [], False
    with httpx.Client(timeout=240, headers={"User-Agent": "Sidequest/0.1 (place index batch)"}) as c:
        for tag in BASE_TAGS:
            name = f"{tag[0]}-{tag[1]}"
            path = folder / f"{name}.json"
            if path.exists() and (done := record(name, path.read_bytes())):
                records.append(done)
                print(f"{name:22} {done['elements']:6} elements  （已存在，跳过）")
                continue
            if requested:
                time.sleep(PAUSE_SECONDS)
            requested = True
            raw, why = fetch(c, name, tag)
            if raw is None:
                # Keep going: one stubborn tag must not block every tag queued behind it.
                failed.append(f"{name}（{why}）")
                print(f"{name:22} 失败：{why}，跳过，下次续抓")
                continue
            tmp = path.with_suffix(".part")
            tmp.write_bytes(raw)
            tmp.replace(path)  # a half-written file never looks like a finished tag
            records.append(record(name, raw))
            print(f"{name:22} {records[-1]['elements']:6} elements  {len(raw):>9} bytes")
    if failed:
        raise SystemExit(f"快照不完整（{folder.relative_to(ROOT)} 无 manifest），"
                         f"已完成 {len(records)}/{len(BASE_TAGS)}；失败：{'、'.join(failed)}；"
                         "稍后重跑即可续抓")
    manifest = {"endpoint": OVERPASS_ENDPOINT, "bbox": SYDNEY_BBOX,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "attribution": "© OpenStreetMap contributors, ODbL", "files": records}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"完成：{folder.relative_to(ROOT)}，共 {sum(r['elements'] for r in records)} 个元素")


if __name__ == "__main__":
    main()
