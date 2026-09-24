"""
c2t/cbh.py: crown base height (CBH) from the normalized point cloud. Port of cloud2trees
trees_cbh() (R/trees_cbh.R, R/utils_cbh.R), ladderfuelsr_cbh() (R/ladderfuelsr_cbh.R) and
leafr_for_ladderfuelsr() (R/leafr_for_ladderfuelsr.R), which wrap LadderFuelsR 0.0.7 (Viedma et al.
2024) and a rewrite of leafR 0.3.5 (Almeida et al. 2019). The LadderFuelsR and leafR sources were
read from the CRAN tarballs while porting; nothing here calls R.

Pipeline per sampled tree
  points inside the crown (classes 2, 9, 18 dropped, xyz duplicates dropped, at least 7 points)
  -> voxel pulse counts at voxel_grain_size_m horizontally and 1 m vertically
  -> MacArthur-Horn LAD per voxel, top down per column, ground bin dropped
  -> LAD profile: mean LAD per 1 m bin, height = bin + 0.5
  -> percentile classes of LAD, gap bins and fuel base bins (get_gaps_fbhs)
  -> fuel layers, distances, depths (get_distance, get_depths)
  -> merge layers separated by at most num_jump_steps bins (get_real_fbh, get_real_depths)
  -> effective gaps (get_effective_gap)
  -> LAD share per layer, drop layers below min_lad_pct (get_layers_lad)
  -> CBH by three criteria (get_cbh_metrics): maxlad_Hcbh, max_Hcbh, last_Hcbh
then a random forest on tree_height_m, crown area and centroid x, y fills the trees that were not
sampled or where no CBH could be extracted.

Ported faithfully (same rule, same thresholds, checked against the R source)
  * leafr_for_ladderfuelsr(): z below 0 set to 0, horizontal voxel = max(1, floor(grain)), vertical
    voxel fixed at 1 m (dist_btwn_bins_m is not a voxel size in cloud2trees either), voxel x, y from
    round_to_multiple() with the per tree floor(min) start, z voxel = floor(z), columns expanded from
    the lowest voxel z in the data to the tree top, pulses_in = pulses_out + pulses of the voxel,
    LAD = ln(pulses_in / pulses_out) / (k * dz) with k = 1 and dz = 1, non finite LAD set to NA,
    ground bin (z voxel 0) dropped, profile lad = mean LAD over columns (NA ignored, all NA gives
    NaN), total_pulses = pulses in bins above the ground bin, height = z + 0.5, trees with fewer than
    7 points dropped (min_pulses = 6).
  * ladderfuelsr_cbh() depuration: total_pulses >= 6, at least one bin with lad > 0, at least
    min_vhp_n bins (cloud2trees passes 3), NaN lad replaced by 0.01.
  * get_gaps_fbhs(): all lad equal gives no CBH; smooth.spline needs 4 unique heights so a 3 bin
    profile gives no CBH; percentile classes 5, 10, ..., 95, 99, 100 from type 7 quantiles; gap rows
    = minima of descending class runs with class <= lad_pct_gap, first and last row of each run of
    class 5, first and last row of each run of class <= lad_pct_gap; fuel base rows = minima of runs
    where the class rises by more than 5 with class > lad_pct_base, first and last row of each run of
    class > lad_pct_base; rows that are both are bases; heights floored to the bin index.
  * get_distance() and get_depths(): every gap row belongs to the closest base above it; a layer is
    a run of base rows with no gap row between them, its base is the first row and its top (Hdptf)
    the last; dist = base minus the lowest gap row below, Hdist = base minus step; the first layer's
    dist is base minus step when the base is above min_height and 0 otherwise; depth = top minus
    base with 0 replaced by 1.
  * get_real_fbh() and get_real_depths(): a layer whose dist is <= num_jump_steps is merged into the
    layer below (base of the lower, top of the upper, dist of the lower); depth recomputed as top
    minus base, 0 replaced by 1.
  * get_effective_gap() and get_layers_lad(): effdist of layer i >= 2 is the gap below it; effdist
    of the first layer is base minus step when above min_height else 0, and 0 when there is more
    than one layer and it is <= num_jump_steps; LAD share of a layer = sum of lad over its bins over
    the sum of the whole profile times 100; layers with share below min_lad_pct are dropped when at
    least one layer is above it.
  * get_cbh_metrics(): maxlad_Hcbh = base of the layer with the largest share (last on ties),
    max_Hcbh = base of the layer with the largest effdist (last on ties), last_Hcbh = base of the
    last (highest) layer; maxlad1_Hcbh is the second layer when the first has the largest share and
    its top Hdptf1 <= frst_layer_min_ht_m (the R code tests Hdptf1, not the depth).
  * trees_cbh(): sample of tree_sample_n or tree_sample_prop crowns, cbh must be below the tree
    height to be training data, predictors tree_height_m, crown_area_zzz (polygon area),
    tree_x_zzz and tree_y_zzz (polygon centroid), random forest through c2t.rf with subsamples of
    25000 and clamp(ceil(0.5 n / 25000), 3, 50) models, imputation only with more than 10 training
    trees, the 95th percentile of cbh / height among training trees caps every imputed value,
    training trees with cbh >= height are demoted, outputs tree_cbh_m and is_training_cbh, the
    which_cbh mapping of cloud2trees: "lowest" = last_Hcbh, "highest" = max_Hcbh, "max_lad" =
    maxlad_Hcbh.

Assumptions and known departures (read before trusting a number)
  * cloud2trees names last_Hcbh "lowest" and max_Hcbh "highest". In LadderFuelsR last_Hcbh is the
    base of the topmost effective layer and max_Hcbh the base of the layer above the largest gap,
    so "lowest" is never below "highest". The mapping is kept so numbers match R; the per tree dict
    also returns Hcbh1, the base of the lowest layer, for callers who want that.
  * LadderFuelsR's get_distance(), get_depths(), get_real_fbh(), get_real_depths() and
    get_effective_gap() are several thousand lines of column name surgery whose final sections
    reduce to the rules listed above. Branches for degenerate column layouts are not reproduced:
    the effdist misalignment when the first dist equals num_jump_steps, the Hdist inheritance that
    only runs with more than two Hdist columns, and the "condition1 and condition2" base reset in
    get_real_fbh(). After a layer is dropped for low LAD share the gap of the layer above is the
    count of bins between the surviving layers; R sums dist + dptf + dist, which is one bin less
    because dptf is top minus base, unless the first layer sits at min_height, in which case R also
    recomputes the bin count.
  * Points of a crown are pooled over every normalized file that touches it. cloud2trees processes
    tile by tile and keeps the profile from the tile with the most pulses.
  * Points are assigned to crowns with cloud.points_to_crowns (crown ids burned to a 0.25 m grid),
    not with an exact point in polygon test.
  * The lowest voxel z used to expand every column is the minimum over all crown points loaded;
    in R it is the minimum within each catalog chunk.
  * The cloud2trees retry with alternative parameters (voxel 2 m, bins 0.5 m, min_vhp_n 3,
    min_lad_pct 10) is kept for the case where no CBH at all was extracted.
  * R saves only the first random forest (cbh_height_model_estimates.rds); here every model is
    saved as cbh_model_<i>.joblib and the training table as cbh_training_data.csv.
  * dist_btwn_bins_m only enters as the additive step in Hdist and in the first layer distance;
    the profile bins are always 1 m, as in cloud2trees.
  * tree_sample_n defaults to 333 here (NA in R), so a given tree_sample_prop takes precedence;
    R prefers tree_sample_n when both are passed.
  * num_jump_steps, min_lad_pct, min_vhp_n and frst_layer_min_ht_m are extra keyword arguments
    with the cloud2trees defaults (1, 10, 3, 1).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd

from . import cloud, rf, schema

DROP_CLASSES_CBH = (2, 9, 18)                 # lidR catalog filter in trees_cbh_sf: ground, water, high noise
MIN_PULSES = 6                                # leafr_for_ladderfuelsr(min_pulses = 6): keep trees with more than 6 points
PCT_PROBS = np.array([5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 99]) / 100.0
PCT_CLASSES = np.array([5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 99, 100])
WHICH_CBH = {"lowest": "cbh_last_height_m", "highest": "cbh_max_height_m", "max_lad": "cbh_maxlad_height_m"}


# ---------------------------------------------------------------------------
# leafR part: voxels and LAD profile
# ---------------------------------------------------------------------------

def round_to_multiple(x, multiple: float, start=None) -> np.ndarray:
    """round_to_multiple() in leafr_for_ladderfuelsr.R: nearest multiple, never below start."""
    x = np.asarray(x, dtype=float)
    rem = np.mod(x, multiple)
    nearest = np.where(rem < multiple / 2.0, x - rem, x - rem + multiple)
    if start is not None:
        nearest = np.where(nearest < start, start, nearest)
    return nearest


def lad_profiles(x, y, z, tree, voxel_grain_size_m: float = 1.0, k: float = 1.0, z_min=None) -> pd.DataFrame:
    """
    leafr_lad_voxels() + leafr_lad_profile() for many trees at once. `tree` is an int label per
    point. Returns one row per tree and 1 m bin above the ground bin with columns tree, height,
    lad, pulses, total_pulses. Bins with no finite LAD in any column have lad NaN.
    """
    x = np.asarray(x, float); y = np.asarray(y, float); z = np.asarray(z, float); tree = np.asarray(tree)
    if len(x) == 0:
        return pd.DataFrame(columns=["tree", "height", "lad", "pulses", "total_pulses"])
    res = max(1.0, float(math.floor(float(voxel_grain_size_m))))
    z = np.where(z < 0, 0.0, z)
    df = pd.DataFrame({"tree": tree, "x": x, "y": y})
    x_start = round_to_multiple(np.floor(df.groupby("tree")["x"].transform("min").values), res)
    y_start = round_to_multiple(np.floor(df.groupby("tree")["y"].transform("min").values), res)
    vox = pd.DataFrame({"tree": tree,
                        "x": round_to_multiple(x, res, x_start),
                        "y": round_to_multiple(y, res, y_start),
                        "z": np.floor(z)})
    counts = vox.groupby(["tree", "x", "y", "z"]).size().rename("pulses").reset_index()
    zmin = float(counts["z"].min()) if z_min is None else float(math.floor(float(z_min)))
    # trim_voxels = "xy": every column of a tree gets every z from zmin to the tree's top
    zmax = counts.groupby("tree")["z"].max()
    nz = (zmax.values - zmin + 1).astype(int)
    zr = pd.DataFrame({"tree": np.repeat(zmax.index.values, nz),
                       "z": np.concatenate([np.arange(zmin, m + 1) for m in zmax.values])})
    cols = counts[["tree", "x", "y"]].drop_duplicates()
    full = cols.merge(zr, on="tree").merge(counts, on=["tree", "x", "y", "z"], how="left")
    full["pulses"] = full["pulses"].fillna(0).astype(float)
    full = full.sort_values(["tree", "x", "y", "z"], ascending=[True, True, True, False]).reset_index(drop=True)
    g = full.groupby(["tree", "x", "y"], sort=False)["pulses"]
    total = g.transform("sum")
    pulses_out = total - g.cumsum()
    pulses_in = pulses_out + full["pulses"]           # the pulses out of the voxel above
    with np.errstate(divide="ignore", invalid="ignore"):
        lad = np.log(pulses_in.values / pulses_out.values) / k / 1.0
    lad[~np.isfinite(lad)] = np.nan
    full["LAD"] = lad
    full = full[full["z"] > 0]                        # drop the ground bin
    prof = full.groupby(["tree", "z"]).agg(pulses=("pulses", "sum"), lad=("LAD", "mean")).reset_index()
    prof["total_pulses"] = prof.groupby("tree")["pulses"].transform("sum")
    prof["height"] = prof["z"] + 0.5
    return prof[["tree", "height", "lad", "pulses", "total_pulses"]]


def lad_profile(points_dict: dict, voxel_grain_size_m: float = 1.0, k: float = 1.0, z_min=None) -> pd.DataFrame:
    """
    LAD profile of one tree from its points (dict with x, y, z arrays, z = height above ground,
    classes 2, 9, 18 already excluded or not present). Columns height, lad, pulses, total_pulses.
    z_min defaults to the lowest voxel of these points; trees_cbh() passes the minimum over all
    crowns as R does per catalog chunk.
    """
    n = len(points_dict["x"])
    prof = lad_profiles(points_dict["x"], points_dict["y"], points_dict["z"], np.zeros(n, dtype=int), voxel_grain_size_m, k, z_min)
    return prof.drop(columns=["tree"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# LadderFuelsR part
# ---------------------------------------------------------------------------

def percentile_classes(lad: np.ndarray) -> np.ndarray:
    """get_gaps_fbhs() classes: 5 when lad <= P5, 10 when P5 < lad <= P10, ..., 99, and 100 above P99."""
    q = np.quantile(lad, PCT_PROBS)                   # numpy linear = R type 7
    idx = np.searchsorted(q, lad, side="left")
    return PCT_CLASSES[idx]


def _runs(idx: np.ndarray, consecutive: bool = True) -> list:
    """Split sorted positions into runs of consecutive positions (or one run per position)."""
    idx = np.asarray(idx, dtype=int)
    if len(idx) == 0:
        return []
    if not consecutive:
        return [np.array([i]) for i in idx]
    breaks = np.where(np.diff(idx) != 1)[0] + 1
    return np.split(idx, breaks)


def gaps_fbhs_rows(lad: np.ndarray, cls: np.ndarray, perc_gap: float, perc_base: float, step: float = 1.0):
    """
    get_gaps_fbhs(): positions of gap rows and fuel base rows in a profile sorted by height.
    Runs of classes are consecutive bins when step is the 1 m bin, otherwise every bin is its own
    run (R groups by diff(height) != step).
    """
    consecutive = abs(float(step) - 1.0) < 1e-9
    d = np.diff(cls.astype(float))
    gap_rows: set = set()
    for run in _runs(np.where(d < 0)[0] + 1):
        l = lad[run]; fin = np.isfinite(l)
        if fin.any():
            mn = l[fin].min()
            for i in run[l == mn]:
                if cls[i] <= perc_gap:
                    gap_rows.add(int(i))
    for subset in (np.where(cls == 5)[0], np.where(cls <= perc_gap)[0]):
        for run in _runs(subset, consecutive):
            gap_rows.add(int(run[0])); gap_rows.add(int(run[-1]))
    base_rows: set = set()
    for run in _runs(np.where(d > 5)[0] + 1):
        l = lad[run]; fin = np.isfinite(l)
        if fin.any():
            mn = l[fin].min()
            for i in run[l == mn]:
                if cls[i] > perc_base:
                    base_rows.add(int(i))
    for run in _runs(np.where(cls > perc_base)[0], consecutive):
        base_rows.add(int(run[0])); base_rows.add(int(run[-1]))
    gap_rows -= base_rows                              # "adapt the gaps to the cbh": bases win
    return gap_rows, base_rows


def fuel_layers(height, lad, step: float = 1.0, min_height: float = 1.0, perc_gap: float = 25, perc_base: float = 25,
                number_steps: int = 1, threshold: float = 10):
    """
    The LadderFuelsR chain from get_gaps_fbhs() to get_layers_lad() on one depurated profile.
    Returns (layers DataFrame, reason). layers is None when no CBH can be determined; otherwise it
    has one row per effective fuel layer, lowest first: Hcbh (base bin), Hdptf (top bin), dptf
    (depth), Hdist, effdist (gap below the layer), lad_pct (share of the profile LAD).
    """
    height = np.asarray(height, float); lad = np.asarray(lad, float)
    order = np.argsort(height, kind="stable")
    height, lad = height[order], lad[order]
    lad = np.where(np.isfinite(lad), lad, 0.01)       # ladderfuelsr_cbh(): coalesce(lad, 0.01)
    if len(np.unique(height)) < 4:
        return None, "fewer than 4 height bins (smooth.spline in get_gaps_fbhs needs 4 unique heights)"
    if len(np.unique(lad)) == 1:
        return None, "all LAD values equal, no fuel gaps"
    bins = np.floor(height).astype(int)
    cls = percentile_classes(lad)
    gap_rows, base_rows = gaps_fbhs_rows(lad, cls, perc_gap, perc_base, step)
    if not base_rows or not gap_rows:
        return None, "no fuel base or no gap found"

    # get_distance() + get_depths(): layers are runs of base rows separated by gap rows
    layers = []                                        # dicts with base, top (bins), gaps (bins below)
    cur = None; pending = []
    for i in sorted(gap_rows | base_rows):
        if i in base_rows:
            if cur is None:
                cur = {"base": int(bins[i]), "top": int(bins[i]), "gaps": pending}; pending = []
            else:
                cur["top"] = int(bins[i])
        else:
            if cur is not None:
                layers.append(cur); cur = None
            pending.append(int(bins[i]))
    if cur is not None:
        layers.append(cur)
    for j, L in enumerate(layers):
        if j == 0:
            if L["base"] > min_height:
                L["dist"] = math.floor(L["base"] - step); L["Hdist"] = L["base"] - step
            else:
                L["dist"] = 0.0; L["Hdist"] = float(min_height)
        else:
            L["dist"] = float(L["base"] - min(L["gaps"])); L["Hdist"] = L["base"] - step

    # get_real_fbh() + get_real_depths(): merge layers separated by at most number_steps
    merged = []
    for j, L in enumerate(layers):
        if j > 0 and L["dist"] <= number_steps:
            merged[-1]["top"] = L["top"]
        else:
            merged.append(dict(L))
    for L in merged:
        L["dptf"] = float(max(L["top"] - L["base"], 1))

    # get_layers_lad(): LAD share per layer and removal below the threshold
    total = float(np.sum(lad))
    for L in merged:
        m = (bins >= L["base"]) & (bins <= L["top"])
        L["lad_pct"] = float(np.sum(lad[m]) / total * 100.0) if total > 0 else 0.0
    if any(L["lad_pct"] > threshold for L in merged):
        merged = [L for L in merged if not L["lad_pct"] < threshold]
    if not merged:
        return None, "every fuel layer below the LAD share threshold"

    # get_effective_gap() (and the get_layers_lad() recomputation): gap below every surviving layer
    for j, L in enumerate(merged):
        if j == 0:
            e = float(math.floor(L["base"] - step)) if L["base"] > min_height else 0.0
            if len(merged) > 1 and e <= number_steps:
                e = 0.0
            L["effdist"] = e
        else:
            L["effdist"] = float(L["base"] - merged[j - 1]["top"] - 1)
    out = pd.DataFrame({"Hcbh": [float(L["base"]) for L in merged], "Hdptf": [float(L["top"]) for L in merged],
                        "dptf": [L["dptf"] for L in merged], "Hdist": [float(L["Hdist"]) for L in merged],
                        "effdist": [L["effdist"] for L in merged], "lad_pct": [L["lad_pct"] for L in merged]})
    return out, "ok"


def cbh_metrics(layers: pd.DataFrame, hdepth1_height: float = 1.0) -> dict:
    """get_cbh_metrics(): the three CBH criteria on the effective layers (lowest layer first)."""
    pct = layers["lad_pct"].values; eff = layers["effdist"].values; hcbh = layers["Hcbh"].values
    i_maxlad = int(np.where(pct == pct.max())[0][-1])
    i_max = int(np.where(eff == eff.max())[0][-1])
    i_maxlad1 = i_maxlad
    if i_maxlad == 0 and len(layers) > 1 and layers["Hdptf"].iloc[0] <= hdepth1_height:
        i_maxlad1 = 1
    return {"maxlad_Hcbh": float(hcbh[i_maxlad]), "maxlad1_Hcbh": float(hcbh[i_maxlad1]), "max_Hcbh": float(hcbh[i_max]),
            "last_Hcbh": float(hcbh[-1]), "Hcbh1": float(hcbh[0]), "maxlad_lad": float(pct[i_maxlad]),
            "max_effdist": float(eff[i_max]), "nlayers": int(len(layers))}


def profile_ok(prof: pd.DataFrame, min_vhp_n: int = 3) -> tuple[bool, str]:
    """The depuration filter of ladderfuelsr_cbh() on one tree's profile."""
    if len(prof) == 0:
        return False, "no profile"
    lad = pd.to_numeric(prof["lad"], errors="coerce").fillna(0.0).values
    if float(prof["total_pulses"].iloc[0]) < 6:
        return False, "fewer than 6 pulses above the ground bin"
    if int(np.sum(lad > 0)) < 1:
        return False, "no bin with LAD above 0"
    if len(prof) < 2 or len(prof) < max(int(min_vhp_n), 2):
        return False, f"fewer than {max(int(min_vhp_n), 2)} height bins"
    return True, "ok"


