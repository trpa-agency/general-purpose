"""
c2t — Python port of the cloud2trees R package (Woolsey and Tinkham, USFS RMRS, v0.8.3, GPL-3).

    from src import c2t
    ans = c2t.cloud2trees(output_dir, input_las_dir, estimate_tree_dbh=True, ...)
    trees = c2t.trees_dbh(c2t.from_taos(taos), study_boundary=aoi)

See docs/C2T_PORT.md for the contract and the substitutions made for brms, lasR, ForestTools,
LadderFuelsR, and TreeLS.
"""
from .schema import check_tree_list, as_points, from_taos, ensure_treeid
from .ws import itd_ws_functions, lin_fn, exp_fn, log_fn, constant_fn, as_ws_function
from .cloud import cloud2raster, cloud2raster_tile, load_points, points_to_crowns, sample_crowns, reproject_las, list_las
from .itd import raster2trees, itd_tuning, locate_trees, mcws, write_raster2trees_ans
from .extdata import get_data, get_treemap, get_foresttype, get_landfire, find_ext_data
from .dbh import trees_dbh, fit_regional_model, predict_regional
from .stems import treels_stem_dbh
from .foresttype import trees_type
from .competition import trees_competition
from .hmd import trees_hmd
from .cbh import trees_cbh, ladderfuelsr_cbh
from .biomass import trees_biomass, trees_biomass_landfire, trees_biomass_cruz, trees_landfire_cbd
from .lanl import cloud2trees_to_lanl_trees
from .pipeline import cloud2trees

__version__ = "0.1.0"
__r_source__ = "georgewoolsey/cloud2trees 0.8.3 @ 8ab10b8"
