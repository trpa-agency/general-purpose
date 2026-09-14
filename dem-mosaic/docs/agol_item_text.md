# ArcGIS Online item text — Tahoe DEM geodatabase

Copy for the Tahoe Open Data (ArcGIS Hub) item that links to the Box download. Field names
match the ArcGIS Online item page. Written to the TRPA Style Guide: Oxford comma, one space
between sentences, numerals with units, "percent" spelled out, months abbreviated with dates.

Source of every fact below: `dem-mosaic/config.yaml` (`metadata.source_citations`), the run
outputs in `C:\GIS\dem-mosaic`, and the USGS project report dated Oct. 25, 2024. Update this
file and the item together whenever the mosaic is rebuilt.

---

## Title

Lake Tahoe Basin Elevation Models: Topobathymetric Mosaic and Source DEMs (2026)

## Summary (snippet)

A seamless 1 meter bare-earth elevation model of the Lake Tahoe Basin, from ridgeline to lake
bottom, packaged with the source elevation datasets it was built from.

## Description

### Overview

This file geodatabase contains a seamless bare-earth digital elevation model of the Lake Tahoe
Basin at 1 meter resolution, along with the source elevation datasets used to build it. The
mosaic joins the land surface and the lake bottom into a single continuous surface, from the
ridgelines of the Tahoe Basin to the deepest part of Lake Tahoe.

No single elevation dataset covers both the basin's terrain and its lakebed, so the Tahoe
Regional Planning Agency combined four independent surveys collected between 1998 and 2022.
Each dataset was projected to a common grid, aligned to a common vertical reference, compared
against its neighbors to measure and remove systematic offsets, and merged in a documented
priority order so that the best available data wins at every location. A companion raster
records which source supplied each cell.

### What is in the download

| Layer | Description |
|---|---|
| Elevation mosaic | Bare-earth elevation, land and lakebed, 1 meter, 32-bit float |
| Source identifier | Integer code naming the source that supplied each elevation cell |
| 2022 lidar DEM | The 2022 U.S. Geological Survey lidar bare-earth surface, basin wide |
| 2010 lidar DEM | The 2010 Lake Tahoe Basin lidar bare-earth surface |
| 2018 nearshore DEM | Topobathymetric lidar covering the shoreline and the shallow lake bottom |
| 1998 bathymetry | U.S. Geological Survey multibeam sonar survey of the deep lake |

Use the source identifier raster before drawing conclusions from any location. It tells you the
vintage and the original resolution behind every elevation value.

### Source data

- **2022 terrestrial lidar.** U.S. Geological Survey 3D Elevation Program, project
  CA_SierraNevada_B22, collected by NV5 Geospatial between Nov. 3, 2021 and Aug. 25, 2022 at
  Quality Level 1 and delivered at 0.5 meter. Tested accuracy for the project elevation models
  is 5.04 centimeters root mean square error in non-vegetated terrain. This survey supplies
  nearly all of the land surface in the mosaic.
- **2010 terrestrial lidar.** Lake Tahoe Basin lidar collected by Watershed Sciences between
  Aug. 11 and Aug. 24, 2010 under contract to TRPA and the U.S. Geological Survey, reported at
  3.5 centimeters root mean square error. It fills gaps in the 2022 coverage.
- **2018 nearshore topobathymetric lidar.** Green-wavelength lidar flown by Quantum Spatial for
  Spatial Informatics Group and TRPA between Sept. 9 and Sept. 16, 2018, which reaches roughly
  13 meters of water depth and supplies the shoreline transition and the shallow lake bottom.
- **1998 lake bathymetry.** U.S. Geological Survey multibeam sonar survey of Lake Tahoe
  conducted between Aug. 2 and Aug. 17, 1998 and gridded at 10 meters, published as Digital
  Data Series DDS-55. It supplies the deep lake.

### Specifications

| Property | Value |
|---|---|
| Cell size | 1 meter |
| Horizontal reference | NAD 1983 UTM Zone 10N (EPSG 26910) |
| Vertical datum | NAVD88, GEOID18, inherited from the 2022 lidar |
| Pixel type | 32-bit floating point |
| NoData value | -9999 |
| Extent | TRPA jurisdictional boundary combined with the lake at high water, buffered by 100 meters |
| Format | File geodatabase raster datasets |

