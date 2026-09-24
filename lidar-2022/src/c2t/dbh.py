"""
c2t/dbh.py — DBH from tree height. Port of trees_dbh() from cloud2trees.

Regional model (always): TreeMap 2022 pixels (30 m, one FIA plot id per pixel) inside the study boundary
plus boundary_buffer are counted; each plot's live trees (STATUSCD 1, DIA in inches, HT in feet) enter a
height-to-DBH fit weighted by the pixel count of their plot. R fit this with brms:
  cr:    dbh = asym * (1 - exp(-k * h))^p, lognormal family
  power: dbh = b1 * h + h^b2, Gamma family
Here the same forms are fit with scipy.optimize.curve_fit on log(dbh) (weights as sigma = 1/sqrt(w)); the
5 and 95 percent bounds come from the weighted residual sd on the log scale (a prediction band, not a
posterior predictive interval). Predictions are made on a 0.01 m height grid and joined to the tree list
exactly as R does (fia_est_dbh_cm, _lower, _upper).

Local model (when treels_dbh_locations is given): stems are joined to crown polygons, those outside the
regional 90 percent band are dropped, the stem closest to the regional estimate is kept per crown, and
with more than 10 trees a local model (lin: log(dbh) ~ height, or rf: random forest on height, x, y) predicts
DBH for the other crowns. dbh_cm = coalesce(stem, local prediction, regional).

Output columns match R: fia_est_dbh_cm, fia_est_dbh_cm_lower, fia_est_dbh_cm_upper, dbh_cm,
is_training_data, dbh_m, radius_m, basal_area_m2, basal_area_ft2, ptcld_extracted_dbh_cm, ptcld_predicted_dbh_cm.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit

from . import schema
from .extdata import find_ext_data

DBH_COLS = ("fia_est_dbh_cm", "fia_est_dbh_cm_lower", "fia_est_dbh_cm_upper", "dbh_cm", "is_training_data", "dbh_m",
            "radius_m", "basal_area_m2", "basal_area_ft2", "ptcld_extracted_dbh_cm", "ptcld_predicted_dbh_cm")
TREEMAP_2022 = ("treemap2022_conus.tif", "treemap2022_conus_tree_table.csv")
TREEMAP_2016 = ("treemap2016.tif", "treemap2016_tree_table.csv")


# ---------------------------------------------------------------------------
# TreeMap training data
# ---------------------------------------------------------------------------

def treemap_files(treemap_dir) -> dict:
    d = Path(treemap_dir)
    for vintage, (tif, csv) in ((2022, TREEMAP_2022), (2016, TREEMAP_2016)):
        hits_tif = list(d.rglob(tif)); hits_csv = list(d.rglob(csv))
        if hits_tif and hits_csv:
            return {"which_treemap": vintage, "treemap_rast": hits_tif[0], "treemap_trees": hits_csv[0]}
    raise FileNotFoundError("Treemap data has not been downloaded. Use `get_treemap()` first.\nIf you supplied `input_treemap_dir` check that directory for data.")


def treemap_training_data(treemap_dir, boundary_gdf, log=None) -> tuple[pd.DataFrame, int]:
    """
    Live FIA trees of the TreeMap plots imputed inside boundary_gdf, with tree_weight = pixel count of the
    plot. Returns (DataFrame[tm_id, cn, species_symbol, tree_weight, dbh_cm, tree_height_m], vintage).
    """
    import rasterio
    from rasterio.mask import mask as rio_mask
    tm = treemap_files(treemap_dir)
    with rasterio.open(tm["treemap_rast"]) as r:
        b = boundary_gdf.to_crs(r.crs)
        geoms = [g for g in b.geometry.values if g is not None and not g.is_empty]
        try:
            arr, _ = rio_mask(r, geoms, crop=True, filled=True, nodata=r.nodata if r.nodata is not None else 0, indexes=1)
        except ValueError as e:
            raise ValueError("The search area does not overlap with an area within CONUS. Cannot estimate DBH.") from e
        nod = r.nodata if r.nodata is not None else 0
    vals = arr[(arr != nod) & np.isfinite(arr.astype(float))]
    if vals.size == 0:
        raise ValueError("The search area does not overlap with a forested area within CONUS. Cannot estimate DBH.\n ... try expanding the `boundary_buffer` ?")
    ids, counts = np.unique(vals.astype(np.int64), return_counts=True)
    weights = pd.DataFrame({"tm_id": ids.astype(str), "tree_weight": counts})
    if tm["which_treemap"] == 2022:
        cols = ["TM_ID", "PLT_CN", "SPECIES_SYMBOL", "STATUSCD", "DIA", "HT", "CR"]
    else:
        cols = ["tm_id", "CN", "SPECIES_SYMBOL", "STATUSCD", "DIA", "HT", "CR"]
    # the tree table is large (millions of rows); read only the columns and filter by plot id in chunks
    keep_ids = set(weights["tm_id"])
    parts = []
    for chunk in pd.read_csv(tm["treemap_trees"], usecols=cols, dtype={cols[0]: str, cols[1]: str}, chunksize=500_000, low_memory=False):
        chunk.columns = [c.lower() for c in chunk.columns]
        if "plt_cn" in chunk.columns:
            chunk = chunk.rename(columns={"plt_cn": "cn"})
        chunk["tm_id"] = chunk["tm_id"].astype(str).str.replace(r"\.0$", "", regex=True)
        sub = chunk[chunk["tm_id"].isin(keep_ids)]
        if len(sub):
            parts.append(sub)
    if not parts:
        raise ValueError("Could not estimate DBH from Treemap data. Ensure the study area is in the continental US.")
    df = pd.concat(parts, ignore_index=True).merge(weights, on="tm_id", how="inner")
    df = df[(df["statuscd"] == 1) & df["dia"].notna() & df["ht"].notna() & df["tree_weight"].notna()].copy()
    df["dbh_cm"] = df["dia"] * 2.54
    df["tree_height_m"] = df["ht"] * 0.3048
    df = df.drop(columns=["statuscd", "dia", "ht", "cr"])
    if log: log.info(f"TreeMap {tm['which_treemap']}: {len(weights):,} plots in the search area, {len(df):,} live trees for the regional model")
    return df, tm["which_treemap"]


# ---------------------------------------------------------------------------
# Regional model
# ---------------------------------------------------------------------------

def _cr(h, asym, k, p):
    return asym * np.power(1.0 - np.exp(-k * h), p)


def _power(h, b1, b2):
    return b1 * h + np.power(h, b2)


def fit_regional_model(train: pd.DataFrame, model: str = "cr") -> dict:
    """Weighted fit on the log scale. Returns dict with params, sigma_log, model, n, and the formula string."""
    h = train["tree_height_m"].to_numpy(float); d = train["dbh_cm"].to_numpy(float); w = train["tree_weight"].to_numpy(float)
    ok = (h > 0) & (d > 0) & (w > 0)
    h, d, w = h[ok], d[ok], w[ok]
    sigma = 1.0 / np.sqrt(w)
    if model.strip().lower() == "power":
        f = lambda x, b1, b2: np.log(np.clip(_power(x, b1, b2), 1e-6, None))
        p0 = [1.0, 1.0]; bounds = ([-np.inf, -5.0], [np.inf, 5.0]); names = ["b1", "b2"]
        formula = "dbh_cm ~ (b1 * tree_height_m) + tree_height_m^b2"
    else:
        f = lambda x, asym, k, p: np.log(np.clip(_cr(x, asym, k, p), 1e-6, None))
        p0 = [60.0, 0.05, 2.0]; bounds = ([1.0, 1e-4, 0.1], [500.0, 2.0, 20.0]); names = ["asym", "k", "p"]
        formula = "dbh_cm ~ asym * (1 - exp(-k * tree_height_m))^p"
    params, _ = curve_fit(f, h, np.log(d), p0=p0, sigma=sigma, bounds=bounds, maxfev=20000)
    resid = np.log(d) - f(h, *params)
    sigma_log = float(np.sqrt(np.sum(w * resid ** 2) / np.sum(w)))
    return {"model": "power" if model.strip().lower() == "power" else "cr", "params": dict(zip(names, map(float, params))),
            "sigma_log": sigma_log, "n": int(len(h)), "formula": formula}


def predict_regional(fit: dict, heights, z: float = 1.645) -> pd.DataFrame:
    """fia_est_dbh_cm and its 5 and 95 percent bounds for the given heights."""
    h = np.asarray(heights, dtype=float)
    p = fit["params"]
    mu = _power(h, p["b1"], p["b2"]) if fit["model"] == "power" else _cr(h, p["asym"], p["k"], p["p"])
    mu = np.clip(mu, 1e-6, None)
    s = fit["sigma_log"]
    return pd.DataFrame({"tree_height_m": h, "fia_est_dbh_cm": mu, "fia_est_dbh_cm_lower": mu * np.exp(-z * s), "fia_est_dbh_cm_upper": mu * np.exp(z * s)})


# ---------------------------------------------------------------------------
# trees_dbh
# ---------------------------------------------------------------------------

def trees_dbh(tree_list, crs=None, study_boundary=None, dbh_model_regional: str = "cr", dbh_model_local: str = "lin",
              treels_dbh_locations=None, boundary_buffer: float = 50.0, input_treemap_dir=None, outfolder=None, log=None):
    """
    Estimate DBH for a tree list. tree_list needs treeID, tree_x, tree_y, tree_height_m (a crown Polygon
    GeoDataFrame when treels_dbh_locations is given). Returns the tree list as points with the DBH columns.
    Writes regional_dbh_height_model_training_data.csv, _estimates.csv (json params), _predictions.csv to outfolder.
    """
    import geopandas as gpd
    from shapely.geometry import box
    ext = find_ext_data(input_treemap_dir=input_treemap_dir)
    if ext.get("treemap_dir") is None:
        raise FileNotFoundError("Treemap data has not been downloaded to package contents. Use `get_treemap()` first.\nIf you supplied a value to the `input_treemap_dir` parameter check that directory for data.")
    schema.check_tree_list(tree_list, need=("tree_height_m",))
    tl = tree_list.copy()
    tl["tree_height_m"] = pd.to_numeric(tl["tree_height_m"], errors="coerce")
    tops = schema.as_points(schema.ensure_treeid(tl), crs)
    tops = schema.drop_cols(tops, DBH_COLS)
    out = Path(outfolder) if outfolder else None
    if out: out.mkdir(parents=True, exist_ok=True)
    use_local = treels_dbh_locations is not None and len(treels_dbh_locations) > 0
    if use_local:
        schema.check_tree_list(tl, need=("tree_height_m",), geometry="polygon")
        if not isinstance(treels_dbh_locations, gpd.GeoDataFrame) or not treels_dbh_locations.geom_type.isin(["Point", "MultiPoint"]).all():
            raise ValueError("`treels_dbh_locations` data must be a GeoDataFrame with POINT geometry (see treels_stem_dbh())")
        if "dbh_cm" not in treels_dbh_locations.columns:
            raise ValueError("`treels_dbh_locations` data must have a column titled `dbh_cm` with numeric DBH values in cm.")
    # study boundary plus buffer, in the tree CRS
    if study_boundary is not None:
        buff = gpd.GeoDataFrame(geometry=[study_boundary.to_crs(tops.crs).geometry.union_all().buffer(boundary_buffer)], crs=tops.crs)
    else:
        buff = gpd.GeoDataFrame(geometry=[box(*tops.total_bounds).buffer(boundary_buffer)], crs=tops.crs)
    if not tops.intersects(buff.geometry.iloc[0]).any():
        raise ValueError("No trees in `tree_list` are within the `study_boundary`. DBH not estimated.\n .... Check your data locations. If confident in tree locations, leave `study_boundary` as None")
    # regional model
    train, vintage = treemap_training_data(ext["treemap_dir"], buff, log=log)
    fit = fit_regional_model(train, dbh_model_regional)
    if log: log.info(f"Regional DBH model ({fit['model']}): {fit['params']}, sigma_log {fit['sigma_log']:.3f}, n {fit['n']:,}")
    grid = np.unique(np.round(np.concatenate([tops["tree_height_m"].dropna().to_numpy(float), np.arange(0, 120.0001, 0.1)]), 2))
    grid = grid[grid > 0]
    pred = predict_regional(fit, grid)
    pred["tree_height_m_tnth"] = np.round(pred["tree_height_m"], 2).astype(str)
    if out:
        train.assign(which_treemap=vintage).to_csv(out / "regional_dbh_height_model_training_data.csv", index=False)
        pd.DataFrame([{"variable": k, "estimate": v, "formula": fit["formula"]} for k, v in fit["params"].items()] +
                     [{"variable": "sigma_log", "estimate": fit["sigma_log"], "formula": fit["formula"]}]).to_csv(out / "regional_dbh_height_model_estimates.csv", index=False)
        pred.drop(columns=["tree_height_m"]).to_csv(out / "regional_dbh_height_model_predictions.csv", index=False)
        (out / "regional_dbh_height_model.json").write_text(json.dumps(fit, indent=2))
    tops["tree_height_m_tnth"] = np.round(tops["tree_height_m"].astype(float), 2).astype(str)
    tops = tops.merge(pred.drop(columns=["tree_height_m"]), on="tree_height_m_tnth", how="left").drop(columns=["tree_height_m_tnth"])
    tops = gpd.GeoDataFrame(tops, geometry="geometry", crs=tree_list.crs if getattr(tree_list, "crs", None) else crs)
    tops["dbh_cm"] = tops["fia_est_dbh_cm"]
    tops["is_training_data"] = False
    tops["stem_dbh_cm"] = np.nan
    tops["predicted_dbh_cm"] = np.nan
    # local model from stems
    if use_local:
        crowns = tl.copy()
        stems = treels_dbh_locations.to_crs(crowns.crs) if treels_dbh_locations.crs and crowns.crs else treels_dbh_locations
        stems = stems.rename(columns={c: f"stem_{c}" for c in stems.columns if c != "geometry" and not str(c).startswith("stem_")})
        joined = gpd.sjoin(crowns[["treeID", "tree_height_m", "tree_x", "tree_y", "geometry"]], stems[["stem_dbh_cm", "geometry"]], how="inner", predicate="intersects")
        joined["tree_height_m_tnth"] = np.round(joined["tree_height_m"].astype(float), 1).astype(str)
        p1 = predict_regional(fit, np.unique(np.round(joined["tree_height_m"].astype(float), 1)))
        p1["tree_height_m_tnth"] = np.round(p1["tree_height_m"], 1).astype(str)
        joined = joined.merge(p1.drop(columns=["tree_height_m"]), on="tree_height_m_tnth", how="inner")
        joined["stem_dbh_cm"] = pd.to_numeric(joined["stem_dbh_cm"], errors="coerce")
        joined["fia_est_dbh_pct_diff"] = (joined["stem_dbh_cm"] - joined["fia_est_dbh_cm"]).abs() / joined["fia_est_dbh_cm"]
        training = joined[(joined["stem_dbh_cm"].notna()) & (joined["stem_dbh_cm"] > 0) & (joined["stem_dbh_cm"] >= joined["fia_est_dbh_cm_lower"]) & (joined["stem_dbh_cm"] <= joined["fia_est_dbh_cm_upper"])]
        if len(training):
            training = training.sort_values(["treeID", "fia_est_dbh_pct_diff"]).drop_duplicates("treeID", keep="first")
            training = pd.DataFrame(training[["treeID", "stem_dbh_cm", "tree_height_m", "tree_x", "tree_y"]])
        if len(training) > 10:
            if dbh_model_local.strip().lower() == "rf":
                from .rf import rf_tune_model
                X = training[["tree_height_m", "tree_x", "tree_y"]]
                mod = rf_tune_model(X, training["stem_dbh_cm"], seed=21)
                def predict_local(df):
                    return mod.predict(df[["tree_height_m", "tree_x", "tree_y"]].astype(float).values)
                if out:
                    import joblib
                    joblib.dump(mod, out / "local_dbh_height_model.joblib")
            else:
                # Gamma GLM with log link in R; least squares on log(dbh) ~ height gives the same mean structure
                A = np.column_stack([np.ones(len(training)), training["tree_height_m"].to_numpy(float)])
                coef, *_ = np.linalg.lstsq(A, np.log(training["stem_dbh_cm"].to_numpy(float)), rcond=None)
                def predict_local(df):
                    return np.exp(coef[0] + coef[1] * df["tree_height_m"].to_numpy(float))
                if out:
                    (out / "local_dbh_height_model.json").write_text(json.dumps({"formula": "log(stem_dbh_cm) ~ 1 + tree_height_m", "intercept": float(coef[0]), "slope": float(coef[1]), "n": int(len(training))}, indent=2))
            others = pd.DataFrame(crowns.drop(columns="geometry"))
            others = others[~others["treeID"].isin(training["treeID"])][["treeID", "tree_height_m", "tree_x", "tree_y"]]
            others = others[others["tree_height_m"].notna()]
            others["predicted_dbh_cm"] = predict_local(others)
            tops = tops.drop(columns=["stem_dbh_cm", "predicted_dbh_cm", "is_training_data"])
            tops = tops.merge(training[["treeID", "stem_dbh_cm"]].assign(is_training_data=True), on="treeID", how="left")
            tops = tops.merge(others[["treeID", "predicted_dbh_cm"]], on="treeID", how="left")
            tops["is_training_data"] = tops["is_training_data"].fillna(False).astype(bool)
            tops["dbh_cm"] = tops["stem_dbh_cm"].fillna(tops["predicted_dbh_cm"]).fillna(tops["fia_est_dbh_cm"])
            tops = gpd.GeoDataFrame(tops, geometry="geometry", crs=crowns.crs)
            if log: log.info(f"Local DBH model ({dbh_model_local}) trained on {len(training)} stems; predicted {len(others):,} trees")
        else:
            if log: log.info("Insufficient data to estimate DBH using trees provided in `treels_dbh_locations`...\nReturning tree list with DBH estimates using FIA data instead.")
    tops["dbh_m"] = tops["dbh_cm"] / 100.0
    tops["radius_m"] = tops["dbh_m"] / 2.0
    tops["basal_area_m2"] = np.pi * tops["radius_m"] ** 2
    tops["basal_area_ft2"] = tops["basal_area_m2"] * 10.764
    tops["ptcld_extracted_dbh_cm"] = tops["stem_dbh_cm"]
    tops["ptcld_predicted_dbh_cm"] = tops["predicted_dbh_cm"]
    tops = tops.drop(columns=["stem_dbh_cm", "predicted_dbh_cm"])
    if vintage == 2016 and log:
        log.warning("Treemap 2022 data has not been downloaded. You are currently using Treemap 2016. Use get_treemap(force=True) to update.")
    return tops
