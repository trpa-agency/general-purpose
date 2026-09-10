"""Read-only diagnosis of the 2022 DEM: quantization vs nearest-neighbor artifacts, in a small window."""
import numpy as np
import arcpy

SDE = r"F:\GIS\DB_CONNECT\Raster.sde"
# (raster, window name, lower-left x, y in the raster's own CRS, window size in m)
CASES = [
    ("2022", SDE + r"\SDE.DEM_BareEarth_LiDAR_2022", "kings_beach_flat", 756400, 4347500, 300),
    ("2010", SDE + r"\SDE.DEM_BareEarth_LiDAR_2010", "kings_beach_flat", 756400, 4347500, 300),
    ("2022", SDE + r"\SDE.DEM_BareEarth_LiDAR_2022", "kings_beach_slope", 756400, 4348100, 300),
    ("2010", SDE + r"\SDE.DEM_BareEarth_LiDAR_2010", "kings_beach_slope", 756400, 4348100, 300),
    ("green", SDE + r"\SDE.Nearshore_BareEarth_DEM", "kings_beach_nearshore", 756400, 4346600, 300),
    # Sonar is UTM 11N; deep flat basin floor near the lake centre, and the north shelf
    ("sonar", SDE + r"\SDE.DEM_USGS_DeepWaterBathyTopo", "deep_basin_floor", 233000, 4326000, 3000),
    ("sonar", SDE + r"\SDE.DEM_USGS_DeepWaterBathyTopo", "north_shelf", 231000, 4345000, 3000),
]


def analyze(name, path, x, y, size_m):
    r = arcpy.Raster(path)
    cw = r.meanCellWidth
    ncols = int(size_m / cw)
    a = arcpy.RasterToNumPyArray(r, arcpy.Point(x, y), ncols, ncols, nodata_to_value=np.nan).astype(float)
    v = a[np.isfinite(a)]
    if v.size == 0:
        print(f"{name}: all NoData in window");
        return
    # Quantization: how many distinct values, and what increment explains them
    uniq = np.unique(np.round(v, 5))
    diffs = np.diff(uniq)
    diffs = diffs[diffs > 1e-6]
    inc = np.min(diffs) if diffs.size else float("nan")
    # Fraction of values that are multiples of candidate increments (m and ft based)
    def share(step):
        return np.mean(np.abs(v / step - np.round(v / step)) < 1e-3)
    cands = {"0.001 m": 0.001, "0.01 m": 0.01, "0.1 m": 0.1, "0.01 ft": 0.003048, "0.1 ft": 0.03048, "1 ft": 0.3048}
    # Nearest-neighbor signature: share of horizontally / vertically identical neighbors
    same_h = np.nanmean(a[:, 1:] == a[:, :-1])
    same_v = np.nanmean(a[1:, :] == a[:-1, :])
    print(f"\n{name} @ ({x},{y}) cell {cw:.3f} m, {ncols}x{ncols}: valid {v.size}, range {v.min():.3f}-{v.max():.3f}")
    print(f"  distinct values: {uniq.size} of {v.size} ({100*uniq.size/v.size:.1f}%), smallest step between distinct values {inc:.5f} m")
    print("  share of values on a grid of: " + ", ".join(f"{k} {share(s)*100:.0f}%" for k, s in cands.items()))
    print(f"  identical to right neighbor {same_h*100:.1f}%, to lower neighbor {same_v*100:.1f}%")
    # Typical local relief so the reader can judge what a step means
    print(f"  median |dz| between neighbors: {np.nanmedian(np.abs(a[:, 1:] - a[:, :-1])):.4f} m")


for rname, path, wname, x, y, size in CASES:
    try:
        analyze(f"{rname} {wname}", path, x, y, size)
    except Exception as e:
        print(f"{rname} {wname}: ERROR {str(e)[:200]}")