### How the sources were combined

Sources were projected once each to the output grid using bilinear resampling, so no dataset
is resampled more than a single time. Vertical differences between datasets were measured at
random points on stable, low-slope ground, and the median difference was removed so that every
source sits on the vertical datum of the 2022 lidar. The measured shifts were small for the
lidar datasets and roughly 0.8 meters for the 1998 bathymetry.

Hydro-flattened water surfaces were removed from the terrestrial lidar inside the lake
boundary, so that the nearshore and sonar surveys supply the lake bottom while exposed beach
above the waterline is preserved. Where the 2018 nearshore lidar reaches its depth limit, the
mosaic blends into the 1998 bathymetry over 300 meters rather than cutting sharply between them.

### Limitations and fitness for use

**This dataset is not for navigation.**

- **The model mixes four vintages spanning 24 years.** Elevations reflect conditions at the
  time each source was flown or surveyed, not a single date. Consult the source identifier
  raster before using the model for change detection or volumetric analysis.
- **The deep lake is 10 meter sonar data resampled to a 1 meter grid.** The cell size does not
  imply 1 meter detail below roughly 13 meters of water depth.
- **The nearshore transition is the weakest part of the model.** In water shallower than about
  13 meters, the 1998 bathymetric grid disagrees with the 2018 lidar by an amount that grows
  with depth, approximately 3 meters for every 10 meters of depth. The published bathymetry does
  not document whether that band is interpolated or derived from an older survey. Elevations
  within the 300 meter blend between the two sources may be in error by several meters.
- **The vertical reference of the 1998 bathymetry was inferred, not documented.** Absolute
  elevations in the deep lake carry roughly 1 meter of uncertainty. Relative depths and lakebed
  morphology are unaffected.
- **Land elevations are bare earth.** Vegetation, buildings, and other structures have been
  removed by the original data producers.

### Contact

Tahoe Regional Planning Agency, GIS Program. Email gis@trpa.gov.

## Terms of use

This dataset is provided by the Tahoe Regional Planning Agency for public use without warranty
of any kind, express or implied, including accuracy, completeness, or fitness for a particular
purpose. It is not a survey product and is not suitable for navigation, engineering design, or
boundary determination. Source data are in the public domain and were produced by the U.S.
Geological Survey and by contractors to TRPA. Users are responsible for determining whether the
dataset is appropriate for their application, and should cite both this dataset and the original
source surveys.

## Credits (attribution)

Tahoe Regional Planning Agency. Source data: U.S. Geological Survey 3D Elevation Program
CA_SierraNevada_B22 lidar (NV5 Geospatial, 2021 to 2022); Lake Tahoe Basin lidar (Watershed
Sciences, 2010, for TRPA and the U.S. Geological Survey); Lake Tahoe nearshore topobathymetric
lidar (Quantum Spatial for Spatial Informatics Group and TRPA, 2018); and U.S. Geological Survey
Lake Tahoe multibeam bathymetry (Dartnell and Gardner, 1999, Digital Data Series DDS-55).

## Tags

elevation, DEM, digital elevation model, bare earth, bathymetry, topobathymetric, lidar,
multibeam sonar, terrain, lakebed, Lake Tahoe, Tahoe Basin, TRPA, 3DEP, open data

---

## Before publishing

- Swap the layer names in "What is in the download" for the actual dataset names in the
  geodatabase.
- Add the area supplied by each source if you want the figures public. They are in
  `area_by_source.csv` in the outputs folder, and the lake accounts for a little over a third
  of the model.
- State the geodatabase size and the Box link in the item, since Hub visitors cannot see either
  from the item page alone.
- The full technical record, including per-pair offset statistics and seam measurements, lives
  in `dem-mosaic/config.yaml` and the QA files in the outputs folder. Consider attaching the
  source identifier lookup table, `source_id_lookup.csv`, to the item.
