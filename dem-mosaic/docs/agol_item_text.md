# ArcGIS Online item text — Tahoe DEM geodatabase

Copy for the Tahoe Open Data (ArcGIS Hub) item that links to the Box download.

**How to paste.** The ArcGIS Online description editor has a source view, usually a `</>`
button in the toolbar. Open it and paste the HTML block below in one go; headings, bold text,
and lists come through intact. If you would rather work in the rich text editor, use the plain
text version instead and apply headings with the toolbar. Title, Summary, Credits, and Tags are
single-line fields, so paste those as plain text.

No tables are used. ArcGIS Online allows them, but Hub renders item descriptions in a narrow
column and tables break on phones.

Every fact below comes from `dem-mosaic/config.yaml`, the run outputs in `C:\GIS\dem-mosaic`,
and the USGS project report dated Oct. 25, 2024. Written to the TRPA Style Guide: Oxford comma,
one space between sentences, numerals with units, "percent" spelled out, months abbreviated with
specific dates. Update this file and the item together whenever the mosaic is rebuilt.

---

## 1. Title

```
Lake Tahoe Basin Elevation Models: Topobathymetric Mosaic and Source DEMs (2026)
```

## 2. Summary

```
A seamless 1 meter bare-earth elevation model of the Lake Tahoe Basin, from ridgeline to lake bottom, packaged with the source elevation datasets it was built from.
```

## 3. Description, HTML

Paste into the source view of the description editor.

```html
<h3>Overview</h3>
<p>This file geodatabase contains a seamless bare-earth digital elevation model of the Lake Tahoe Basin at 1 meter resolution, along with the source elevation datasets used to build it. The mosaic joins the land surface and the lake bottom into a single continuous surface, from the ridgelines of the Tahoe Basin to the deepest part of Lake Tahoe.</p>
<p>No single elevation dataset covers both the basin's terrain and its lakebed, so the Tahoe Regional Planning Agency combined four independent surveys collected between 1998 and 2022. Each dataset was projected to a common grid, aligned to a common vertical reference, compared against its neighbors to measure and remove systematic offsets, and merged in a documented priority order so that the best available data wins at every location. A companion raster records which source supplied each cell.</p>

<h3>What is in the download</h3>
<ul>
<li><strong>Elevation mosaic.</strong> Bare-earth elevation for land and lakebed, 1 meter, 32-bit floating point.</li>
<li><strong>Source identifier.</strong> Integer code naming the source that supplied each elevation cell.</li>
<li><strong>2022 lidar elevation model.</strong> The 2022 U.S. Geological Survey lidar bare-earth surface, basin wide.</li>
<li><strong>2010 lidar elevation model.</strong> The 2010 Lake Tahoe Basin lidar bare-earth surface.</li>
<li><strong>2018 nearshore elevation model.</strong> Topobathymetric lidar covering the shoreline and the shallow lake bottom.</li>
<li><strong>1998 lake bathymetry.</strong> U.S. Geological Survey multibeam sonar survey of the deep lake.</li>
</ul>
<p>Use the source identifier raster before drawing conclusions from any location. It tells you the vintage and the original resolution behind every elevation value.</p>

<h3>Source data</h3>
<ul>
<li><strong>2022 terrestrial lidar.</strong> U.S. Geological Survey 3D Elevation Program, project CA_SierraNevada_B22, collected by NV5 Geospatial between Nov. 3, 2021 and Aug. 25, 2022 at Quality Level 1 and delivered at 0.5 meter. Tested accuracy for the project elevation models is 5.04 centimeters root mean square error in non-vegetated terrain. This survey supplies nearly all of the land surface in the mosaic.</li>
<li><strong>2010 terrestrial lidar.</strong> Lake Tahoe Basin lidar collected by Watershed Sciences between Aug. 11 and Aug. 24, 2010 under contract to TRPA and the U.S. Geological Survey, reported at 3.5 centimeters root mean square error. It fills gaps in the 2022 coverage.</li>
<li><strong>2018 nearshore topobathymetric lidar.</strong> Green-wavelength lidar flown by Quantum Spatial for Spatial Informatics Group and TRPA between Sept. 9 and Sept. 16, 2018, which reaches roughly 13 meters of water depth and supplies the shoreline transition and the shallow lake bottom.</li>
<li><strong>1998 lake bathymetry.</strong> U.S. Geological Survey multibeam sonar survey of Lake Tahoe conducted between Aug. 2 and Aug. 17, 1998 and gridded at 10 meters, published as Digital Data Series DDS-55.</li>
</ul>

<h3>Specifications</h3>
<ul>
<li><strong>Cell size.</strong> 1 meter.</li>
<li><strong>Horizontal reference.</strong> NAD 1983 UTM Zone 10N, EPSG 26910.</li>
<li><strong>Vertical datum.</strong> NAVD88, GEOID18, inherited from the 2022 lidar.</li>
<li><strong>Pixel type.</strong> 32-bit floating point, NoData value -9999.</li>
<li><strong>Extent.</strong> The TRPA jurisdictional boundary combined with the lake at high water, buffered by 100 meters.</li>
<li><strong>Format.</strong> File geodatabase raster datasets.</li>
</ul>

<h3>How the sources were combined</h3>
<p>Sources were projected once each to the output grid using bilinear resampling, so no dataset is resampled more than a single time. Vertical differences between datasets were measured at random points on stable, low-slope ground, and the median difference was removed so that every source sits on the vertical datum of the 2022 lidar. The measured shifts were small for the lidar datasets and roughly 0.8 meters for the 1998 bathymetry.</p>
<p>Hydro-flattened water surfaces were removed from the terrestrial lidar inside the lake boundary, so that the nearshore and sonar surveys supply the lake bottom while exposed beach above the waterline is preserved. Where the 2018 nearshore lidar reaches its depth limit, the mosaic blends into the 1998 bathymetry over 300 meters rather than cutting sharply between them.</p>

<h3>Limitations and fitness for use</h3>
<p><strong>This dataset is not for navigation.</strong></p>
<ul>
<li><strong>The model mixes four vintages spanning 24 years.</strong> Elevations reflect conditions at the time each source was flown or surveyed, not a single date. Consult the source identifier raster before using the model for change detection or volumetric analysis.</li>
<li><strong>The deep lake is 10 meter sonar data resampled to a 1 meter grid.</strong> The cell size does not imply 1 meter detail below roughly 13 meters of water depth.</li>
<li><strong>The nearshore transition is the weakest part of the model.</strong> In water shallower than about 13 meters, the 1998 bathymetric grid disagrees with the 2018 lidar by an amount that grows with depth, approximately 3 meters for every 10 meters of depth. The published bathymetry does not document whether that band is interpolated or derived from an older survey. Elevations within the 300 meter blend between the two sources may be in error by several meters.</li>
<li><strong>The vertical reference of the 1998 bathymetry was inferred, not documented.</strong> Absolute elevations in the deep lake carry roughly 1 meter of uncertainty. Relative depths and lakebed morphology are unaffected.</li>
<li><strong>Land elevations are bare earth.</strong> Vegetation, buildings, and other structures have been removed by the original data producers.</li>
</ul>

<h3>Contact</h3>
<p>Tahoe Regional Planning Agency, GIS Program. Email <a href="mailto:gis@trpa.gov">gis@trpa.gov</a>.</p>
```

