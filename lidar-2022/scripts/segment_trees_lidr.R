# scripts/segment_trees_lidr.R
# Second-pass individual tree segmentation from the height-normalized point cloud (LAZ_Basin_HAG),
# for validation against the CHM watershed pass and for plot footprints / LITIDA training areas.
#
# Run:  Rscript scripts/segment_trees_lidr.R <laz_dir> <out_dir> [aoi.gpkg] [workers]
#   laz_dir  folder of *_hag.laz written by 00a (z = HAG on the chunked path; HeightAboveGround extra dim otherwise)
#   out_dir  per-tile *_trees_lidr.gpkg and *_crowns_lidr.gpkg, plus trees_lidr_all.gpkg
#   aoi      optional polygon file; only tiles intersecting it are processed (e.g. plot buffers for validation)
#   workers  parallel tiles (default 3)
#
# Needs: install.packages(c("lidR", "sf", "future", "terra"))

suppressPackageStartupMessages({ library(lidR); library(sf); library(future) })
args <- commandArgs(trailingOnly = TRUE)
laz_dir <- args[1]; out_dir <- args[2]
aoi <- if (length(args) >= 3 && nzchar(args[3])) st_read(args[3], quiet = TRUE) else NULL
workers <- if (length(args) >= 4) as.integer(args[4]) else 3L
dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)

# Parameters mirrored from config.yaml taos:
hmin <- 3                                      # min_tree_height_m
ws <- function(h) { w <- 3 + 0.2 * h; w[h < 5] <- 3; w[h > 25] <- 9; w }   # window_m_by_height as a smooth function
crown_floor <- 0.5                             # crown_floor_frac (Dalponte th_tree, th_seed, th_cr)

ctg <- readLAScatalog(laz_dir, select = "xyzrncE")   # E: extra bytes (HeightAboveGround) if present
if (!is.null(aoi)) {
  ctg <- catalog_intersect(ctg, aoi)
  message("Tiles intersecting AOI: ", nrow(ctg))
}
opt_chunk_buffer(ctg) <- 25                    # same neighbour pad as the CHM pass
opt_output_files(ctg) <- file.path(out_dir, "{ORIGINALFILENAME}_trees_lidr")
opt_laz_compression(ctg) <- TRUE
plan(multisession, workers = workers)

segment_one <- function(las) {
  # if HeightAboveGround exists use it as Z; otherwise the tile already has z = HAG (chunked path in 00a)
  if ("HeightAboveGround" %in% names(las@data)) las@data$Z <- las@data$HeightAboveGround
  las <- filter_poi(las, Z >= 0 & Z <= 80 & !(Classification %in% c(6, 7, 9, 17, 18)))
  if (is.empty(las)) return(NULL)
  chm <- rasterize_canopy(las, res = 1, algorithm = p2r(0.2, na.fill = tin()))
  ttops <- locate_trees(chm, lmf(ws, hmin = hmin))
  if (nrow(ttops) == 0) return(NULL)
  las <- segment_trees(las, dalponte2016(chm, ttops, th_tree = hmin, th_seed = crown_floor, th_cr = crown_floor, max_cr = 12))
  crowns <- crown_metrics(las, func = .stdtreemetrics, geom = "concave")
  crowns
}

res <- catalog_map(ctg, segment_one)
trees <- do.call(rbind, Filter(Negate(is.null), res))
st_write(trees, file.path(out_dir, "trees_lidr_all.gpkg"), delete_dsn = TRUE, quiet = TRUE)
message("Wrote ", nrow(trees), " trees to ", file.path(out_dir, "trees_lidr_all.gpkg"))