def ladderfuelsr_cbh(points_dict: dict | None = None, tree_height_m=None, profile: pd.DataFrame | None = None,
                     min_vhp_n: int = 3, voxel_grain_size_m: float = 1.0, dist_btwn_bins_m: float = 1.0,
                     min_fuel_layer_ht_m: float = 1.0, lad_pct_gap: float = 25, lad_pct_base: float = 25,
                     num_jump_steps: int = 1, min_lad_pct: float = 10, frst_layer_min_ht_m: float = 1.0,
                     z_min=None) -> dict:
    """
    ladderfuelsr_cbh() for a single tree. Pass the tree's points (x, y, z arrays, z above ground,
    ground and noise classes already excluded) or a ready LAD profile with columns height, lad,
    total_pulses. Returns a dict with ok, reason, lad_profile (DataFrame), layers (DataFrame or None),
    the LadderFuelsR names maxlad_Hcbh, maxlad1_Hcbh, max_Hcbh, last_Hcbh, Hcbh1, nlayers, max_height,
    and the cloud2trees names cbh_maxlad_height_m, cbh_max_height_m, cbh_last_height_m (NaN when not
    ok). tree_height_m is echoed as tree_height_m and sets cbh_below_height for the selected criteria.
    """
    if profile is None:
        if points_dict is None:
            raise ValueError("pass points_dict or profile")
        profile = lad_profile(points_dict, voxel_grain_size_m, 1.0, z_min)
    out = {"ok": False, "reason": "", "lad_profile": profile, "layers": None, "maxlad_Hcbh": np.nan, "maxlad1_Hcbh": np.nan,
           "max_Hcbh": np.nan, "last_Hcbh": np.nan, "Hcbh1": np.nan, "nlayers": 0, "max_height": np.nan,
           "cbh_maxlad_height_m": np.nan, "cbh_max_height_m": np.nan, "cbh_last_height_m": np.nan,
           "tree_height_m": tree_height_m}
    ok, reason = profile_ok(profile, min_vhp_n)
    if not ok:
        out["reason"] = reason
        return out
    layers, reason = fuel_layers(profile["height"].values, profile["lad"].values, dist_btwn_bins_m, min_fuel_layer_ht_m,
                                 lad_pct_gap, lad_pct_base, num_jump_steps, min_lad_pct)
    out["reason"] = reason
    if layers is None:
        return out
    out.update(cbh_metrics(layers, frst_layer_min_ht_m))
    out["layers"] = layers
    out["ok"] = True
    out["max_height"] = float(np.nanmax(profile["height"].values))
    out["cbh_maxlad_height_m"] = out["maxlad_Hcbh"]; out["cbh_max_height_m"] = out["max_Hcbh"]; out["cbh_last_height_m"] = out["last_Hcbh"]
    return out


