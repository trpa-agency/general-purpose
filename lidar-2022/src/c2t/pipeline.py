"""
c2t/pipeline.py — the all-in-one runner. Port of cloud2trees().

Order (as R): cloud2raster -> raster2trees -> trees_competition -> treels_stem_dbh -> trees_dbh -> trees_cbh
-> trees_type -> trees_hmd -> trees_biomass -> write final_detected_crowns.gpkg, final_detected_tree_tops.gpkg,
fia_foresttype_raster.tif, stand_cell_data_*.csv, processed_tracking_data.csv into
<output_dir>/point_cloud_processing_delivery. Each optional step is wrapped like R's purrr::safely: a
failure is logged and the run continues without that step's columns.

Every argument keeps the R name and default so the config `c2t:` section reads like the R call.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import schema
from .cloud import cloud2raster
from .itd import raster2trees, write_raster2trees_ans
from .ws import itd_ws_functions, as_ws_function


def _safe(step_name: str, fn, logger, *args, **kw):
    """Run a step; on failure log it and return (None, message) so the pipeline continues (R: purrr::safely)."""
    try:
        return fn(*args, **kw), None
    except Exception as e:  # noqa: BLE001
        msg = f"{step_name} failed: {e}"
        if logger:
            logger.error(msg); logger.debug(traceback.format_exc())
        return None, msg


def cloud2trees(output_dir, input_las_dir, input_treemap_dir=None, input_foresttype_dir=None, input_landfire_dir=None,
                accuracy_level: int = 2, keep_intrmdt: bool = False, dtm_res_m: float = 1.0, chm_res_m: float = 0.25,
                min_height: float = 2.0, max_height: float = 70.0, noise_level: int = 2, ws=None,
                estimate_tree_dbh: bool = False, max_dbh: float = 2.0, dbh_model_regional: str = "cr", dbh_model_local: str = "lin",
                estimate_dbh_from_cloud: bool = False, estimate_tree_competition: bool = False, competition_buffer_m: float = 5.0,
                competition_max_search_dist_m: float = 10.0, estimate_tree_type: bool = False, type_max_search_dist_m: float = 1000.0,
                estimate_tree_hmd: bool = False, hmd_tree_sample_n=None, hmd_tree_sample_prop=None, hmd_estimate_missing_hmd: bool = False,
                estimate_biomass_method=None, biomass_max_crown_kg_per_m3: float = 2.0,
                estimate_tree_cbh: bool = False, cbh_tree_sample_n=None, cbh_tree_sample_prop=None, cbh_which_cbh: str = "lowest",
                cbh_estimate_missing_cbh: bool = False, cbh_min_vhp_n: int = 3, cbh_voxel_grain_size_m: float = 1.0,
                cbh_dist_btwn_bins_m: float = 1.0, cbh_min_fuel_layer_ht_m: float = 1.0, cbh_lad_pct_gap: float = 25,
                cbh_lad_pct_base: float = 25, cbh_num_jump_steps: int = 1, cbh_min_lad_pct: float = 10, cbh_frst_layer_min_ht_m: float = 1.0,
                overwrite: bool = True, ground: str = "existing", ground_class: int = 2, workers: int = 1, crs=None,
                min_crown_area: float = 0.1, study_boundary=None, log=None) -> dict:
    """
    Returns dict(crowns_sf, treetops_sf, dtm_rast (path), chm_rast (path), foresttype_rast (path or None),
    delivery_dir, tracking (DataFrame), errors (list)).
    """
    t = {"start": time.time()}
    ws_fn = as_ws_function(ws if ws is not None else itd_ws_functions()["log_fn"])
    ws_name = ws if isinstance(ws, str) else getattr(ws_fn, "__name__", "custom")
    errors = []
    out = Path(output_dir)
    # 1 cloud2raster
    if log: log.info("starting cloud2raster() step")
    c2r = cloud2raster(out, input_las_dir, dtm_res_m=dtm_res_m, chm_res_m=chm_res_m, min_height=min_height, max_height=max_height,
                       accuracy_level=accuracy_level, noise_level=noise_level, ground=ground, ground_class=ground_class,
                       keep_intrmdt=keep_intrmdt, write_normalized=True, overwrite=overwrite, workers=workers, crs=crs, log=log)
    delivery = Path(c2r["delivery_dir"]); norm_dir = c2r["norm_dir"]
    t["cloud2raster"] = time.time()
    # 2 raster2trees
    if log: log.info("starting raster2trees() step")
    crowns = raster2trees(c2r["chm_path"], outfolder=None, ws=ws_fn, min_height=min_height, min_crown_area=min_crown_area, log=log)
    if log: log.info(f"raster2trees: {len(crowns):,} trees")
    t["raster2trees"] = time.time()
    # 3 competition
    if estimate_tree_competition:
        from .competition import trees_competition
        ans, err = _safe("trees_competition", trees_competition, log, crowns, competition_buffer_m=competition_buffer_m,
                         study_boundary=study_boundary, search_dist_max=competition_max_search_dist_m)
        if ans is not None: crowns = ans
        else: errors.append(err)
    t["trees_competition"] = time.time()
    # 4 stems from the cloud
    stems = None
    if estimate_tree_dbh and estimate_dbh_from_cloud:
        from .stems import treels_stem_dbh
        stems, err = _safe("treels_stem_dbh", treels_stem_dbh, log, norm_dir, max_dbh_cm=max_dbh * 100.0, crs=crowns.crs,
                           outfolder=str(delivery / "stems"), log=log)
        if err: errors.append(err)
    t["treels_stem_dbh"] = time.time()
    # 5 dbh
    if estimate_tree_dbh:
        from .dbh import trees_dbh
        ans, err = _safe("trees_dbh", trees_dbh, log, crowns, study_boundary=study_boundary, dbh_model_regional=dbh_model_regional,
                         dbh_model_local=dbh_model_local, treels_dbh_locations=stems, input_treemap_dir=input_treemap_dir,
                         outfolder=str(delivery), log=log)
        if ans is not None:
            dbh_cols = [c for c in ans.columns if c not in crowns.columns and c != "geometry"]
            crowns = crowns.merge(pd.DataFrame(ans.drop(columns="geometry"))[["treeID", *dbh_cols]], on="treeID", how="left")
        else:
            errors.append(err)
    t["trees_dbh"] = time.time()
    # 6 cbh
    if estimate_tree_cbh:
        from .cbh import trees_cbh
        ans, err = _safe("trees_cbh", trees_cbh, log, crowns, norm_dir, tree_sample_n=cbh_tree_sample_n if cbh_tree_sample_n else 333,
                         tree_sample_prop=cbh_tree_sample_prop, which_cbh=cbh_which_cbh, estimate_missing_cbh=cbh_estimate_missing_cbh,
                         voxel_grain_size_m=cbh_voxel_grain_size_m, dist_btwn_bins_m=cbh_dist_btwn_bins_m, min_fuel_layer_ht_m=cbh_min_fuel_layer_ht_m,
                         lad_pct_gap=cbh_lad_pct_gap, lad_pct_base=cbh_lad_pct_base, outfolder=str(delivery), log=log,
                         min_vhp_n=cbh_min_vhp_n, num_jump_steps=cbh_num_jump_steps, min_lad_pct=cbh_min_lad_pct, frst_layer_min_ht_m=cbh_frst_layer_min_ht_m)
        if ans is not None: crowns = _take_cols(crowns, ans, ["tree_cbh_m", "is_training_cbh"])
        else: errors.append(err)
    t["trees_cbh"] = time.time()
    # 7 type
    ft_rast_path = None
    if estimate_tree_type:
        from .foresttype import trees_type, write_foresttype_raster
        ans, err = _safe("trees_type", trees_type, log, crowns, study_boundary=study_boundary, input_foresttype_dir=input_foresttype_dir,
                         max_search_dist_m=type_max_search_dist_m, log=log)
        if ans is not None:
            crowns, rast = ans
            ft_rast_path = delivery / "fia_foresttype_raster.tif"
            write_foresttype_raster(rast, ft_rast_path)
        else:
            errors.append(err)
    t["trees_type"] = time.time()
    # 8 hmd
    if estimate_tree_hmd:
        from .hmd import trees_hmd
        ans, err = _safe("trees_hmd", trees_hmd, log, crowns, norm_dir, tree_sample_n=hmd_tree_sample_n, tree_sample_prop=hmd_tree_sample_prop,
                         estimate_missing_hmd=hmd_estimate_missing_hmd, outfolder=str(delivery), log=log)
        if ans is not None: crowns = _take_cols(crowns, ans, ["max_crown_diam_height_m", "is_training_hmd"])
        else: errors.append(err)
    t["trees_hmd"] = time.time()
    # 9 biomass
    if estimate_biomass_method:
        from .biomass import trees_biomass
        ans, err = _safe("trees_biomass", trees_biomass, log, crowns, method=estimate_biomass_method, study_boundary=study_boundary,
                         input_landfire_dir=input_landfire_dir, input_foresttype_dir=input_foresttype_dir,
                         max_crown_kg_per_m3=biomass_max_crown_kg_per_m3, log=log)
        if ans is not None:
            new = [c for c in ans.columns if c not in crowns.columns and c != "geometry"]
            crowns = _take_cols(crowns, ans, new)
            for key, name in (("stand_cell_data_landfire", "stand_cell_data_landfire.csv"), ("stand_cell_data_cruz", "stand_cell_data_cruz.csv"), ("stand_cell_data", "stand_cell_data.csv")):
                if key in getattr(ans, "attrs", {}):
                    pd.DataFrame(ans.attrs[key]).to_csv(delivery / name, index=False)
        else:
            errors.append(err)
    t["trees_biomass"] = time.time()
    # 10 write
    import geopandas as gpd
    crowns = gpd.GeoDataFrame(crowns, geometry="geometry", crs=crowns.crs)
    files = write_raster2trees_ans(crowns, delivery)
    treetops = gpd.GeoDataFrame(crowns.drop(columns="geometry"), geometry=gpd.points_from_xy(crowns["tree_x"], crowns["tree_y"]), crs=crowns.crs)
    t["write"] = time.time()
    ctg = c2r["ctg"]
    tracking = pd.DataFrame([{
        "processing_date": datetime.now().isoformat(timespec="seconds"),
        "number_of_points": int(ctg["n_points"].sum()) if "n_points" in ctg.columns else np.nan,
        "las_area_m2": float(ctg.geometry.union_all().area) if len(ctg) else np.nan,
        "n_trees": int(len(crowns)),
        "timer_cloud2raster_mins": (t["cloud2raster"] - t["start"]) / 60, "timer_raster2trees_mins": (t["raster2trees"] - t["cloud2raster"]) / 60,
        "timer_trees_competition_mins": (t["trees_competition"] - t["raster2trees"]) / 60, "timer_treels_stem_dbh_mins": (t["treels_stem_dbh"] - t["trees_competition"]) / 60,
        "timer_trees_dbh_mins": (t["trees_dbh"] - t["treels_stem_dbh"]) / 60, "timer_trees_cbh_mins": (t["trees_cbh"] - t["trees_dbh"]) / 60,
        "timer_trees_type_mins": (t["trees_type"] - t["trees_cbh"]) / 60, "timer_trees_hmd_mins": (t["trees_hmd"] - t["trees_type"]) / 60,
        "timer_trees_biomass_mins": (t["trees_biomass"] - t["trees_hmd"]) / 60, "timer_write_data_mins": (t["write"] - t["trees_biomass"]) / 60,
        "timer_total_time_mins": (t["write"] - t["start"]) / 60,
        "sttng_input_las_dir": str(input_las_dir if not isinstance(input_las_dir, (list, tuple)) else input_las_dir[0]),
        "sttng_accuracy_level": accuracy_level, "sttng_dtm_res_m": dtm_res_m, "sttng_chm_res_m": chm_res_m, "sttng_min_height": min_height,
        "sttng_max_height": max_height, "sttng_noise_level": noise_level, "sttng_ws": ws_name, "sttng_ground": ground,
        "sttng_estimate_tree_dbh": estimate_tree_dbh, "sttng_max_dbh": max_dbh, "sttng_dbh_model_regional": dbh_model_regional,
        "sttng_dbh_model_local": dbh_model_local, "sttng_estimate_dbh_from_cloud": estimate_dbh_from_cloud,
        "sttng_estimate_tree_competition": estimate_tree_competition, "sttng_competition_buffer_m": competition_buffer_m,
        "sttng_competition_max_search_dist_m": competition_max_search_dist_m, "sttng_estimate_tree_type": estimate_tree_type,
        "sttng_type_max_search_dist_m": type_max_search_dist_m, "sttng_estimate_tree_hmd": estimate_tree_hmd,
        "sttng_hmd_tree_sample_n": hmd_tree_sample_n, "sttng_hmd_tree_sample_prop": hmd_tree_sample_prop, "sttng_hmd_estimate_missing_hmd": hmd_estimate_missing_hmd,
        "sttng_estimate_biomass_method": estimate_biomass_method if isinstance(estimate_biomass_method, str) or estimate_biomass_method is None else ",".join(estimate_biomass_method),
        "sttng_biomass_max_crown_kg_per_m3": biomass_max_crown_kg_per_m3, "sttng_estimate_tree_cbh": estimate_tree_cbh,
        "sttng_cbh_tree_sample_n": cbh_tree_sample_n, "sttng_cbh_tree_sample_prop": cbh_tree_sample_prop, "sttng_cbh_which_cbh": cbh_which_cbh,
        "sttng_cbh_estimate_missing_cbh": cbh_estimate_missing_cbh, "sttng_cbh_min_vhp_n": cbh_min_vhp_n, "sttng_cbh_voxel_grain_size_m": cbh_voxel_grain_size_m,
        "sttng_cbh_dist_btwn_bins_m": cbh_dist_btwn_bins_m, "sttng_cbh_min_fuel_layer_ht_m": cbh_min_fuel_layer_ht_m, "sttng_cbh_lad_pct_gap": cbh_lad_pct_gap,
        "sttng_cbh_lad_pct_base": cbh_lad_pct_base, "sttng_cbh_num_jump_steps": cbh_num_jump_steps, "sttng_cbh_min_lad_pct": cbh_min_lad_pct,
        "sttng_cbh_frst_layer_min_ht_m": cbh_frst_layer_min_ht_m, "errors": " | ".join(errors),
    }])
    tracking.to_csv(delivery / "processed_tracking_data.csv", index=False)
    if log:
        log.info(f"cloud2trees() total time {(t['write'] - t['start']) / 60:.1f} min for {len(crowns):,} trees; errors: {len(errors)}")
    return {"crowns_sf": crowns, "treetops_sf": treetops, "dtm_rast": c2r["dtm_path"], "chm_rast": c2r["chm_path"],
            "foresttype_rast": str(ft_rast_path) if ft_rast_path else None, "delivery_dir": str(delivery), "norm_dir": norm_dir,
            "tracking": tracking, "errors": errors, "files": files}


def _take_cols(base, ans, cols):
    """Merge selected columns from a step's result onto the running crowns table by treeID."""
    cols = [c for c in cols if c in ans.columns]
    base = base.drop(columns=[c for c in cols if c in base.columns])
    right = pd.DataFrame(ans.drop(columns="geometry") if "geometry" in ans.columns else ans)[["treeID", *cols]]
    merged = base.merge(right, on="treeID", how="left")
    import geopandas as gpd
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=base.crs)
