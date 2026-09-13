"""Fetch the USGS seamless 1 m tiles that patch the meridian wedges, then build their mosaic dataset.

Usage (arcgispro-py3, on the GIS server):
    python fetch_1m_patch.py                 # download missing tiles to server_paths.onem_tiles, verify sizes
    python fetch_1m_patch.py --build         # ...and then build the lidar_2022_1m mosaic dataset
    python fetch_1m_patch.py --dest D:\\somewhere --build

Safe to rerun: a tile already on disk whose size matches the size USGS reports is skipped;
a partial or wrong-sized file is downloaded again. Each transfer goes to a .part file and is
renamed only after its size verifies. The tile list is data/usgs_2022_1m_wedge_patch_urls.txt
(ten zone-10 tiles, x75-x76 / y431-y435, about 1.3 GB). Standard library only.
"""

import argparse
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
LIST = REPO / "data" / "usgs_2022_1m_wedge_patch_urls.txt"


def remote_size(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers.get("Content-Length", 0))


def fetch(url, dest, expected, attempts=3):
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, attempts + 1):
        try:
            t0 = time.time()
            with urllib.request.urlopen(url, timeout=120) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, length=8 * 1024 * 1024)
            got = part.stat().st_size
            if expected and got != expected:
                raise IOError(f"size {got:,} != expected {expected:,}")
            part.replace(dest)
            mb = got / 1e6
            print(f"  ok  {dest.name}  {mb:,.0f} MB in {time.time() - t0:,.0f} s")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  attempt {attempt} failed for {dest.name}: {e}")
            part.unlink(missing_ok=True)
            time.sleep(5 * attempt)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(REPO / "config.yaml"))
    ap.add_argument("--list", default=str(LIST), help="text file of tile URLs, one per line")
    ap.add_argument("--dest", help="folder for the tiles (default: server_paths.onem_tiles in config)")
    ap.add_argument("--build", action="store_true", help="after downloading, build the lidar_2022_1m mosaic dataset")
    ap.add_argument("--gdb", default=r"C:\GIS\lidar2022.gdb", help="file gdb for the mosaic dataset (with --build)")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    dest = Path(args.dest or cfg.get("server_paths", {}).get("onem_tiles") or "")
    if not str(dest):
        sys.exit("give --dest or set server_paths.onem_tiles in config.yaml")
    dest.mkdir(parents=True, exist_ok=True)
    urls = [u.strip() for u in Path(args.list).read_text(encoding="ascii").splitlines() if u.strip()]
    print(f"{len(urls)} tiles listed; destination {dest}")

    ok, skipped, failed = 0, 0, []
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        target = dest / name
        try:
            expected = remote_size(url)
        except Exception as e:  # noqa: BLE001
            print(f"  HEAD failed for {name}: {e}; downloading without size check")
            expected = 0
        if target.exists() and expected and target.stat().st_size == expected:
            print(f"  have {name}  {expected / 1e6:,.0f} MB")
            skipped += 1
            continue
        if target.exists():
            print(f"  {name} on disk but {target.stat().st_size:,} bytes vs {expected:,} expected; re-downloading")
        if fetch(url, target, expected):
            ok += 1
        else:
            failed.append(name)
    print(f"\ndownloaded {ok}, already present {skipped}, failed {len(failed)}")
    if failed:
        sys.exit("failed: " + ", ".join(failed) + "\nrerun to retry; nothing else was touched")

    if args.build:
        sys.path.insert(0, str(REPO / "scripts"))
        from build_2022_source import build_onem  # noqa: E402  (imports arcpy)
        build_onem(str(dest), args.gdb, force=False)


if __name__ == "__main__":
    main()