## 4. Description, plain text

Use this if you are working in the rich text editor instead of the source view. Paste it, then
select each section heading and apply a heading style, and select each list and apply bullets.

```
Overview

This file geodatabase contains a seamless bare-earth digital elevation model of the Lake Tahoe Basin at 1 meter resolution, along with the source elevation datasets used to build it. The mosaic joins the land surface and the lake bottom into a single continuous surface, from the ridgelines of the Tahoe Basin to the deepest part of Lake Tahoe.

No single elevation dataset covers both the basin's terrain and its lakebed, so the Tahoe Regional Planning Agency combined four independent surveys collected between 1998 and 2022. Each dataset was projected to a common grid, aligned to a common vertical reference, compared against its neighbors to measure and remove systematic offsets, and merged in a documented priority order so that the best available data wins at every location. A companion raster records which source supplied each cell.

What is in the download

Elevation mosaic. Bare-earth elevation for land and lakebed, 1 meter, 32-bit floating point.
Source identifier. Integer code naming the source that supplied each elevation cell.
2022 lidar elevation model. The 2022 U.S. Geological Survey lidar bare-earth surface, basin wide.
2010 lidar elevation model. The 2010 Lake Tahoe Basin lidar bare-earth surface.
2018 nearshore elevation model. Topobathymetric lidar covering the shoreline and the shallow lake bottom.
1998 lake bathymetry. U.S. Geological Survey multibeam sonar survey of the deep lake.

Use the source identifier raster before drawing conclusions from any location. It tells you the vintage and the original resolution behind every elevation value.

Source data

2022 terrestrial lidar. U.S. Geological Survey 3D Elevation Program, project CA_SierraNevada_B22, collected by NV5 Geospatial between Nov. 3, 2021 and Aug. 25, 2022 at Quality Level 1 and delivered at 0.5 meter. Tested accuracy for the project elevation models is 5.04 centimeters root mean square error in non-vegetated terrain. This survey supplies nearly all of the land surface in the mosaic.
2010 terrestrial lidar. Lake Tahoe Basin lidar collected by Watershed Sciences between Aug. 11 and Aug. 24, 2010 under contract to TRPA and the U.S. Geological Survey, reported at 3.5 centimeters root mean square error. It fills gaps in the 2022 coverage.
2018 nearshore topobathymetric lidar. Green-wavelength lidar flown by Quantum Spatial for Spatial Informatics Group and TRPA between Sept. 9 and Sept. 16, 2018, which reaches roughly 13 meters of water depth and supplies the shoreline transition and the shallow lake bottom.
1998 lake bathymetry. U.S. Geological Survey multibeam sonar survey of Lake Tahoe conducted between Aug. 2 and Aug. 17, 1998 and gridded at 10 meters, published as Digital Data Series DDS-55.

Specifications

Cell size. 1 meter.
Horizontal reference. NAD 1983 UTM Zone 10N, EPSG 26910.
Vertical datum. NAVD88, GEOID18, inherited from the 2022 lidar.
Pixel type. 32-bit floating point, NoData value -9999.
Extent. The TRPA jurisdictional boundary combined with the lake at high water, buffered by 100 meters.
Format. File geodatabase raster datasets.

How the sources were combined

Sources were projected once each to the output grid using bilinear resampling, so no dataset is resampled more than a single time. Vertical differences between datasets were measured at random points on stable, low-slope ground, and the median difference was removed so that every source sits on the vertical datum of the 2022 lidar. The measured shifts were small for the lidar datasets and roughly 0.8 meters for the 1998 bathymetry.

Hydro-flattened water surfaces were removed from the terrestrial lidar inside the lake boundary, so that the nearshore and sonar surveys supply the lake bottom while exposed beach above the waterline is preserved. Where the 2018 nearshore lidar reaches its depth limit, the mosaic blends into the 1998 bathymetry over 300 meters rather than cutting sharply between them.

Limitations and fitness for use

This dataset is not for navigation.

The model mixes four vintages spanning 24 years. Elevations reflect conditions at the time each source was flown or surveyed, not a single date. Consult the source identifier raster before using the model for change detection or volumetric analysis.
The deep lake is 10 meter sonar data resampled to a 1 meter grid. The cell size does not imply 1 meter detail below roughly 13 meters of water depth.
The nearshore transition is the weakest part of the model. In water shallower than about 13 meters, the 1998 bathymetric grid disagrees with the 2018 lidar by an amount that grows with depth, approximately 3 meters for every 10 meters of depth. The published bathymetry does not document whether that band is interpolated or derived from an older survey. Elevations within the 300 meter blend between the two sources may be in error by several meters.
The vertical reference of the 1998 bathymetry was inferred, not documented. Absolute elevations in the deep lake carry roughly 1 meter of uncertainty. Relative depths and lakebed morphology are unaffected.
Land elevations are bare earth. Vegetation, buildings, and other structures have been removed by the original data producers.

Contact

Tahoe Regional Planning Agency, GIS Program. Email gis@trpa.gov.
```