# ---------------------------------------------------------------------------
# trees_cbh
# ---------------------------------------------------------------------------

def read_trees_poly(trees_poly) -> gpd.GeoDataFrame:
    """A GeoDataFrame, a spatial file, a list of files, or a directory holding final_detected_crowns*.gpkg."""
    if isinstance(trees_poly, gpd.GeoDataFrame):
        g = trees_poly
    elif isinstance(trees_poly, (list, tuple)):
        parts = [gpd.read_file(p) for p in trees_poly]
        g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    else:
        p = Path(str(trees_poly))
        if p.is_dir():
            files = sorted(q for q in p.iterdir() if q.name.startswith("final_detected_crowns") and q.suffix.lower() == ".gpkg")
            if not files:
                raise FileNotFoundError("`trees_poly` directory must have final_detected_crowns* files from cloud2trees() or raster2trees()")
            return read_trees_poly(files)
        if not p.exists():
            raise FileNotFoundError(f"could not find the file:\n    {p}")
        g = gpd.read_file(p)
    g = schema.check_tree_list(g, need=("treeID", "tree_height_m"), geometry="polygon")
    if g["treeID"].duplicated().any():
        raise ValueError("Duplicates found in the treeID column. Please remove duplicates and try again.")
    g = schema.drop_cols(g, ["tree_cbh_m", "is_training_cbh"]).copy()
    g["treeID"] = g["treeID"].astype(str)
    return g.reset_index(drop=True)


