"""
c2t/schema.py — the tree list contract shared by every trees_*() function.

A tree list is a GeoDataFrame in the working CRS with treeID, tree_x, tree_y, tree_height_m,
crown_area_m2, and either Polygon (crown) or Point (top) geometry. See docs/C2T_PORT.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

REQUIRED = ("treeID", "tree_x", "tree_y", "tree_height_m")


def check_tree_list(gdf, need: tuple[str, ...] = ("tree_height_m",), geometry: str = "any") -> gpd.GeoDataFrame:
    """Validate a tree list. Raises ValueError with the R package's wording when a column is missing."""
    if not isinstance(gdf, (pd.DataFrame, gpd.GeoDataFrame)):
        raise ValueError("`tree_list` must be a DataFrame or GeoDataFrame")
    cols = set(gdf.columns)
    for c in need:
        if c not in cols:
            if c == "tree_height_m":
                raise ValueError("`tree_list` data must contain `tree_height_m` column.\nRename the height column if it exists and ensure it is in meters.")
            raise ValueError(f"`tree_list` data must contain `{c}` column.")
    if "tree_height_m" in cols:
        h = pd.to_numeric(gdf["tree_height_m"], errors="coerce")
        if h.isna().all() or (h.fillna(0) <= 0).all():
            raise ValueError("`tree_list` contains all missing `tree_height_m` data.\n   height is required.")
    if geometry != "any":
        if not isinstance(gdf, gpd.GeoDataFrame):
            raise ValueError(f"`tree_list` must be a GeoDataFrame with {geometry.upper()} geometry")
        gt = set(gdf.geom_type.unique())
        want = {"Polygon", "MultiPolygon"} if geometry == "polygon" else {"Point", "MultiPoint"}
        if not gt <= want:
            raise ValueError(f"`tree_list` data must be a GeoDataFrame with {geometry.upper()} geometry (see raster2trees())")
    return gdf


def as_points(tree_list, crs=None) -> gpd.GeoDataFrame:
    """
    check_spatial_points() in R: return a Point GeoDataFrame of tree tops. Polygon input is converted
    using tree_x, tree_y when present, otherwise the polygon centroid. A plain DataFrame needs tree_x,
    tree_y and a crs.
    """
    if isinstance(tree_list, gpd.GeoDataFrame) and len(tree_list) and tree_list.geom_type.isin(["Point"]).all():
        g = tree_list.copy()
        if "tree_x" not in g.columns:
            g["tree_x"] = g.geometry.x
            g["tree_y"] = g.geometry.y
        if crs is not None and g.crs is None:
            g = g.set_crs(crs)
        return g
    df = pd.DataFrame(tree_list).copy()
    use_crs = getattr(tree_list, "crs", None) or crs
    if "tree_x" not in df.columns or "tree_y" not in df.columns:
        if isinstance(tree_list, gpd.GeoDataFrame):
            cen = tree_list.geometry.centroid
            df["tree_x"] = cen.x.values
            df["tree_y"] = cen.y.values
        else:
            raise ValueError("`tree_list` needs `tree_x` and `tree_y` columns (or a geometry) to become spatial points")
    if "geometry" in df.columns:
        df = df.drop(columns=["geometry"])
    g = gpd.GeoDataFrame(df, geometry=[Point(xy) for xy in zip(df["tree_x"].values, df["tree_y"].values)], crs=use_crs)
    if g.crs is None:
        raise ValueError("Cannot proceed with blank CRS. Ensure that the `tree_list` data has a CRS or pass `crs`.")
    return g


def ensure_treeid(gdf) -> gpd.GeoDataFrame:
    """Add treeID as a string row number if missing; cast to str otherwise."""
    g = gdf.copy()
    if "treeID" not in g.columns:
        g.insert(0, "treeID", np.arange(1, len(g) + 1).astype(str))
    else:
        g["treeID"] = g["treeID"].astype(str)
    return g


def from_taos(taos: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Rename the 00b tree table (tree_id, x, y, height_m, crown_area_m2, ...) to the contract."""
    g = taos.rename(columns={"tree_id": "treeID", "x": "tree_x", "y": "tree_y", "height_m": "tree_height_m"}).copy()
    g["treeID"] = g["treeID"].astype(str)
    if "crown_area_m2" not in g.columns:
        g["crown_area_m2"] = np.nan
    return g


def drop_cols(gdf, cols) -> gpd.GeoDataFrame:
    """Drop any of cols that exist (R: select(-any_of(...)))."""
    return gdf.drop(columns=[c for c in cols if c in gdf.columns])
