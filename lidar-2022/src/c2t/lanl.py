"""
c2t/lanl.py: cloud2trees_to_lanl_trees(), the LANL TREES export of cloud2trees.

Ported from cloud2trees v0.8.3 R/cloud2trees_to_lanl_trees.R and R/utils_lanl_trees.R (plus the
helpers they call: check_spatial_points(), as_character_safe(), check_biomass_method(),
check_df_cols_all_missing(), search_dir_final_detected(), read_trees_flist(),
adjust_raster_resolution()). TREES (github.com/lanl/Trees) turns a tree list into the 3D fuel arrays
read by QUIC-Fire and FIRETEC.

What the export writes, all inside <output_dir>/lanl_trees_delivery (the folder is created, or
emptied of files if it exists, exactly as R does):

  Lidar_Bounds.geojson      the QUIC-Fire domain rectangle in EPSG:4326 (one feature, no attributes)
  Cloud2Trees_TreeList.txt  the TREES tree list: space separated, no header, one tree per line
  fuellist                  the TREES namelist, the package template with 19 lines substituted
  dtm_Clipped.tif           the DTM on the domain grid (only when a DTM is given)
  topo.dat                  FORTRAN unformatted topography file (only when a DTM is given and it has
                            no missing cells inside the domain)

Domain (R quicfire_define_domain, horizontal resolution fixed at 2 m):
  take the bounding box of the AOI; if width is not a multiple of 2 m set
  xmax = 2*round(xmax/2), xmin = 2*round(xmin/2) - 2, likewise for y; nx = width/2, ny = length/2.
  round() is half to even in both R and Python. Note the R rule always moves xmin and ymin outward
  but can move xmax and ymax inward by up to 1 m, so a tree on the east or north edge can end up
  with x_coord slightly larger than ndatax. Kept as in R.

Tree list text file (R make_lanl_trees_input), columns in order, no header:
  1 sp                        species code, always 1 (R hard codes one species; see assumptions)
  2 x_coord                   tree_x - domain xmin (m, relative to the domain SW corner)
  3 y_coord                   tree_y - domain ymin (m)
  4 tree_height_m             tree height (m)
  5 tree_cbh_m                crown base height (m)
  6 crown_dia_m               crown diameter (m)
  7 max_crown_diam_height_m   height of the maximum crown diameter (m)
  8 landfire_tree_kg_per_m3   or cruz_tree_kg_per_m3: crown bulk density (kg/m3), by cbd_method
  9 moist                     fuel moisture fraction, always 1
 10 ss                        fuel size scale (m), always 0.0005
  Every value is rounded to 4 decimals and printed the way R's format(scientific=FALSE) prints a
  numeric vector: fixed notation, at most 7 significant digits, and the same number of decimals for
  every row of a column. Rows with any missing value are dropped; if that drops more than 10 percent
  of the trees the export stops.

fuellist (R lines 5..79, 1-based): nx, ny, nz = ceiling(max tree height) + 1, dx = dy = 2,
  topofile (flat, or the quoted path of topo.dat), treefile (quoted path), ndatax = domain width (m),
  ndatay = domain length (m), ilitter, lrho, lmoisture, lss, ldepth, igrass, grho, gmoisture, gss,
  gdepth. Everything else is left as in the template.

topo.dat: one FORTRAN unformatted sequential record: a 4-byte little-endian int32 record length
  (nx*ny*4), then nx*ny little-endian float32 elevations, then the same 4-byte marker again. Values
  are ordered south row first, west to east within a row, which is zs(i, j) column-major for
  real(4) zs(nx, ny) in TREES. R flips the raster vertically and writes it with writeBin(size = 4)
  in native (little) endian.

Ported faithfully:
  * argument checks and R error messages for study_boundary, topofile, cbd_method, fuel_litter,
    fuel_grass, output_dir, the tree list columns (treeID present and unique; tree_x, tree_y,
    tree_height_m, tree_cbh_m, crown_dia_m, max_crown_diam_height_m and the CBD column present and
    not all missing) and the fuellist template layout (quicfire_check_fuellist)
  * AOI handling: reproject the boundary to the tree list CRS, buffer only when buffer > 0, then
    bounding box when bbox_aoi; trees kept when their top intersects the AOI
  * the domain rounding, Lidar_Bounds.geojson, the tree list columns, units, coordinate shift,
    rounding and R text formatting, the 10 percent drop rule, nz, and every fuellist substitution
  * the per-species parameters sp = 1, moist = 1, ss = 0.0005 for every tree
  * writing dtm_Clipped.tif and topo.dat whenever a DTM is available, not only for topofile = "dtm",
    and the "missing values in DTM bounding box extent" warning that suppresses topo.dat

Assumptions and deviations (read before comparing with R output):
  * The R function takes an input_dir and reads final_detected_tree_tops*.gpkg (or crowns) and
    dtm_*.tif from it. Here tree_list is a GeoDataFrame, a spatial file, or that directory; the DTM
    is dtm_path (a GeoTIFF), defaulting to the first dtm_*.tif when tree_list is a directory.
  * study_boundary is required in R (the NA default errors). Here None means the bounding box of
    the tree tops in the tree list CRS, which is what bbox_aoi = True produces from any polygon
    that just encloses the trees.
  * There is no forest type to TREES species mapping in cloud2trees 0.8.3: sp is 1 for every tree
    and the R comment says species codes are "eventually". dbh and forest type are not required by
    the R export either (only the roxygen text mentions dbh); the port checks the same columns the
    R code checks.
  * topo.dat record markers: R writes the literal bytes "BRUH" as the leading marker and no
    trailing marker (a placeholder that gfortran tolerates because it never validates the length).
    The port writes the real record length before and after the data, which every FORTRAN
    sequential reader accepts. Data bytes are identical to R.
  * The DTM is resampled straight onto the domain grid (ny rows by nx columns of 2 m from the
    domain SW corner, in the tree list CRS) with average resampling when the DTM is an integer
    factor finer than 2 m, nearest when it is an integer factor coarser, bilinear otherwise. R
    aggregates by an integer factor from the DTM's own origin, resamples if that is not exactly
    2 m, reprojects the domain to the DTM CRS and crops with terra, which can leave dtm_Clipped
    offset from the domain by a fraction of a cell and does not guarantee nx by ny cells. TREES
    needs exactly nx*ny values, so the port guarantees the shape.
  * Paths written into the fuellist use the OS native separator. On Linux and macOS R replaced
    "/" with "\\" (a no-op on Windows after normalizePath), which breaks the path on those systems.
  * Text files are written with "\\n" line endings (R on Windows would write "\\r\\n").
  * The fuel lists accept a dict (names checked like R's named list), or a 5-element sequence
    taken in order like R's unnamed list or numeric vector.
  * Argument names follow R: bbox_aoi and buffer (metres in the tree list CRS), not bbox_buffer_m.
"""
from __future__ import annotations