## 5. Terms of Use, HTML

```html
<p>This dataset is provided by the Tahoe Regional Planning Agency for public use without warranty of any kind, express or implied, including accuracy, completeness, or fitness for a particular purpose. It is not a survey product and is not suitable for navigation, engineering design, or boundary determination.</p>
<p>Source data are in the public domain and were produced by the U.S. Geological Survey and by contractors to TRPA. Users are responsible for determining whether the dataset is appropriate for their application, and should cite both this dataset and the original source surveys.</p>
```

## 6. Terms of Use, plain text

```
This dataset is provided by the Tahoe Regional Planning Agency for public use without warranty of any kind, express or implied, including accuracy, completeness, or fitness for a particular purpose. It is not a survey product and is not suitable for navigation, engineering design, or boundary determination.

Source data are in the public domain and were produced by the U.S. Geological Survey and by contractors to TRPA. Users are responsible for determining whether the dataset is appropriate for their application, and should cite both this dataset and the original source surveys.
```

## 7. Credits (attribution)

```
Tahoe Regional Planning Agency. Source data: U.S. Geological Survey 3D Elevation Program CA_SierraNevada_B22 lidar (NV5 Geospatial, 2021 to 2022); Lake Tahoe Basin lidar (Watershed Sciences, 2010, for TRPA and the U.S. Geological Survey); Lake Tahoe nearshore topobathymetric lidar (Quantum Spatial for Spatial Informatics Group and TRPA, 2018); and U.S. Geological Survey Lake Tahoe multibeam bathymetry (Dartnell and Gardner, 1999, Digital Data Series DDS-55).
```

## 8. Tags

```
elevation, DEM, digital elevation model, bare earth, bathymetry, topobathymetric, lidar, multibeam sonar, terrain, lakebed, Lake Tahoe, Tahoe Basin, TRPA, 3DEP, open data
```

---

## Before publishing

- Swap the layer names under "What is in the download" for the actual dataset names in the
  geodatabase.
- Add the area supplied by each source if you want the figures public. They are in
  `area_by_source.csv` in the outputs folder, and the lake accounts for a little over a third
  of the model.
- State the geodatabase size and the Box link in the item, since Hub visitors cannot see either
  from the item page alone.
- Attach `source_id_lookup.csv` to the item. The source identifier raster is not usable without
  the code-to-source mapping.
- The full technical record, including per-pair offset statistics and seam measurements, lives
  in `dem-mosaic/config.yaml` and the QA files in the outputs folder.