def make_spatial_predictors(poly: gpd.GeoDataFrame) -> pd.DataFrame:
    """make_spatial_predictors(): crown_area_zzz, tree_x_zzz, tree_y_zzz from the polygons."""
    cen = poly.geometry.centroid
    df = pd.DataFrame({"treeID": poly["treeID"].values, "tree_height_m": pd.to_numeric(poly["tree_height_m"], errors="coerce").values,
                       "crown_area_zzz": poly.geometry.area.values, "tree_x_zzz": cen.x.values, "tree_y_zzz": cen.y.values})
    return df


def crown_points(samp: gpd.GeoDataFrame, norm_las, log=None) -> dict:
    """
    Points inside the sampled crowns pooled over every normalized file that touches them, with the
    lidR filter of trees_cbh_sf (classes 2, 9, 18 and xyz duplicates dropped). Returns x, y, z and
    tree (row position in samp).
    """
    files = cloud.list_las(norm_las)
    if not files:
        raise FileNotFoundError(f"could not detect .las|.laz files at {norm_las}")
    b = samp.bounds
    xs, ys, zs, ts = [], [], [], []
    for f in files:
        h = cloud.las_header(f)
        hit = samp[(b["maxx"] >= h["xmin"]) & (b["minx"] <= h["xmax"]) & (b["maxy"] >= h["ymin"]) & (b["miny"] <= h["ymax"])]
        if hit.empty:
            continue
        pts = cloud.load_points(f, bounds=tuple(hit.total_bounds))
        if len(pts["x"]) == 0:
            continue
        keep = ~np.isin(pts["classification"], DROP_CLASSES_CBH)
        idx = cloud.points_to_crowns({k: v[keep] for k, v in pts.items()}, hit)
        m = idx >= 0
        if not m.any():
            continue
        x, y, z, t = pts["x"][keep][m], pts["y"][keep][m], pts["z"][keep][m], hit.index.values[idx[m]]
        key = np.round(np.column_stack([x, y, z]) * 1000).astype(np.int64)     # lidR -drop_duplicates
        _, first = np.unique(key, axis=0, return_index=True)
        first = np.sort(first)
        xs.append(x[first]); ys.append(y[first]); zs.append(z[first]); ts.append(t[first])
        if log:
            log.info(f"cbh: {Path(f).name}: {len(first)} points in {len(hit)} crowns")
    if not xs:
        return {"x": np.zeros(0), "y": np.zeros(0), "z": np.zeros(0), "tree": np.zeros(0, int)}
    return {"x": np.concatenate(xs), "y": np.concatenate(ys), "z": np.concatenate(zs), "tree": np.concatenate(ts).astype(int)}


