"""
src/layers.py — read a source that is either a local file or a TRPA ArcGIS REST service.

    read_layer(source, cfg, where="1=1", fields="*")
        source: local path (.gpkg/.shp/.geojson) or a FeatureServer/MapServer layer URL
                e.g. https://maps.trpa.gov/server/rest/services/<Service>/FeatureServer/0
        Returns a GeoDataFrame in cfg["crs"]["working"]. Pages through maxRecordCount.

    read_raster(source, cfg, bbox=None)
        source: local GeoTIFF path or an ImageServer URL
                e.g. https://maps.trpa.gov/server/rest/services/<Image>/ImageServer
        Returns (array, transform, crs). For an ImageServer, exports the bbox at the
        LiDAR grid resolution in the working CRS and caches the GeoTIFF in data/raw/.

Fill config.yaml `sources:` with the service URLs Mason supplies; nothing else changes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import geopandas as gpd
import requests

from src.io import project_root


def _is_url(s: str) -> bool:
    return str(s).lower().startswith("http")


def read_layer(source: str, cfg: dict, where: str = "1=1", fields: str = "*", log=None) -> gpd.GeoDataFrame:
    wcrs = cfg["crs"]["working"]
    if not _is_url(source):
        gdf = gpd.read_file(project_root() / source)
        if log: log.info(f"Read {len(gdf):,} features from {source} (CRS {gdf.crs})")
        return gdf.to_crs(wcrs)

    epsg = int(str(wcrs).split(":")[-1])
    base = source.rstrip("/")
    meta = requests.get(base, params={"f": "json"}, timeout=60).json()
    page = int(meta.get("maxRecordCount", 1000))
    frames, offset = [], 0
    while True:
        params = {"where": where, "outFields": fields, "outSR": epsg, "f": "geojson",
                  "resultOffset": offset, "resultRecordCount": page, "returnGeometry": "true"}
        r = requests.get(f"{base}/query", params=params, timeout=300)
        r.raise_for_status()
        gj = r.json()
        feats = gj.get("features", [])
        if not feats:
            break
        frames.append(gpd.GeoDataFrame.from_features(feats, crs=f"EPSG:{epsg}"))
        offset += len(feats)
        if not gj.get("properties", {}).get("exceededTransferLimit", len(feats) == page):
            break
    gdf = gpd.GeoDataFrame(gpd.pd.concat(frames, ignore_index=True), crs=f"EPSG:{epsg}") if frames else gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{epsg}")
    if log: log.info(f"Read {len(gdf):,} features from {meta.get('name', base)}")
    return gdf


def read_raster(source: str, cfg: dict, bbox: tuple | None = None, log=None):
    import rasterio
    wcrs = cfg["crs"]["working"]
    if not _is_url(source):
        with rasterio.open(project_root() / source) as r:
            if log: log.info(f"Read raster {source}: {r.shape}, res {r.res}, CRS {r.crs}")
            return r.read(1), r.transform, r.crs

    epsg = int(str(wcrs).split(":")[-1])
    g = cfg["crs"]["lidar_grid_m"]
    if bbox is None:
        raise ValueError("bbox (xmin, ymin, xmax, ymax) in working CRS is required for an ImageServer source")
    xmin, ymin, xmax, ymax = bbox
    size = f"{int((xmax - xmin) / g)},{int((ymax - ymin) / g)}"
    cache = project_root() / cfg["paths"]["raw"] / ("imgsvc_" + hashlib.md5(f"{source}{bbox}{g}".encode()).hexdigest()[:10] + ".tif")
    if not cache.exists():
        params = {"bbox": ",".join(map(str, bbox)), "bboxSR": epsg, "imageSR": epsg, "size": size,
                  "format": "tiff", "pixelType": "F32", "interpolation": "RSP_BilinearInterpolation", "f": "image"}
        r = requests.get(f"{source.rstrip('/')}/exportImage", params=params, timeout=600)
        r.raise_for_status()
        cache.write_bytes(r.content)
        if log: log.info(f"Exported {source} to {cache.name} ({size} px at {g} m)")
    with rasterio.open(cache) as r:
        return r.read(1), r.transform, r.crs