import logging
import math
import os
import re
import tempfile
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from .schema import as_points

HORIZONTAL_RES = 2.0  # "potential future parameters" in R: horizontal_res <- 2
FUELLIST_TEMPLATE = Path(__file__).with_name("fuellist")  # copy of inst/extdata/fuellist
DELIVERY_DIR = "lanl_trees_delivery"

# R defaults for fuel_litter and fuel_grass, in the order TREES expects them.
DEFAULT_FUEL_LITTER = {"ilitter": 0, "lrho": 4.667, "lmoisture": 0.06, "lss": 0.0005, "ldepth": 0.06}
DEFAULT_FUEL_GRASS = {"igrass": 0, "grho": 1.17, "gmoisture": 0.06, "gss": 0.0005, "gdepth": 0.27}
FUEL_NAMES = {
    "litter": ("ilitter", "lrho", "lmoisture", "lss", "ldepth"),
    "grass": ("igrass", "grho", "gmoisture", "gss", "gdepth"),
}

# R fuel_trees tibble: one species for every tree, moisture 100 percent, pine size scale.
FUEL_TREES = {"sp": 1, "moist": 1, "ss": 0.0005}

CBD_COLUMN = {"landfire": "landfire_tree_kg_per_m3", "cruz": "cruz_tree_kg_per_m3"}

# (parameter, 1-based line number) of the template as checked by R quicfire_check_fuellist()
FUELLIST_PARAMETERS = (
    ("nx", 5), ("ny", 6), ("nz", 7), ("dx", 8), ("dy", 9), ("dz", 10), ("aa1", 11), ("singlefuel", 12),
    ("lreduced", 13), ("topofile", 14),
    ("ifuelin", 18), ("inx", 19), ("iny", 20), ("inz", 21), ("idx", 22), ("idy", 23), ("idz", 24),
    ("iaa1", 25), ("infuel", 26), ("intopofile", 27), ("rhoffile", 28), ("ssfile", 29), ("moistfile", 30),
    ("afdfile", 31),
    ("itrees", 35), ("tfuelbins", 36), ("treefile", 37), ("ndatax", 38), ("ndatay", 39), ("datalocx", 40),
    ("datalocy", 41),
    ("ilitter", 45), ("litterconstant", 47), ("lrho", 48), ("lmoisture", 49), ("lss", 50), ("ldepth", 51),
    ("windprofile", 54), ("YearsSinceBurn", 55), ("StepsPerYear", 56), ("relhum", 57), ("grassstep", 58),
    ("iFIA", 59), ("FIA", 60), ("randomwinds", 61), ("litout", 63), ("gmoistoverride", 64), ("uavg", 65),
    ("vavg", 66), ("ustd", 67), ("vstd", 68),
    ("igrass", 72), ("ngrass", 74), ("grassconstant", 75), ("grho", 76), ("gmoisture", 77), ("gss", 78),
    ("gdepth", 79),
    ("verbose", 83),
)