def extract_cbh(pts: dict, samp: gpd.GeoDataFrame, which_cbh: str, min_vhp_n, voxel_grain_size_m, dist_btwn_bins_m,
                min_fuel_layer_ht_m, lad_pct_gap, lad_pct_base, num_jump_steps, min_lad_pct, frst_layer_min_ht_m) -> pd.DataFrame:
    """LAD profiles for every sampled crown, the LadderFuelsR chain per tree, and clean_cbh_df()."""
    if len(pts["x"]) == 0:
        return pd.DataFrame(columns=["treeID", "cbh_maxlad_height_m", "cbh_max_height_m", "cbh_last_height_m", "tree_cbh_m"])
    n_per_tree = np.bincount(pts["tree"], minlength=len(samp))
    keep = n_per_tree[pts["tree"]] > MIN_PULSES
    prof = lad_profiles(pts["x"][keep], pts["y"][keep], pts["z"][keep], pts["tree"][keep], voxel_grain_size_m, 1.0)
    rows = []
    for t, p in prof.groupby("tree", sort=True):
        r = ladderfuelsr_cbh(profile=p.reset_index(drop=True), min_vhp_n=min_vhp_n, dist_btwn_bins_m=dist_btwn_bins_m,
                             min_fuel_layer_ht_m=min_fuel_layer_ht_m, lad_pct_gap=lad_pct_gap, lad_pct_base=lad_pct_base,
                             num_jump_steps=num_jump_steps, min_lad_pct=min_lad_pct, frst_layer_min_ht_m=frst_layer_min_ht_m)
        if r["ok"]:
            rows.append({"treeID": samp["treeID"].iloc[int(t)], "cbh_maxlad_height_m": r["cbh_maxlad_height_m"],
                         "cbh_max_height_m": r["cbh_max_height_m"], "cbh_last_height_m": r["cbh_last_height_m"]})
    df = pd.DataFrame(rows, columns=["treeID", "cbh_maxlad_height_m", "cbh_max_height_m", "cbh_last_height_m"])
    df["tree_cbh_m"] = df[WHICH_CBH[which_cbh]] if len(df) else np.nan
    return df


