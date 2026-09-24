"""
c2t/competition.py — neighbourhood competition metrics. Port of trees_competition() from cloud2trees.

Per tree top, within competition_buffer_m (5 m): n_trees (the tree itself included, as the R spatial join
counts it) and the tallest tree; comp_trees_per_ha = n_trees / area * 10000 where area is the buffer area
clipped to study_boundary when given (R divides by 1 when no boundary, which is what the R code does even
though it reads like a per-hectare density); comp_relative_tree_height = height / max height in the
buffer; comp_dist_to_nearest_m = distance to the nearest other tree within search_dist_max (10 m), or
search_dist_max when none.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from . import schema

COMP_COLS = ("comp_trees_per_ha", "comp_relative_tree_height", "comp_dist_to_nearest_m")


def trees_competition(tree_list, crs=None, competition_buffer_m: float = 5.0, study_boundary=None, search_dist_max: float = 10.0):
    schema.check_tree_list(tree_list, need=("tree_height_m",))
    tl = tree_list.copy()
    tl["tree_height_m"] = pd.to_numeric(tl["tree_height_m"], errors="coerce")
    tops = schema.as_points(schema.ensure_treeid(tl), crs)
    tops = schema.drop_cols(tops, COMP_COLS)
    xy = np.column_stack([tops.geometry.x.to_numpy(), tops.geometry.y.to_numpy()])
    h = tops["tree_height_m"].to_numpy(float)
    tree = cKDTree(xy)
    neigh = tree.query_ball_point(xy, r=competition_buffer_m)
    n_trees = np.array([len(n) for n in neigh])
    hmax = np.array([np.nanmax(h[n]) if len(n) else np.nan for n in neigh])
    if study_boundary is not None:
        import geopandas as gpd
        sb = study_boundary.to_crs(tops.crs).geometry.union_all()
        area_in = tops.geometry.buffer(competition_buffer_m).intersection(sb).area.to_numpy()
        area_in = np.where(area_in > 0, area_in, np.nan)
    else:
        area_in = np.ones(len(tops))
    d, i = tree.query(xy, k=2, distance_upper_bound=search_dist_max)
    nearest = np.where(np.isfinite(d[:, 1]), d[:, 1], search_dist_max)
    tops["comp_trees_per_ha"] = n_trees / np.where(np.isfinite(area_in), area_in, 1.0) * 10000.0
    tops["comp_relative_tree_height"] = h / hmax
    tops["comp_dist_to_nearest_m"] = nearest
    # hand the columns back on the caller's geometry (crowns stay crowns)
    out = schema.drop_cols(tree_list.copy(), COMP_COLS)
    out = out.merge(pd.DataFrame(tops.drop(columns="geometry"))[["treeID", *COMP_COLS]], on="treeID", how="left") if "treeID" in out.columns else tops
    return type(tree_list)(out, geometry="geometry", crs=getattr(tree_list, "crs", None)) if hasattr(tree_list, "geometry") else out