# ----------------------------------------------------------------------------------------------
# small helpers shared with the rest of the R package
# ----------------------------------------------------------------------------------------------
def _squish(s) -> str:
    """dplyr::coalesce(s, "") then tolower() then stringr::str_squish()."""
    s = "" if s is None else str(s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _find_methods(value, options: tuple[str, ...]) -> list[str]:
    """stringr::str_extract_all(value, "a|b") then unique(): matches in order of appearance."""
    found = re.findall("|".join(options), _squish(value))
    return list(dict.fromkeys(found))


def as_character_safe(x, digits: int = 7) -> np.ndarray:
    """
    R as_character_safe(): format(x, scientific = FALSE, trim = TRUE) on a numeric vector.

    Fixed notation with at most `digits` significant digits per value, and one common number of
    decimals for the whole vector (the most any value needs after trailing zeros are dropped).
    Missing values (NaN, None) become None. Strings pass through unchanged.
    """
    arr = np.asarray(x)
    if arr.dtype.kind in "USO" and all(isinstance(v, str) or v is None for v in arr.ravel()):
        return arr.astype(object)  # R: character input is returned as is
    v = np.asarray(pd.to_numeric(pd.Series(arr.ravel()), errors="coerce"), dtype=float)
    decimals = 0
    for val in v[np.isfinite(v)]:
        if val == 0:
            continue
        exponent = int(math.floor(math.log10(abs(val))))
        d = max(0, digits - 1 - exponent)
        text = f"{val:.{d}f}".rstrip("0")
        decimals = max(decimals, len(text.split(".")[1]) if "." in text else 0)
    out = np.array([f"{val:.{decimals}f}" if np.isfinite(val) else None for val in v], dtype=object)
    return out


def _fmt(x) -> str:
    """One scalar the way R paste0()/as_character_safe() prints it."""
    return str(as_character_safe([float(x)])[0])


def _outdir(outdir) -> Path:
    """R's `outdir = tempdir()` default: None means a fresh temporary directory."""
    outdir = Path(tempfile.mkdtemp()) if outdir is None else Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    return outdir


# ----------------------------------------------------------------------------------------------
# argument checks (R: check_fuel_list, check_biomass_method, topofile block)
# ----------------------------------------------------------------------------------------------
def check_fuel_list(fuel_list, ftype: str = "grass") -> dict:
    """R check_fuel_list(): return a dict with the five TREES litter or grass parameters in order."""
    found = _find_methods(ftype, ("litter", "grass"))
    if not found:
        raise ValueError("`ftype` parameter must be one of:\n    litter, grass")
    ftype = found[0]
    fnames = FUEL_NAMES[ftype]
    expected_len = 5
    if fuel_list is None:
        fuel_list = DEFAULT_FUEL_LITTER if ftype == "litter" else DEFAULT_FUEL_GRASS
    try:
        n = len(fuel_list)
    except TypeError:
        n = 0
    if n != expected_len:
        raise ValueError(f"incorrect list length in {ftype} fuel list. expecting: {expected_len}")
    if isinstance(fuel_list, dict):
        ndiff = [k for k in fnames if k not in fuel_list]
        if ndiff:
            raise ValueError(f"incorrect list names in {ftype} fuel list. need to add/rename:\n   " + ", ".join(ndiff))
        return {k: float(fuel_list[k]) for k in fnames}
    if isinstance(fuel_list, (list, tuple, np.ndarray, pd.Series)):
        return {k: float(v) for k, v in zip(fnames, list(fuel_list))}
    raise ValueError(f"incorrect list structure in {ftype} fuel list")


def check_biomass_method(method) -> list[str]:
    """R check_biomass_method(): the biomass methods named in `method`, in order of appearance."""
    found = _find_methods(method, ("cruz", "landfire"))
    if not found:
        raise ValueError("`method` parameter must be one or multiple of:\n    cruz, landfire")
    return found


def check_topofile(topofile) -> str:
    """The topofile block of cloud2trees_to_lanl_trees(): 'flat' or 'dtm'."""
    found = _find_methods(topofile, ("flat", "dtm"))
    if not found:
        raise ValueError("`topofile` parameter must be one of:\n    flat, dtm")
    return found[0]


def check_df_cols_all_missing(df, col_names, all_numeric: bool = True, check_vals_missing: bool = True) -> bool:
    """R check_df_cols_all_missing(): every column must exist and not be entirely missing."""
    if not isinstance(df, pd.DataFrame):
        raise ValueError("`df` must be a data.frame")
    missing = [c for c in col_names if c not in df.columns]
    if missing:
        raise ValueError("the data does not contain the columns: " + ", ".join(missing) + "\n this data must exist")
    if not check_vals_missing:
        return True
    all_missing = []
    for c in col_names:
        col = df[c]
        if all_numeric:
            col = pd.to_numeric(col, errors="coerce")
        if int(col.isna().sum()) == len(df):
            all_missing.append(c)
    if all_missing:
        raise ValueError("the columns listed below have all missing data:\n   " + ", ".join(all_missing))
    return True


def check_spatial_points(tree_list, crs=None) -> gpd.GeoDataFrame:
    """
    R check_spatial_points(): treeID present and unique, then a Point GeoDataFrame of tree tops with
    tree_x, tree_y (taken from the geometry when the input is already points).
    """
    if "treeID" not in tree_list.columns:
        raise ValueError("`tree_list` data must contain `treeID` column.\nProvide the `treeID` as a unique identifier of individual trees.")
    if len(tree_list) != tree_list["treeID"].nunique(dropna=False):
        raise ValueError("Duplicates found in the treeID column. Please remove duplicates and try again.")
    if not isinstance(tree_list, gpd.GeoDataFrame):
        if not isinstance(tree_list, pd.DataFrame):
            raise ValueError("must pass a data.frame or sf object to the `tree_list` parameter")
        if crs is None:
            raise ValueError("must provide the EPSG code in `crs` parameter for the projection of x,y data")
        if "tree_x" not in tree_list.columns or "tree_y" not in tree_list.columns:
            raise ValueError("must provide the columns `tree_x` and `tree_y`")
    pts = as_points(tree_list, crs=crs)
    if isinstance(tree_list, gpd.GeoDataFrame) and len(tree_list) and tree_list.geom_type.isin(["Point"]).all():
        pts["tree_x"] = pts.geometry.x.values
        pts["tree_y"] = pts.geometry.y.values
    pts["treeID"] = pts["treeID"].astype(str)
    return pts


# ----------------------------------------------------------------------------------------------
# reading a cloud2trees delivery directory (R: search_dir_final_detected, read_trees_flist)
# ----------------------------------------------------------------------------------------------
def search_dir_final_detected(directory) -> dict:
    """R search_dir_final_detected(): the crowns, tree tops, DTM and CHM files in a delivery folder."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError("could not locate the directory:\n   " + str(directory.resolve()))
    names = sorted(os.listdir(directory))

    def pick(pattern):
        hits = [str((directory / n).resolve()) for n in names if re.search(pattern, n)]
        return hits or None

    return {
        "crowns_flist": pick(r"final_detected_crowns.*\.gpkg$"),
        "ttops_flist": pick(r"final_detected_tree_tops.*\.gpkg$"),
        "dtm_flist": pick(r"dtm_.*\.tif$"),
        "chm_flist": pick(r"chm_.*\.tif$"),
    }


def read_trees_flist(flist, which_trees: str = "tops") -> gpd.GeoDataFrame:
    """R read_trees_flist(): read one file, a list of files, or a delivery directory (tops preferred)."""
    found = _find_methods(which_trees, ("crowns", "tops"))
    if not found:
        raise ValueError("`which_trees` parameter must be one of:\n    crowns, tops")
    which_trees = found[0]
    msg = ("If attempting to pass a list of files, the file list must:"
           "\n   * be a vector of class character -AND-"
           "\n   * be a directory that has final_detected_tree_tops* or final_detected_crowns* files from cloud2trees::cloud2trees() or cloud2trees::raster2trees()"
           "\n   * -OR- be a vector of class character that includes spatial files that can be read by sf::st_read()")
    if isinstance(flist, (str, Path)):
        flist = [flist]
    flist = list(dict.fromkeys(str(Path(f).resolve()) for f in flist))
    if len(flist) == 1 and Path(flist[0]).is_dir():
        found_files = search_dir_final_detected(flist[0])
        crowns, ttops = found_files["crowns_flist"], found_files["ttops_flist"]
        if crowns is None and ttops is None:
            raise ValueError(msg)
        if ttops is not None and which_trees == "tops":
            flist = ttops
        elif crowns is not None and which_trees == "crowns":
            flist = crowns
        elif ttops is not None:
            flist = ttops
        else:
            flist = crowns
    parts = []
    for f in flist:
        g = gpd.read_file(f)
        g["source_filename"] = f
        parts.append(g)
    trees = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    if len(trees) == 0:
        raise ValueError(msg)
    return trees


# ----------------------------------------------------------------------------------------------
# AOI (R: get_custom_aoi, clip_tree_list_aoi)
# ----------------------------------------------------------------------------------------------
def _as_boundary(study_boundary, default_crs=None) -> gpd.GeoSeries:
    """Coerce a path, GeoDataFrame, GeoSeries or shapely geometry to a one-row GeoSeries."""
    if isinstance(study_boundary, (str, Path)):
        if not Path(study_boundary).exists():
            raise ValueError("study_boundary must be sf class object")
        study_boundary = gpd.read_file(study_boundary)
    if isinstance(study_boundary, gpd.GeoDataFrame):
        study_boundary = study_boundary.geometry
    elif isinstance(study_boundary, BaseGeometry):
        study_boundary = gpd.GeoSeries([study_boundary], crs=default_crs)
    if not isinstance(study_boundary, gpd.GeoSeries):
        raise ValueError("study_boundary must be sf class object")
    return study_boundary


def get_custom_aoi(study_boundary, bbox_aoi: bool = False, buffer: float = 0, reproject_epsg=None) -> gpd.GeoSeries:
    """R get_custom_aoi(): reproject, buffer (only when buffer > 0), then bounding box when bbox_aoi."""
    study_boundary = _as_boundary(study_boundary)
    if study_boundary.crs is None:
        raise ValueError("study_boundary does not have a CRS")
    if len(study_boundary) != 1:
        raise ValueError("study_boundary must only have a single record geometry")
    if not study_boundary.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("study_boundary must contain POLYGON type geometry only")
    if reproject_epsg is not None:
        study_boundary = study_boundary.to_crs(reproject_epsg)
    if isinstance(buffer, str):
        buffer = float(re.search(r"-?\d+\.?\d*", buffer).group(0))
    buffer = 0 if buffer is None or (isinstance(buffer, float) and np.isnan(buffer)) else buffer
    if buffer > 0:
        study_boundary = study_boundary.buffer(buffer)
    if bbox_aoi:
        study_boundary = gpd.GeoSeries([box(*study_boundary.total_bounds)], crs=study_boundary.crs)
    return study_boundary.reset_index(drop=True)


def clip_tree_list_aoi(tree_list, study_boundary, bbox_aoi: bool = False, buffer: float = 0, reproject_epsg=None, crs=None) -> dict:
    """R clip_tree_list_aoi(): keep trees whose top intersects the customised AOI. Returns tree_list and aoi."""
    if not isinstance(tree_list, gpd.GeoDataFrame):
        raise ValueError("tree_list must be sf class object")
    if tree_list.crs is None:
        raise ValueError("tree_list does not have a CRS")
    if reproject_epsg is None:
        reproject_epsg = tree_list.crs
    aoi = get_custom_aoi(study_boundary, bbox_aoi=bool(bbox_aoi), buffer=buffer, reproject_epsg=reproject_epsg)
    tree_tops = check_spatial_points(tree_list, crs).to_crs(aoi.crs)
    keep = tree_tops.intersects(aoi.iloc[0]).values
    clipped = tree_list.iloc[np.flatnonzero(keep)].copy()
    if len(clipped) == 0:
        warnings.warn("no tree_list trees found within study_boundary bounds")
    return {"tree_list": clipped, "aoi": aoi}


# ----------------------------------------------------------------------------------------------
# domain (R: quicfire_define_domain)
# ----------------------------------------------------------------------------------------------
def quicfire_define_domain(aoi: gpd.GeoSeries, horizontal_resolution: float = HORIZONTAL_RES, outdir=None, log=None) -> dict:
    """
    R quicfire_define_domain(): the domain rectangle, rounded so its width and length are multiples
    of the horizontal resolution, written as Lidar_Bounds.geojson in EPSG:4326.
    """
    log = log or logging.getLogger(__name__)
    if not isinstance(aoi, (gpd.GeoSeries, gpd.GeoDataFrame)):
        raise ValueError("sf_data must be sf class object")
    if aoi.crs is None:
        raise ValueError("sf_data does not have a CRS")
    hr = float(horizontal_resolution)
    if not np.isfinite(hr):
        raise ValueError("horizontal_resolution must be numeric")
    outdir = _outdir(outdir)

    xmin, ymin, xmax, ymax = (float(v) for v in aoi.total_bounds)
    width = xmax - xmin
    length = ymax - ymin
    if width % hr != 0:
        xmax = hr * round(xmax / hr)
        xmin = hr * round(xmin / hr) - hr
    if length % hr != 0:
        ymax = hr * round(ymax / hr)
        ymin = hr * round(ymin / hr) - hr
    width = xmax - xmin
    length = ymax - ymin
    nx = width / hr
    ny = length / hr

    geom = box(xmin, ymin, xmax, ymax)
    fp = outdir.resolve() / "Lidar_Bounds.geojson"
    if fp.exists():
        fp.unlink()
    gpd.GeoDataFrame(geometry=[geom], crs=aoi.crs).to_crs(4326).to_file(fp, driver="GeoJSON")
    log.info("exported QUIC-Fire domain to:\n ........ %s", fp)

    domain = {
        "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
        "nx": nx, "ny": ny, "width": width, "length": length,
        "dx": hr, "dy": hr, "geometry": geom, "crs": aoi.crs,
    }
    return {"quicfire_domain_df": domain, "domain_path": str(fp)}


# ----------------------------------------------------------------------------------------------
# topography (R: quicfire_dtm_topofile, adjust_raster_resolution)
# ----------------------------------------------------------------------------------------------
def write_topo_dat(path, dtm: np.ndarray) -> str:
    """
    Write a north-up (rows, cols) elevation array as the TREES / FIRETEC topo.dat: one FORTRAN
    unformatted sequential record of little-endian float32, south row first, west to east.
    """
    values = np.flipud(np.asarray(dtm, dtype=np.float32)).ravel(order="C").astype("<f4")
    marker = np.array([values.nbytes], dtype="<i4").tobytes()
    with open(path, "wb") as f:
        f.write(marker)
        f.write(values.tobytes())
        f.write(marker)
    return str(path)


def read_topo_dat(path, nx: int, ny: int) -> np.ndarray:
    """Read a topo.dat written by write_topo_dat() back to a north-up (ny, nx) float32 array."""
    raw = np.fromfile(path, dtype=np.uint8)
    n = int(nx) * int(ny)
    head = int(raw[:4].view("<i4")[0])
    tail = int(raw[-4:].view("<i4")[0])
    if head != n * 4 or tail != n * 4 or raw.size != n * 4 + 8:
        raise ValueError(f"{path} is not a single FORTRAN record of {n} float32 values")
    values = raw[4:4 + n * 4].view("<f4").reshape(int(ny), int(nx))
    return np.flipud(values).copy()


def _resampling_for(src_res: float, target: float) -> Resampling:
    """R adjust_raster_resolution(): aggregate by mean when finer, disaggregate (nearest) when coarser, bilinear otherwise."""
    tol = 1e-6
    if target > src_res:
        fact = target / src_res
        if fact >= 2 and abs(fact - round(fact)) < tol:
            return Resampling.average
        return Resampling.bilinear
    if target < src_res:
        fact = src_res / target
        if fact >= 2 and abs(fact - round(fact)) < tol:
            return Resampling.nearest
        return Resampling.bilinear
    return Resampling.nearest


def quicfire_dtm_topofile(dtm_rast, horizontal_resolution: float = HORIZONTAL_RES, study_boundary: dict | None = None, outdir=None, log=None) -> dict:
    """
    R quicfire_dtm_topofile(): put the DTM on the domain grid, write dtm_Clipped.tif, and write
    topo.dat unless the domain has missing DTM cells. `study_boundary` is the domain dict from
    quicfire_define_domain(). Returns dtm (north-up array), dtm_path and topofile_path (None when
    topo.dat could not be written).
    """
    log = log or logging.getLogger(__name__)
    if isinstance(dtm_rast, (list, tuple)):
        dtm_rast = [f for f in dtm_rast if re.search(r"\.(tif|tiff)$", str(f), re.I)]
        if not dtm_rast:
            raise ValueError("this is not a readabile dtm_rast")
        dtm_rast = dtm_rast[0]
    dtm_rast = Path(dtm_rast)
    if dtm_rast.is_dir():
        flist = search_dir_final_detected(dtm_rast)["dtm_flist"]
        if not flist:
            raise ValueError("no DTM raster file found at: \n .... " + str(dtm_rast.resolve()))
        dtm_rast = Path(flist[0])
    if not re.search(r"\.(tif|tiff)$", dtm_rast.name, re.I):
        raise ValueError("this is not a readabile dtm_rast")
    if study_boundary is None:
        raise ValueError("study_boundary (the domain from quicfire_define_domain()) is required")
    hr = float(horizontal_resolution)
    if not np.isfinite(hr):
        raise ValueError("horizontal_resolution must be numeric")
    outdir = _outdir(outdir)

    domain = study_boundary
    nx, ny = int(round(domain["nx"])), int(round(domain["ny"]))
    dst_transform = from_origin(domain["xmin"], domain["ymax"], hr, hr)
    dst_crs = domain["crs"]

    with rasterio.open(dtm_rast) as src:
        if src.crs is None:
            raise ValueError("the crs for dtm_rast is NA")
        band = np.ma.filled(src.read(1, masked=True).astype("float32"), np.nan)
        src_transform, src_crs, src_res = src.transform, src.crs, float(src.res[0])

    clipped = np.full((ny, nx), np.nan, dtype="float32")
    reproject(
        source=band, destination=clipped,
        src_transform=src_transform, src_crs=src_crs, src_nodata=np.nan,
        dst_transform=dst_transform, dst_crs=dst_crs, dst_nodata=np.nan,
        resampling=_resampling_for(src_res, hr),
    )

    dtm_path = outdir.resolve() / "dtm_Clipped.tif"
    profile = {"driver": "GTiff", "dtype": "float32", "count": 1, "height": ny, "width": nx,
               "crs": dst_crs, "transform": dst_transform, "nodata": np.nan, "compress": "deflate"}
    with rasterio.open(dtm_path, "w", **profile) as dst:
        dst.write(clipped, 1)

    if np.isnan(clipped).any():
        warnings.warn(
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            "missing values in DTM bounding box extent, could not write QUIC-Fire topo.dat file"
            "\n  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        )
        fp = None
    else:
        fp = write_topo_dat(outdir.resolve() / "topo.dat", clipped)
        log.info("exported QUIC-Fire topo.dat to:\n ........ %s", fp)

    return {"dtm": clipped, "dtm_path": str(dtm_path), "topofile_path": fp}


# ----------------------------------------------------------------------------------------------
# fuellist template (R: quicfire_get_fuellist, quicfire_check_fuellist)
# ----------------------------------------------------------------------------------------------
def quicfire_get_fuellist(template=None) -> list[str]:
    """R quicfire_get_fuellist(): the template lines (without line endings)."""
    template = FUELLIST_TEMPLATE if template is None else Path(template)
    return template.read_text(encoding="utf-8").splitlines()


def quicfire_check_fuellist(fuellist: list[str]) -> bool:
    """R quicfire_check_fuellist(): every expected parameter must sit on its expected line."""
    if not isinstance(fuellist, (list, tuple)) or not all(isinstance(l, str) for l in fuellist):
        raise ValueError("this fuellist isn't even character")
    present = set()
    for i, raw in enumerate(fuellist, start=1):
        line = re.sub(r"\s+", " ", raw).strip()
        if line == "" or line.startswith("!"):
            continue
        present.add((line.split("=")[0].strip(), i))
    comp = [f"{row}:{name}" for name, row in FUELLIST_PARAMETERS if (name, row) not in present]
    if comp:
        raise ValueError(
            "fuellist has missing or unordered parameters listed below (expected_row:parameter). see `quicfire_get_fuellist()`"
            "\n   " + ", ".join(comp)
        )
    return True


# ----------------------------------------------------------------------------------------------
# tree list and fuellist export (R: make_lanl_trees_input)
# ----------------------------------------------------------------------------------------------
def make_lanl_trees_input(
    tree_list,
    quicfire_domain_df: dict,
    topofile: str = "flat",
    cbd_col_name: str = "landfire_tree_kg_per_m3",
    horizontal_resolution: float = HORIZONTAL_RES,
    outdir=None,
    fuel_litter=None,
    fuel_grass=None,
    fuellist_template=None,
    log=None,
) -> dict:
    """
    R make_lanl_trees_input(): write Cloud2Trees_TreeList.txt and fuellist for TREES.
    `topofile` is "flat" or the path of a topo.dat file. Returns treelist (the formatted DataFrame),
    fuellist_path and treelist_path.
    """
    log = log or logging.getLogger(__name__)
    fuel_litter = check_fuel_list(fuel_litter, "litter")
    fuel_grass = check_fuel_list(fuel_grass, "grass")

    topofile = str(topofile)
    if topofile.lower().endswith(".dat"):
        topofile_text = "'" + str(Path(topofile).resolve()) + "'"
    elif topofile.lower() == "flat":
        topofile_text = "flat"
    else:
        raise ValueError("topofile must be either 'flat' or the path to a '.dat' file")

    outdir = _outdir(outdir)

    lines = quicfire_get_fuellist(fuellist_template)
    quicfire_check_fuellist(lines)

    domain_keys = ["xmin", "ymin", "nx", "ny", "width", "length"]
    domain_df = pd.DataFrame([{k: quicfire_domain_df.get(k, np.nan) for k in domain_keys}])
    check_df_cols_all_missing(domain_df, domain_keys, check_vals_missing=True)

    tree_list = check_spatial_points(tree_list)
    needed = ["tree_x", "tree_y", "tree_height_m", "tree_cbh_m", "crown_dia_m", "max_crown_diam_height_m", cbd_col_name]
    check_df_cols_all_missing(pd.DataFrame(tree_list.drop(columns="geometry")), needed, check_vals_missing=True, all_numeric=True)

    # set up data for return
    data = pd.DataFrame(tree_list.drop(columns="geometry"))
    data["sp"] = FUEL_TREES["sp"]
    data["moist"] = FUEL_TREES["moist"]
    data["ss"] = FUEL_TREES["ss"]
    # coordinates relative to the SW corner of the domain written to Lidar_Bounds.geojson
    data["x_coord"] = pd.to_numeric(data["tree_x"], errors="coerce") - float(quicfire_domain_df["xmin"])
    data["y_coord"] = pd.to_numeric(data["tree_y"], errors="coerce") - float(quicfire_domain_df["ymin"])
    columns = ["sp", "x_coord", "y_coord", "tree_height_m", "tree_cbh_m", "crown_dia_m", "max_crown_diam_height_m", cbd_col_name, "moist", "ss"]
    data = data[columns].copy()
    # round to 4 decimals and print like R, then drop rows with any missing value
    for c in columns:
        data[c] = as_character_safe(np.round(pd.to_numeric(data[c], errors="coerce").astype(float).values, 4))
    data = data.dropna().reset_index(drop=True)

    if len(data) < math.floor(len(tree_list) * 0.9):
        raise ValueError("more than 10% of data dropped due to missing values in tree attributes...fill in missing data first")

    # the tree list must be a text file without headers
    treefile_path = outdir.resolve() / "Cloud2Trees_TreeList.txt"
    with open(treefile_path, "w", encoding="utf-8", newline="\n") as f:
        for row in data.itertuples(index=False):
            f.write(" ".join(row) + "\n")
    log.info("exported Treelist for LANL TREES program to:\n ........ %s", treefile_path)
    treefile_path_q = "'" + str(treefile_path) + "'"

    # now the fuellist (R line numbers are 1-based)
    nz = math.ceil(max(float(h) for h in data["tree_height_m"])) + 1  # max tree height + 1 m
    lines[5 - 1] = "      nx  = " + _fmt(quicfire_domain_df["nx"])
    lines[6 - 1] = "      ny  = " + _fmt(quicfire_domain_df["ny"])
    lines[7 - 1] = "      nz  = " + _fmt(nz)
    lines[8 - 1] = "      dx  = " + _fmt(horizontal_resolution)
    lines[9 - 1] = "      dy  = " + _fmt(horizontal_resolution)
    lines[14 - 1] = "      topofile = " + topofile_text  # flat for QUIC-Fire, a topo.dat path for FIRETEC
    lines[37 - 1] = "      treefile = " + treefile_path_q
    lines[38 - 1] = "      ndatax = " + _fmt(quicfire_domain_df["width"])
    lines[39 - 1] = "      ndatay = " + _fmt(quicfire_domain_df["length"])
    # litter
    lines[45 - 1] = "      ilitter = " + _fmt(round(fuel_litter["ilitter"], 0))  # 0 = no litter, 1 = basic litter, 2 = DUET
    lines[48 - 1] = "      lrho = " + _fmt(round(fuel_litter["lrho"], 3))
    lines[49 - 1] = "      lmoisture = " + _fmt(round(fuel_litter["lmoisture"], 2))
    lines[50 - 1] = "      lss = " + _fmt(round(fuel_litter["lss"], 5))
    lines[51 - 1] = "      ldepth = " + _fmt(round(fuel_litter["ldepth"], 2))
    # grass
    lines[72 - 1] = "      igrass = " + _fmt(round(fuel_grass["igrass"], 0))
    lines[76 - 1] = "      grho = " + _fmt(round(fuel_grass["grho"], 3))
    lines[77 - 1] = "      gmoisture = " + _fmt(round(fuel_grass["gmoisture"], 2))
    lines[78 - 1] = "      gss = " + _fmt(round(fuel_grass["gss"], 5))
    lines[79 - 1] = "      gdepth = " + _fmt(round(fuel_grass["gdepth"], 2))

    fuellist_path = outdir.resolve() / "fuellist"
    with open(fuellist_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    log.info("exported TREES program fuellist to:\n ........ %s", fuellist_path)

    return {"treelist": data, "fuellist_path": str(fuellist_path), "treelist_path": str(treefile_path)}


# ----------------------------------------------------------------------------------------------
# the exported function
# ----------------------------------------------------------------------------------------------
def cloud2trees_to_lanl_trees(
    tree_list,
    output_dir,
    study_boundary=None,
    bbox_aoi: bool = True,
    buffer: float = 0,
    dtm_path=None,
    topofile: str = "flat",
    cbd_method: str = "landfire",
    fuel_litter=None,
    fuel_grass=None,
    fuellist_template=None,
    log=None,
) -> dict:
    """
    Format cloud2trees outputs as inputs for the LANL TREES program (QUIC-Fire, FIRETEC).

    tree_list       GeoDataFrame of tree tops (Point) or crowns (Polygon with tree_x, tree_y), a
                    spatial file, or a cloud2trees delivery directory. Needs treeID, tree_x, tree_y,
                    tree_height_m, tree_cbh_m, crown_dia_m, max_crown_diam_height_m and the CBD column
                    for cbd_method (landfire_tree_kg_per_m3 or cruz_tree_kg_per_m3).
    output_dir      existing directory; lanl_trees_delivery is created (or emptied) inside it.
    study_boundary  one polygon (GeoDataFrame, GeoSeries, shapely geometry in the tree list CRS, or a
                    file). None uses the bounding box of the tree tops.
    bbox_aoi        use the bounding box of the (buffered) boundary instead of its shape.
    buffer          buffer applied to the boundary before clipping, in tree list CRS units (m).
    dtm_path        GeoTIFF DTM; when given, dtm_Clipped.tif and topo.dat are written whatever
                    topofile is. Defaults to the first dtm_*.tif when tree_list is a directory.
    topofile        "flat" (QUIC-Fire) or "dtm" (fuellist points at topo.dat, for FIRETEC).
    cbd_method      "landfire" or "cruz": which crown bulk density column goes to TREES.
    fuel_litter     dict or 5-sequence: ilitter, lrho, lmoisture, lss, ldepth (see DEFAULT_FUEL_LITTER).
    fuel_grass      dict or 5-sequence: igrass, grho, gmoisture, gss, gdepth (see DEFAULT_FUEL_GRASS).
    fuellist_template  path of an alternative fuellist template (default: the package copy).
    log             logging.Logger; defaults to the module logger.

    Returns a dict: tree_list (clipped), aoi, domain, dtm (array or None), treelist (the formatted
    table), output_dir, domain_path, dtm_path, topofile_path, fuellist_path, treelist_path.
    """
    log = log or logging.getLogger(__name__)
    horizontal_res = HORIZONTAL_RES

    # checks
    input_dir = None
    if isinstance(tree_list, (str, Path)) and Path(tree_list).is_dir():
        input_dir = Path(tree_list)
        found = search_dir_final_detected(input_dir)
        if found["crowns_flist"] is None and found["ttops_flist"] is None:
            raise ValueError("could not locate tree list data in:\n    " + str(input_dir.resolve()))
        if dtm_path is None and found["dtm_flist"] is not None:
            dtm_path = found["dtm_flist"][0]
    topofile = check_topofile(topofile)
    if topofile == "dtm" and dtm_path is None:
        where = str(input_dir.resolve()) if input_dir is not None else "`dtm_path` (none given)"
        raise ValueError("could not locate DTM raster in:\n    " + where)
    cbd_method = check_biomass_method(cbd_method)[0]
    cbd_col_name = CBD_COLUMN[cbd_method]
    fuel_litter = check_fuel_list(fuel_litter, "litter")
    fuel_grass = check_fuel_list(fuel_grass, "grass")

    # outdir
    outdir = Path(output_dir)
    if not outdir.is_dir():
        raise ValueError("could not locate the directory `output_dir` at:\n    " + str(outdir.resolve()))
    outdir = outdir.resolve() / DELIVERY_DIR
    if not outdir.exists():
        outdir.mkdir()
    else:
        for root, _dirs, files in os.walk(outdir):
            for name in files:
                os.remove(os.path.join(root, name))

    # tree list
    if isinstance(tree_list, (str, Path)):
        tree_list = read_trees_flist(tree_list, which_trees="tops")
    if not isinstance(tree_list, gpd.GeoDataFrame):
        raise ValueError("tree_list must be sf class object")
    if tree_list.crs is None:
        raise ValueError("tree_list does not have a CRS")
    if study_boundary is None:
        tops = check_spatial_points(tree_list)
        study_boundary = gpd.GeoSeries([box(*tops.total_bounds)], crs=tree_list.crs)
    else:
        study_boundary = _as_boundary(study_boundary, default_crs=tree_list.crs)

    # clip_tree_list_aoi
    clipped = clip_tree_list_aoi(tree_list, study_boundary, bbox_aoi=bbox_aoi, buffer=buffer, reproject_epsg=None)

    # quicfire_define_domain
    domain_ans = quicfire_define_domain(clipped["aoi"], horizontal_resolution=horizontal_res, outdir=outdir, log=log)
    domain = domain_ans["quicfire_domain_df"]

    # quicfire_dtm_topofile: R runs it whenever a DTM is present, not only for topofile = "dtm"
    if dtm_path is not None:
        topo_ans = quicfire_dtm_topofile(dtm_path, horizontal_resolution=horizontal_res, study_boundary=domain, outdir=outdir, log=log)
    else:
        topo_ans = {"dtm": None, "dtm_path": None, "topofile_path": None}

    # make_lanl_trees_input
    if topofile == "dtm" and not topo_ans["topofile_path"]:
        raise ValueError(
            "`topofile` set to dtm but the topo.dat file could not be generated due to missing vaules in bounding box."
            "\n   set `topofile` to flat instead?"
        )
    topofile_arg = topo_ans["topofile_path"] if topofile == "dtm" else "flat"
    trees_ans = make_lanl_trees_input(
        clipped["tree_list"], domain, topofile=topofile_arg, cbd_col_name=cbd_col_name,
        horizontal_resolution=horizontal_res, outdir=outdir, fuel_litter=fuel_litter, fuel_grass=fuel_grass,
        fuellist_template=fuellist_template, log=log,
    )

    return {
        "tree_list": clipped["tree_list"],
        "aoi": clipped["aoi"],
        "domain": domain,
        "dtm": topo_ans["dtm"],
        "treelist": trees_ans["treelist"],
        "output_dir": str(outdir),
        "domain_path": domain_ans["domain_path"],
        "dtm_path": topo_ans["dtm_path"],
        "topofile_path": topo_ans["topofile_path"],
        "fuellist_path": trees_ans["fuellist_path"],
        "treelist_path": trees_ans["treelist_path"],
    }