def trees_cbh(trees_poly, norm_las, tree_sample_n=333, tree_sample_prop=None, which_cbh: str = "lowest",
              estimate_missing_cbh: bool = True, voxel_grain_size_m: float = 1.0, dist_btwn_bins_m: float = 1.0,
              min_fuel_layer_ht_m: float = 1.0, lad_pct_gap: float = 25, lad_pct_base: float = 25, seed: int = 21,
              outfolder=None, log=None, min_vhp_n: int = 3, num_jump_steps: int = 1, min_lad_pct: float = 10,
              frst_layer_min_ht_m: float = 1.0) -> gpd.GeoDataFrame:
    """
    trees_cbh(): CBH for every crown polygon. Extracts CBH from the normalized cloud for a sample of
    crowns with the LadderFuelsR chain, then fills the rest with a random forest on height, crown
    area and location when estimate_missing_cbh is True and more than 10 trees have a CBH.
    Returns the crowns with tree_cbh_m and is_training_cbh added (False for imputed trees, NaN
    tree_cbh_m where nothing could be estimated). Model files cbh_model_<i>.joblib and
    cbh_training_data.csv are written to outfolder when given.
    """
    which = str(which_cbh or "lowest").strip().lower() or "lowest"
    if which not in WHICH_CBH:
        raise ValueError("`which_cbh` must be one of:\nmax_lad, highest, lowest")
    crowns = read_trees_poly(trees_poly)
    force_cbh_lte_ht = True
    estimate_missing_max_n_training = 25000

    # sample (check_sample_vals + slice_sample). tree_sample_n has a default here, so a given
    # tree_sample_prop wins; in R both default to NA and tree_sample_n wins when both are passed.
    n = None if tree_sample_n is None or (isinstance(tree_sample_n, float) and np.isnan(tree_sample_n)) else int(tree_sample_n)
    prop = None if tree_sample_prop is None or (isinstance(tree_sample_prop, float) and np.isnan(tree_sample_prop)) else float(tree_sample_prop)
    if n is None or n <= 0:
        n = 333
    if prop is not None:
        prop = 0.5 if prop <= 0 else min(prop, 1.0)
    samp = cloud.sample_crowns(crowns, n=n, prop=prop, seed=seed)
    samp = samp.reset_index(drop=True)
    if log:
        log.info(f"cbh: extracting CBH for {len(samp)} of {len(crowns)} crowns")

    # extraction
    pts = crown_points(samp, norm_las, log=log)
    cbh_df = extract_cbh(pts, samp, which, min_vhp_n, voxel_grain_size_m, dist_btwn_bins_m, min_fuel_layer_ht_m,
                         lad_pct_gap, lad_pct_base, num_jump_steps, min_lad_pct, frst_layer_min_ht_m)
    if len(cbh_df) == 0 and len(pts["x"]) > 0:
        # trees_cbh_sf(): retry once with the parameters cloud2trees falls back to
        new_min_ht = min_fuel_layer_ht_m if (dist_btwn_bins_m - min_fuel_layer_ht_m >= 1.5 or min_fuel_layer_ht_m == dist_btwn_bins_m) else 1.0
        if dist_btwn_bins_m - min_fuel_layer_ht_m >= 1.5:
            new_step = min_fuel_layer_ht_m + 1
        elif min_fuel_layer_ht_m == dist_btwn_bins_m:
            new_step = min_fuel_layer_ht_m - 0.5 if min_fuel_layer_ht_m >= 1 else min_fuel_layer_ht_m + 0.5
        else:
            new_step = 0.5
        if log:
            log.info(f"cbh: no CBH extracted; retrying with min_fuel_layer_ht_m {new_min_ht}, dist_btwn_bins_m {new_step}, voxel 2 m, min_vhp_n 3, min_lad_pct 10")
        cbh_df = extract_cbh(pts, samp, which, 3, 2.0, new_step, new_min_ht, lad_pct_gap, lad_pct_base, num_jump_steps, 10, frst_layer_min_ht_m)

    # clean_cbh_df(): cbh must be below the tree height, spatial predictors
    pred_all = make_spatial_predictors(crowns)
    cbh_df = pred_all.merge(cbh_df, on="treeID", how="inner")
    cbh_df = cbh_df[np.isfinite(cbh_df["tree_cbh_m"].values)]
    if force_cbh_lte_ht:
        cbh_df = cbh_df[cbh_df["tree_cbh_m"] < cbh_df["tree_height_m"]]
    cbh_df = cbh_df.assign(is_training_cbh=True).reset_index(drop=True)
    n_cbh = len(cbh_df)
    if log:
        log.info(f"cbh: {n_cbh} trees with an extracted CBH")
    if outfolder is not None:
        Path(outfolder).mkdir(parents=True, exist_ok=True)
        cbh_df.to_csv(Path(outfolder) / "cbh_training_data.csv", index=False)

    out = crowns.copy()
    if n_cbh == 0:
        if log:
            log.info("cbh: No CBH values extracted")
        out["tree_cbh_m"] = np.nan
        out["is_training_cbh"] = False
        return out

    # model and imputation
    predictors = ["tree_height_m", "crown_area_zzz", "tree_x_zzz", "tree_y_zzz"]
    mods = None
    if estimate_missing_cbh and n_cbh > 10:
        ntimes = int(min(max(math.ceil(n_cbh * 0.5 / estimate_missing_max_n_training), 3), 50))
        train = cbh_df[np.isfinite(cbh_df[predictors].values).all(axis=1)]
        mods = rf.rf_subsample_and_model_n_times(train[predictors], train["tree_cbh_m"].values,
                                                 mod_n_subsample=estimate_missing_max_n_training, mod_n_times=ntimes, seed=seed)
        if outfolder is not None:
            import joblib
            for i, m in enumerate(mods, start=1):
                joblib.dump(m, Path(outfolder) / f"cbh_model_{i}.joblib")
    elif estimate_missing_cbh and log:
        log.info("cbh: Insufficient data available to estimate missing CBH values. Returning CBH values extracted from cloud only.")

    out = out.merge(cbh_df[["treeID", "tree_cbh_m", "is_training_cbh"]], on="treeID", how="left")
    out["is_training_cbh"] = out["is_training_cbh"].fillna(False).astype(bool)
    if mods is not None:
        todo = pred_all[~pred_all["treeID"].isin(cbh_df["treeID"])]
        todo = todo[np.isfinite(todo[predictors].values).all(axis=1)]
        if len(todo):
            pred = rf.rf_model_avg_predictions(mods, todo[predictors])
            fill = pd.Series(pred, index=todo["treeID"].values)
            miss = ~out["is_training_cbh"].values
            out.loc[miss, "tree_cbh_m"] = out.loc[miss, "treeID"].map(fill).values

    # prevent the CBH from being above the tree height (force_cbh_lte_ht)
    if force_cbh_lte_ht:
        h = pd.to_numeric(out["tree_height_m"], errors="coerce")
        tr = out["is_training_cbh"] & (out["tree_cbh_m"] < h)
        max_ratio = float(np.quantile((out.loc[tr, "tree_cbh_m"] / h[tr]).values, 0.95)) if tr.any() else np.nan
        out.loc[out["is_training_cbh"] & (out["tree_cbh_m"] >= h), "is_training_cbh"] = False
        if np.isfinite(max_ratio):
            cap = (~out["is_training_cbh"]) & ((out["tree_cbh_m"] / h) > max_ratio)
            out.loc[cap, "tree_cbh_m"] = max_ratio * h[cap]
    return gpd.GeoDataFrame(out, geometry="geometry", crs=crowns.crs)
