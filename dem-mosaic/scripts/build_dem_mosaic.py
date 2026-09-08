"""Build the Tahoe bare-earth DEM mosaic (land + lake bottom) from four SDE rasters.

Usage (arcgispro-py3):
    python build_dem_mosaic.py                       # all steps, full basin
    python build_dem_mosaic.py --steps 1-3 --test    # inventory, standardize, overlap QA on test_extent
    python build_dem_mosaic.py --steps 6-8 --force   # rebuild mosaic and exports, reuse earlier steps
    python build_dem_mosaic.py --steps 2,5           # comma lists work too

Steps
    1 inventory     read CRS / units / extent of each source -> source_inventory.csv
    2 standardize   project + resample onto the target grid, ft->m
    3 overlap_qa    solve vertical offsets along qa.offset_chain -> overlap_qa.csv, resolved_offsets.yaml
    4 zones         land / water zone raster from TRPA boundary and lake polygon
    5 clean         apply offsets, strip water surface from terrestrial lidar, floor green lidar, clip bathy

Datum reconciliation
    Nobody has to know the absolute vertical datum of each product. Step 3 takes qa.reference
    (the 2022 lidar) as the vertical truth and, for every other source, measures the median
    of (source - already-aligned source) over stable ground: land pairs on slopes below
    qa.max_slope_deg and outside the lake polygon, underwater pairs inside it. The negated
    median becomes that source's offset. Sources with vertical_offset_m: auto in the config
    pick the solved value up in step 5; a number in the config overrides it. The output DEM
    is in the reference product's vertical datum. A TILT flag in overlap_qa.csv means the
    difference varies across the overlap and a constant shift is not enough.
    6 mosaic        first-valid-source per zone + source-ID raster
    7 export        COG GeoTIFFs: DEM, source ID, hillshade, plus source_id_lookup.csv
    8 result_qa     area by source, holes, seam step statistics

Every step is idempotent: outputs already present in the scratch gdb are reused unless
--force is given, and --force only applies to the steps selected. A step that needs an
earlier step's output builds it on demand if missing, so a crashed run resumes from
wherever it died.

--test switches to target.test_extent from the config, prefixes all intermediates with
"<name_prefix>test_" and suffixes exported files with "_test", so a test run never
collides with a full-basin run in the same scratch gdb.

SDE is READ ONLY. This script reads through Raster.sde and Vector.sde and never writes back.
All parameters live in ../config.yaml.
"""

import argparse
import logging
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
import arcpy
from arcpy.sa import (Con, IsNull, SetNull, EucDistance, FocalStatistics, NbrRectangle,
                      Hillshade, Int, Slope, ExtractMultiValuesToPoints)

FT_TO_M = 0.3048  # international foot; US survey foot differs by ~4 mm at Tahoe elevations
STEPS = {1: "inventory", 2: "standardize", 3: "overlap_qa", 4: "zones",
         5: "clean", 6: "mosaic", 7: "export", 8: "result_qa"}
log = logging.getLogger("dem_mosaic")


# --------------------------------------------------------------------------- helpers
def parse_steps(spec):
    if spec in (None, "", "all"):
        return sorted(STEPS)
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    bad = out - set(STEPS)
    if bad:
        raise SystemExit(f"unknown steps {sorted(bad)}; valid are 1-8")
    return sorted(out)


def setup_logging(log_file):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    for h in (logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)


def timed(fn):
    def wrapper(self, *a, **k):
        t0 = time.time()
        log.info("=== %s start", fn.__name__)
        r = fn(self, *a, **k)
        log.info("=== %s done in %.1f min", fn.__name__, (time.time() - t0) / 60)
        return r
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# --------------------------------------------------------------------------- pipeline
class Pipeline:
    def __init__(self, cfg_path, test=False, force=False):
        self.cfg_path = Path(cfg_path)
        self.cfg = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.test = test
        self.force = force
        c = self.cfg

        self.pfx = c["project"].get("name_prefix", "dm_") + ("test_" if test else "")
        self.scratch = c["project"]["scratch_gdb"]
        self.out_dir = Path(c["outputs"]["dir"])
        self.out_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(c["project"]["log_file"])

        t = c["target"]
        self.target_sr = arcpy.SpatialReference(int(t["wkid"]))
        self.cell = float(t["cell_size_m"])
        self.nodata = t["nodata"]
        self.z_lo, self.z_hi = t["plausible_z_m"]
        self.src = c["sources"]
        self.snap_key = c["priority"]["land"][0]
        self._ext = None

        self._setup_env()
        self.active = {k: s for k, s in self.src.items()
                       if s["path"] != "TODO" and arcpy.Exists(s["path"])}
        missing = set(self.src) - set(self.active)
        if missing:
            log.warning("sources with unset or missing path, skipped: %s", sorted(missing))
        log.info("config %s | test=%s force=%s | active sources %s | prefix %s",
                 self.cfg_path, test, force, list(self.active), self.pfx)

    def _setup_env(self):
        if not arcpy.Exists(self.scratch):
            p = Path(self.scratch)
            arcpy.management.CreateFileGDB(str(p.parent), p.name)
        arcpy.CheckOutExtension("Spatial")
        arcpy.env.overwriteOutput = True
        arcpy.env.workspace = self.scratch
        arcpy.env.scratchWorkspace = self.scratch
        arcpy.env.parallelProcessingFactor = "75%"
        arcpy.env.compression = self.cfg["target"]["compression"]
        arcpy.env.pyramid = f"PYRAMIDS -1 BILINEAR {self.cfg['target']['compression']}"
        arcpy.env.cellSize = self.cell
        arcpy.env.outputCoordinateSystem = self.target_sr

    # ---- naming / existence -------------------------------------------------
    def n(self, name):
        """Prefixed name of a scratch-gdb item."""
        return self.pfx + name

    def path(self, name):
        return os.path.join(self.scratch, self.n(name))

    def exists(self, name):
        return arcpy.Exists(self.path(name))

    def stale(self, name):
        """True when a step output must be (re)built: missing, or --force for this step."""
        return self.force or not self.exists(name)

    def std_name(self, key):
        return self.n(f"{key}_std")

    def clean_name(self, key):
        return self.n(f"{key}_clean")

    def out_file(self, key):
        stem, ext = os.path.splitext(self.cfg["outputs"][key])
        return str(self.out_dir / f"{stem}{'_test' if self.test else ''}{ext}")

    # ---- lazily built dependencies (never force-rebuilt) --------------------
    def _fc(self, name, build):
        full = self.path(name)
        if not arcpy.Exists(full):
            log.info("building %s", self.n(name))
            build(full)
        return full

    @property
    def trpa(self):
        return self._fc("trpa_boundary_p", lambda o: arcpy.management.Project(
            self.cfg["boundaries"]["trpa_boundary"], o, self.target_sr))

    @property
    def lake(self):
        return self._fc("lake_hw_p", lambda o: arcpy.management.Project(
            self.cfg["boundaries"]["lake_high_water"], o, self.target_sr))

    @property
    def aoi_buf(self):
        def build(out):
            merged = self._fc("aoi_merge", lambda o: arcpy.management.Merge([self.trpa, self.lake], o))
            aoi = self._fc("aoi", lambda o: arcpy.management.Dissolve(merged, o))
            arcpy.analysis.Buffer(aoi, out, f"{self.cfg['target']['extent_buffer_m']} Meters",
                                  dissolve_option="ALL")
        return self._fc("aoi_buf", build)

    @property
    def lake_core(self):
        """Lake polygon eroded inward, clipped to the run extent so samples land on the run's rasters."""
        d = self.cfg["cleaning"]["offshore_erode_m"]

        def build(out):
            full = self._fc("lake_core_full", lambda o: arcpy.analysis.Buffer(self.lake, o, f"-{d} Meters"))
            e = self.ensure_extent()
            box = arcpy.Polygon(arcpy.Array([arcpy.Point(e.XMin, e.YMin), arcpy.Point(e.XMin, e.YMax),
                                             arcpy.Point(e.XMax, e.YMax), arcpy.Point(e.XMax, e.YMin),
                                             arcpy.Point(e.XMin, e.YMin)]), self.target_sr)
            arcpy.analysis.Clip(full, box, out)
        return self._fc("lake_core_clip", build)

    def ensure_extent(self):
        """Set arcpy.env.extent to the AOI (or test box), rounded outward to whole cells."""
        if self._ext is not None:
            return self._ext
        if self.test:
            box = self.cfg["target"].get("test_extent")
            if not box:
                raise SystemExit("--test given but target.test_extent is null in config")
            ext = arcpy.Extent(*box)
            log.warning("TEST EXTENT in use: %s. Outputs are a subset, not the basin.", box)
        else:
            ext = arcpy.Describe(self.aoi_buf).extent
        c = self.cell
        ext = arcpy.Extent(math.floor(ext.XMin / c) * c, math.floor(ext.YMin / c) * c,
                           math.ceil(ext.XMax / c) * c, math.ceil(ext.YMax / c) * c)
        arcpy.env.extent = ext
        n_cells = (ext.XMax - ext.XMin) * (ext.YMax - ext.YMin) / (c * c)
        log.info("extent %.0f %.0f %.0f %.0f; %.2e cells per raster (%.1f GB float32 uncompressed)",
                 ext.XMin, ext.YMin, ext.XMax, ext.YMax, n_cells, n_cells * 4 / 1e9)
        self._ext = ext
        return ext

    @property
    def snap(self):
        s = self.cfg["target"].get("snap_raster")
        if s:
            return s
        full = self.path(f"{self.snap_key}_std")
        return full if arcpy.Exists(full) else None

    def ensure_grid(self):
        """Extent + snap raster, required by every step after standardize."""
        self.ensure_extent()
        if not self.snap:
            raise SystemExit(f"snap raster {self.std_name(self.snap_key)} missing; run step 2 first")
        arcpy.env.snapRaster = self.snap
        arcpy.env.cellSize = self.cell

    def mask(self, name, fc):
        """0/1 raster of a polygon feature class on the current grid."""
        full = self.path(name)
        if not arcpy.Exists(full):
            self.ensure_grid()
            tmp = self.path(name + "_tmp")
            arcpy.conversion.FeatureToRaster(fc, arcpy.Describe(fc).OIDFieldName, tmp, self.cell)
            Con(IsNull(arcpy.Raster(tmp)), 0, 1).save(full)
            arcpy.management.Delete(tmp)
        return arcpy.Raster(full)

    @property
    def aoi_r(self):
        return self.mask("aoi_mask", self.aoi_buf)

    @property
    def lake_r(self):
        return self.mask("lake_mask", self.lake)

    @property
    def other_r(self):
        other = self.cfg["boundaries"].get("other_waterbodies")
        if not other:
            return None
        fc = self._fc("other_wb_p", lambda o: arcpy.management.Project(other, o, self.target_sr))
        return self.mask("other_wb_mask", fc)

    @property
    def zone(self):
        """1 = land (inside AOI, outside lake polygon), 2 = water (inside lake polygon)."""
        full = self.path("zone")
        if not arcpy.Exists(full):
            self.ensure_grid()
            Con(self.aoi_r == 1, Con(self.lake_r == 1, 2, 1)).save(full)
        return arcpy.Raster(full)

    def sample_points(self, name, constraint, n):
        """Random points constrained by an Extent or a polygon feature class."""
        if isinstance(constraint, arcpy.Extent):
            return arcpy.management.CreateRandomPoints(self.scratch, self.n(name), "", constraint, n)
        return arcpy.management.CreateRandomPoints(self.scratch, self.n(name), constraint, "", n)

    # ---- step 1 --------------------------------------------------------------
    @timed
    def step1_inventory(self):
        """Describe each source; flag missing vertical CRS and unit mismatches."""
        rows = []
        for key, s in self.active.items():
            r = arcpy.Raster(s["path"])
            sr = r.spatialReference
            d = arcpy.Describe(s["path"])
            zmax = r.maximum
            guess = None if zmax is None else ("ft" if zmax > 4000 else "m")
            rows.append(dict(
                key=key, label=s["label"], format=d.format,
                h_crs=sr.name, wkid=sr.factoryCode, h_unit=sr.linearUnitName,
                v_crs=(sr.VCS.name if sr.VCS else None),
                cfg_v_datum=s["v_datum"], cfg_z_units=s["z_units"], z_units_guess=guess,
                cell_x=r.meanCellWidth, cell_y=r.meanCellHeight,
                cols=r.width, rows=r.height, bands=r.bandCount,
                pixel_type=r.pixelType, nodata=r.noDataValue, zmin=r.minimum, zmax=zmax,
                xmin=r.extent.XMin, ymin=r.extent.YMin, xmax=r.extent.XMax, ymax=r.extent.YMax,
            ))
            if not sr.VCS:
                log.warning("%s: no vertical CRS in file; relying on config v_datum=%s", key, s["v_datum"])
            if guess and s["z_units"] != guess:
                log.warning("%s: config z_units=%s but value range suggests %s", key, s["z_units"], guess)
            if r.bandCount != 1:
                log.warning("%s: %d bands; expected 1", key, r.bandCount)
        inv = pd.DataFrame(rows).set_index("key")
        inv.to_csv(self.out_dir / "source_inventory.csv")
        return inv

    # ---- step 2 --------------------------------------------------------------
    def _standardize_one(self, key, s, snap):
        src = arcpy.Raster(s["path"])
        in_sr = src.spatialReference
        if s["type"] == "bathy" and s.get("smooth_window_cells"):
            w = int(s["smooth_window_cells"])
            log.info("%s: focal mean %dx%d at native %.1f m", key, w, w, src.meanCellWidth)
            src = FocalStatistics(src, NbrRectangle(w, w, "CELL"), "MEAN", "DATA")

        transforms = arcpy.ListTransformations(in_sr, self.target_sr)
        geo_tf = s.get("geo_transform") or (transforms[0] if transforms else None)
        log.info("%s: %s -> %s via %s (candidates %s)", key, in_sr.name, self.target_sr.name,
                 geo_tf, transforms[:3])

        proj = self.path(f"{key}_proj")
        with arcpy.EnvManager(snapRaster=snap, cellSize=self.cell, outputCoordinateSystem=self.target_sr):
            arcpy.management.ProjectRaster(src, proj, self.target_sr, "BILINEAR", self.cell, geo_tf,
                                           None if snap else "0 0", in_sr)
        z = arcpy.Raster(proj)
        if s["z_units"] == "ft":
            z = z * FT_TO_M
        # Vertical offsets are NOT applied here: step 3 solves them from these rasters and
        # step 5 applies them, so a changed offset never forces a re-projection.
        z = SetNull((z < self.z_lo) | (z > self.z_hi), z)
        out = self.path(f"{key}_std")
        z.save(out)
        arcpy.management.Delete(proj)
        log.info("%s: saved %s", key, self.n(f"{key}_std"))
        return out

    @timed
    def step2_standardize(self):
        """Project + resample every source onto the target grid. Snap source first."""
        self.ensure_extent()
        order = [k for k in [self.snap_key] + list(self.active) if k in self.active]
        order = list(dict.fromkeys(order))
        for key in order:
            name = f"{key}_std"
            snap = None if key == self.snap_key else self.snap
            if key != self.snap_key and not snap:
                raise SystemExit(f"snap source {self.snap_key} is not active and target.snap_raster is null")
            if not self.stale(name):
                log.info("%s: %s exists, skipping", key, self.n(name))
                continue
            self._standardize_one(key, self.active[key], snap)
        arcpy.env.snapRaster = self.snap
        log.info("snap raster: %s", self.snap)

    # ---- step 3 --------------------------------------------------------------
    @property
    def ref_slope(self):
        """Slope (degrees) of the reference source, used to keep offset samples on gentle ground."""
        full = self.path("ref_slope")
        if not arcpy.Exists(full):
            ref = self.cfg["qa"]["reference"]
            Slope(self.path(f"{ref}_std"), "DEGREE").save(full)
        return full

    def resolved_offsets_file(self):
        return self.out_dir / f"resolved_offsets{'_test' if self.test else ''}.yaml"

    def _solve_pair(self, src, ref, offsets):
        """Median of (src - ref) over stable ground; returns stats row and the solved offset."""
        qa = self.cfg["qa"]
        for k in (src, ref):
            if not self.exists(f"{k}_std"):
                raise SystemExit(f"{k}: standardized raster missing; run step 2")
        if ref not in offsets:
            log.error("%s vs %s: %s has no solved offset yet, so this pair cannot be solved. Fix the "
                      "earlier link, reorder qa.offset_chain, or set vertical_offset_m manually.",
                      src, ref, ref)
            return dict(source=src, reference=ref, flag="UNSOLVED_REFERENCE"), None

        zone = self.zone.catalogPath   # builds AOI / lake masks and zone raster on first use
        diff = arcpy.Raster(self.path(f"{src}_std")) - arcpy.Raster(self.path(f"{ref}_std"))
        name = f"diff_{src}_{ref}"
        diff.save(self.path(name))

        # Terrestrial lidar over the lake is a water surface, so any pair involving one is
        # compared on land only. Two underwater products are compared in the water zone.
        types = {self.src[src]["type"], self.src[ref]["type"]}
        want_zone = 2 if types <= {"topobathy", "bathy"} else 1

        # Random points cover the overlap's bounding box, and the usable overlap (green lidar
        # on land, say) can be a sliver of it. Grow the sample until enough points are usable.
        n_pts = int(qa["sample_points"])
        n_cap = int(qa.get("max_sample_points", 500000))
        min_n = int(qa["min_samples"])
        # Slope comes from the basin reference (2022 lidar), which is NoData over the lake, so it
        # is only extracted for land pairs; requiring it underwater would discard every point.
        use_slope = want_zone == 1
        extract = [[self.path(name), "dz"], [zone, "zone"], [self.path(f"{ref}_std"), "zref"]]
        fields = ["SHAPE@X", "SHAPE@Y", "dz", "zone", "zref"]
        cols = ["x", "y", "dz", "zone", "zref"]
        if use_slope:
            extract.append([self.ref_slope, "slope"])
            fields.append("slope")
            cols.append("slope")
        while True:
            pts = self.sample_points(f"qa_{src}_{ref}", diff.extent, n_pts)
            ExtractMultiValuesToPoints(pts, extract)
            # SearchCursor rather than FeatureClassToNumPyArray: the latter intermittently raises
            # "cannot create NumPyArray. geometry type found" on these point sets.
            with arcpy.da.SearchCursor(pts, fields) as cur:
                df = pd.DataFrame([r for r in cur if None not in r], columns=cols).astype(float)
            n_raw = len(df)
            df = df[(df["zone"] == want_zone) & (df["dz"].abs() < 50)]
            if use_slope:
                df = df[df["slope"] <= float(qa["max_slope_deg"])]
            n = len(df)
            if n >= min_n or n_pts >= n_cap:
                break
            n_pts = min(n_pts * 5, n_cap)
            log.info("%s vs %s: %d usable of %d points; resampling with %d", src, ref, n, n_raw, n_pts)

        row = dict(source=src, reference=ref, zone="water" if want_zone == 2 else "land",
                   n_points=n_pts, n_raw=n_raw, n_used=n)
        if n < min_n:
            log.error("%s vs %s: only %d usable samples from %d points (min %d); offset NOT solved. "
                      "Raise qa.max_sample_points, lower max_slope_deg, or set vertical_offset_m manually.",
                      src, ref, n, n_pts, min_n)
            row.update(median=np.nan, offset_m=np.nan, flag="UNSOLVED")
            return row, None

        dz = df["dz"]
        q = dz.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
        med = float(q[0.5])
        # Plane fit: does the offset vary across the overlap? A constant shift cannot fix tilt.
        A = np.c_[df["x"] - df["x"].mean(), df["y"] - df["y"].mean(), np.ones(n)]
        coef, *_ = np.linalg.lstsq(A, dz.to_numpy(), rcond=None)
        tilt = float(np.hypot(coef[0], coef[1]))  # m per m
        span = tilt * max(df["x"].max() - df["x"].min(), df["y"].max() - df["y"].min())

        offset = offsets[ref] - med   # aligned(src) = std(src) + offset; median(aligned diff) -> 0

        # Elevation-band breakdown: does the difference drift with depth / elevation? For an
        # underwater pair, a trend toward shore points at interpolated shallow sonar; a trend
        # with depth points at sound-velocity or refraction bias. Written next to overlap_qa.csv.
        band = 2.0 if want_zone == 2 else 50.0
        bins = (np.floor(df["zref"] / band) * band).astype(int)
        by_band = (df.assign(band=bins).groupby("band")["dz"]
                   .agg(n="size", median="median", iqr=lambda s: s.quantile(0.75) - s.quantile(0.25))
                   .reset_index().rename(columns={"band": f"{ref}_elev_band_m"}))
        by_band.insert(0, "pair", f"{src} vs {ref}")
        self._band_rows.append(by_band)
        dz_per_m = float(np.polyfit(df["zref"], dz, 1)[0])
        if want_zone == 2:
            log.info("%s vs %s: median dz by %s elevation band (m):\n%s", src, ref, ref,
                     by_band.to_string(index=False))
            log.info("%s vs %s: dz changes %+.3f m per m of elevation (%+.2f m per 10 m of depth)",
                     src, ref, dz_per_m, -dz_per_m * 10)
        flags = []
        if abs(med) > float(qa["diff_flag_m"]):
            flags.append("LARGE_SHIFT")
        if tilt * 1000 > float(qa["tilt_flag_m_per_km"]):
            flags.append("TILT")
        row.update(median=med, iqr=float(q[0.75] - q[0.25]), p05=float(q[0.05]), p95=float(q[0.95]),
                   mean=float(dz.mean()), std=float(dz.std()), tilt_m_per_km=tilt * 1000,
                   tilt_span_m=span, dz_per_m_elev=dz_per_m, offset_m=offset, flag=" ".join(flags))
        log.info("%s vs %s (%s): median %+.3f m, IQR %.3f m, tilt %.3f m/km (%.2f m across overlap), "
                 "n=%d/%d -> offset %+.3f m %s", src, ref, row["zone"], med, row["iqr"],
                 row["tilt_m_per_km"], span, n, n_raw, offset, row["flag"])
        return row, offset

    @timed
    def step3_overlap_qa(self):
        """Solve vertical offsets along qa.offset_chain; write overlap_qa.csv and resolved_offsets.yaml."""
        self.ensure_grid()
        qa = self.cfg["qa"]
        ref = qa["reference"]
        if not self.exists(f"{ref}_std"):
            raise SystemExit(f"reference {ref} not standardized; run step 2")
        offsets = {ref: 0.0}
        rows = []
        self._band_rows = []
        for src, against in qa["offset_chain"]:
            if src not in self.active or against not in self.active:
                log.warning("chain pair %s vs %s skipped: source not active", src, against)
                continue
            row, off = self._solve_pair(src, against, offsets)
            rows.append(row)
            if off is not None:
                offsets[src] = off
        table = pd.DataFrame(rows)
        suffix = "_test" if self.test else ""
        table.to_csv(self.out_dir / f"overlap_qa{suffix}.csv", index=False)
        if self._band_rows:
            pd.concat(self._band_rows).to_csv(self.out_dir / f"overlap_qa_by_band{suffix}.csv", index=False)
        resolved = {k: round(float(v), 4) for k, v in offsets.items()}
        self.resolved_offsets_file().write_text(
            yaml.safe_dump(dict(reference=ref, note="offset_m is ADDED to the standardized raster; "
                                "output vertical datum is that of the reference", offsets=resolved)),
            encoding="utf-8")
        log.info("resolved offsets (m, relative to %s): %s -> %s", ref, resolved, self.resolved_offsets_file())
        unsolved = [k for k in self.active if k not in offsets]
        if unsolved:
            log.error("no offset solved for %s; step 5 will refuse them unless vertical_offset_m is set manually",
                      unsolved)
        return table

    def offsets(self):
        """Per-source vertical offset (m): config number, or solved value when config says auto."""
        resolved = {}
        f = self.resolved_offsets_file()
        if f.exists():
            resolved = yaml.safe_load(f.read_text(encoding="utf-8")).get("offsets", {})
        out = {}
        for key in self.active:
            v = self.src[key].get("vertical_offset_m", "auto")
            if isinstance(v, str) and v.lower() == "auto":
                if key not in resolved:
                    raise SystemExit(f"{key}: vertical_offset_m is auto but no solved value in {f}; "
                                     "run step 3 or set a number in config")
                out[key] = float(resolved[key])
            else:
                out[key] = float(v or 0.0)
        return out

    # ---- step 4 --------------------------------------------------------------
    @timed
    def step4_zones(self):
        """Build (or rebuild with --force) the AOI mask, lake mask, and zone raster."""
        self.ensure_grid()
        if self.force:
            for name in ("zone", "aoi_mask", "lake_mask", "other_wb_mask"):
                if self.exists(name):
                    arcpy.management.Delete(self.path(name))
        z = self.zone
        log.info("zone raster %s ready", self.n("zone"))
        return z

    # ---- step 5 --------------------------------------------------------------
    def water_surface_elev(self, key):
        """Median elevation of a terrestrial raster over the open lake, in its own datum.

        Returns (median, mad, n), or (None, None, 0) when the product has no data over open
        water, in which case there is no water surface to strip.
        """
        cl = self.cfg["cleaning"]
        core = self.lake_core
        if int(arcpy.management.GetCount(core)[0]) == 0:
            log.warning("%s: run extent contains no open-lake core; water surface not detected", key)
            return None, None, 0
        pts = self.sample_points(f"ws_{key}", core, cl["sample_points"])
        ExtractMultiValuesToPoints(pts, [[self.path(f"{key}_std"), "z"]])
        with arcpy.da.SearchCursor(pts, ["z"]) as cur:
            z = np.array([r[0] for r in cur if r[0] is not None], dtype=float)
        n_pts = int(arcpy.management.GetCount(pts)[0])
        if len(z) == 0:
            log.info("%s: NoData at all %d open-lake samples; product carries no water surface, nothing to strip",
                     key, n_pts)
            return None, None, 0
        if len(z) < 100:
            log.warning("%s: only %d of %d open-lake samples have data; water surface estimate is weak",
                        key, len(z), n_pts)
        med = float(np.median(z))
        mad = float(np.median(np.abs(z - med)))
        log.info("%s: water surface %.3f m (MAD %.3f m, n=%d of %d samples with data)",
                 key, med, mad, len(z), n_pts)
        return med, mad, len(z)

    def _clean_one(self, key, s, offset):
        z = arcpy.Raster(self.path(f"{key}_std"))
        info = dict(key=key, offset_m=offset)
        if s["type"] == "terrestrial":
            # Water surface is detected on the un-shifted raster, then both shift together
            ws, mad, n = self.water_surface_elev(key)
            tol = float(s["water_strip_tol_m"])
            if ws is None:
                info.update(water_surface_m=None, water_surface_aligned_m=None, mad_m=None,
                            n_samples=0, tol_m=tol, note="no data over open water; nothing stripped")
            else:
                if mad > tol:
                    log.warning("%s: MAD %.3f exceeds tolerance %.3f; water surface partly retained",
                                key, mad, tol)
                log.info("%s: nulling cells inside lake polygon at <= %.3f m (pre-offset)", key, ws + tol)
                z = SetNull((self.lake_r == 1) & (z <= ws + tol), z)
                info.update(water_surface_m=ws, water_surface_aligned_m=ws + offset, mad_m=mad,
                            n_samples=n, tol_m=tol)
            if self.other_r is not None:
                z = SetNull(self.other_r == 1, z)
        if offset:
            log.info("%s: applying vertical offset %+.3f m", key, offset)
            z = z + offset
        if s["type"] == "topobathy" and s.get("floor_elev_m"):
            floor = float(s["floor_elev_m"])   # absolute elevation in the aligned (reference) datum
            log.info("%s: dropping returns below %.2f m", key, floor)
            z = SetNull(z < floor, z)
            info.update(floor_elev_m=floor)
        elif s["type"] == "bathy":
            z = SetNull(self.lake_r != 1, z)
        z.save(self.path(f"{key}_clean"))
        return info

    @timed
    def step5_clean(self):
        """Apply solved offsets and per-source cleaning. Writes water_surface.csv."""
        self.ensure_grid()
        offsets = self.offsets()
        log.info("vertical offsets applied (m): %s", offsets)
        infos = []
        for key, s in self.active.items():
            if not self.exists(f"{key}_std"):
                raise SystemExit(f"{key}: standardized raster missing; run step 2")
            if not self.stale(f"{key}_clean"):
                log.info("%s: %s exists, skipping", key, self.clean_name(key))
                continue
            infos.append(self._clean_one(key, s, offsets[key]))
        df = pd.DataFrame(infos)
        if len(df):
            df.to_csv(self.out_dir / f"water_surface{'_test' if self.test else ''}.csv", index=False)
        return df

    # ---- step 6 --------------------------------------------------------------
    def _first_valid(self, keys, zone_name):
        blend = self.cfg["blend"]
        feather = float(blend["feather_m"] or 0)
        if zone_name not in (blend.get("feather_zones") or ["land", "water"]):
            feather = 0.0
        log.info("%s chain %s: feather %.0f m", zone_name, keys, feather)
        keys = [k for k in keys if self.exists(f"{k}_clean")]
        if not keys:
            raise SystemExit("no cleaned sources found; run step 5")
        val = sid = None
        for key in reversed(keys):
            r = arcpy.Raster(self.path(f"{key}_clean"))
            i = int(self.src[key]["id"])
            if val is None:
                val, sid = r, Con(IsNull(r), 0, i)
                continue
            if feather > 0:
                d = EucDistance(Con(IsNull(r), 1), feather)   # distance from r's edge, inside r
                w = Con(IsNull(d), 1.0, d / feather)           # 0 at edge -> 1 at feather distance
                val = Con(IsNull(r), val, Con(IsNull(val), r, w * r + (1 - w) * val))
            else:
                val = Con(IsNull(r), val, r)
            sid = Con(IsNull(r), sid, i)
        return val, sid

    @timed
    def step6_mosaic(self):
        """Priority mosaic per zone + source-ID raster."""
        self.ensure_grid()
        if not self.stale("dem_mosaic") and self.exists("dem_source"):
            log.info("mosaic exists, skipping")
            return
        zone = self.zone
        land_val, land_sid = self._first_valid(self.cfg["priority"]["land"], "land")
        water_val, water_sid = self._first_valid(self.cfg["priority"]["water"], "water")
        dem = Con(zone == 1, land_val, Con(zone == 2, water_val))
        sid = Con(zone == 1, land_sid, Con(zone == 2, water_sid))
        sid = SetNull(sid == 0, Int(sid))
        dem.save(self.path("dem_mosaic"))
        sid.save(self.path("dem_source"))
        log.info("saved %s and %s", self.n("dem_mosaic"), self.n("dem_source"))

    # ---- step 7 --------------------------------------------------------------
    @timed
    def step7_export(self):
        """COG GeoTIFFs plus provenance lookup."""
        for name in ("dem_mosaic", "dem_source"):
            if not self.exists(name):
                raise SystemExit(f"{self.n(name)} missing; run step 6")
        dem_path, sid_path, hs_path = (self.out_file(k) for k in ("dem", "source_id", "hillshade"))
        if self.force or not Path(dem_path).exists():
            arcpy.management.CopyRaster(self.path("dem_mosaic"), dem_path, nodata_value=self.nodata,
                                        pixel_type=self.cfg["target"]["pixel_type"], format="COG")
        if self.force or not Path(sid_path).exists():
            arcpy.management.CopyRaster(self.path("dem_source"), sid_path, nodata_value=0,
                                        pixel_type="8_BIT_UNSIGNED", format="COG")
            arcpy.management.BuildRasterAttributeTable(sid_path, "Overwrite")
        if self.force or not Path(hs_path).exists():
            Hillshade(self.path("dem_mosaic"), z_factor=1).save(hs_path)
        offsets = self.offsets()
        ref = self.cfg["qa"]["reference"]
        prov = pd.DataFrame([dict(id=s["id"], key=k, label=s["label"], year=s["year"], type=s["type"],
                                  native_cell_m=s["native_cell_m"], source_v_datum=s["v_datum"],
                                  applied_offset_m=offsets.get(k),
                                  output_v_datum=f"aligned to {ref} ({self.src[ref]['v_datum']})")
                             for k, s in self.src.items()])
        prov.to_csv(self.out_dir / f"source_id_lookup{'_test' if self.test else ''}.csv", index=False)
        log.info("exported %s, %s, %s", dem_path, sid_path, hs_path)
        return prov

    # ---- step 8 --------------------------------------------------------------
    @timed
    def step8_result_qa(self):
        """Area by source, holes inside AOI, seam step statistics."""
        self.ensure_grid()
        dem, sid = self.path("dem_mosaic"), self.path("dem_source")
        for p in (dem, sid):
            if not arcpy.Exists(p):
                raise SystemExit(f"{p} missing; run step 6")
        c2 = self.cell * self.cell

        arcpy.management.BuildRasterAttributeTable(sid, "Overwrite")
        area = pd.DataFrame(arcpy.da.TableToNumPyArray(sid, ["Value", "Count"]))
        labels = {int(s["id"]): s["label"] for s in self.src.values()}
        area["label"] = area["Value"].map(labels)
        area["km2"] = area["Count"] * c2 / 1e6
        area["pct_of_dem"] = 100 * area["Count"] / area["Count"].sum()
        log.info("area by source:\n%s", area[["Value", "label", "km2", "pct_of_dem"]].to_string(index=False))

        holes = Con((self.aoi_r == 1) & IsNull(arcpy.Raster(dem)), 1)
        holes.save(self.path("dem_holes"))
        try:
            arcpy.management.BuildRasterAttributeTable(self.path("dem_holes"), "Overwrite")
            n_holes = sum(int(r[0]) for r in arcpy.da.SearchCursor(self.path("dem_holes"), ["Count"]))
        except arcpy.ExecuteError:
            n_holes = 0  # all-NoData hole raster means no holes
        log.info("NoData cells inside AOI: %d (%.3f km2)", n_holes, n_holes * c2 / 1e6)

        seam = FocalStatistics(sid, NbrRectangle(3, 3, "CELL"), "RANGE", "DATA") > 0
        rng = FocalStatistics(dem, NbrRectangle(3, 3, "CELL"), "RANGE", "DATA")
        seam.save(self.path("seam_mask"))
        rng.save(self.path("dem_range3"))
        pts = self.sample_points("qa_seam_pts", self.ensure_extent(), self.cfg["qa"]["sample_points"] * 4)
        ExtractMultiValuesToPoints(pts, [[self.path("seam_mask"), "seam"], [self.path("dem_range3"), "rng"],
                                         [sid, "src"]])
        with arcpy.da.SearchCursor(pts, ["seam", "rng", "src"]) as cur:
            df = pd.DataFrame([r for r in cur if None not in r], columns=["seam", "rng", "src"]).astype(float)
        seams = df.groupby("seam")["rng"].describe(percentiles=[0.5, 0.9, 0.99])
        log.info("3x3 elevation range, seam (1) vs elsewhere (0):\n%s", seams.to_string())

        suffix = "_test" if self.test else ""
        area.to_csv(self.out_dir / f"area_by_source{suffix}.csv", index=False)
        seams.to_csv(self.out_dir / f"seam_stats{suffix}.csv")
        return area, n_holes, seams

    # ---- driver ---------------------------------------------------------------
    def run(self, steps):
        for s in steps:
            getattr(self, f"step{s}_{STEPS[s]}")()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml"))
    ap.add_argument("--steps", default="all", help='e.g. "all", "2-6", "1,3,8"')
    ap.add_argument("--test", action="store_true", help="run on target.test_extent with test_ prefix")
    ap.add_argument("--force", action="store_true", help="rebuild outputs of the selected steps")
    args = ap.parse_args(argv)
    p = Pipeline(args.config, test=args.test, force=args.force)
    p.run(parse_steps(args.steps))


if __name__ == "__main__":
    main()
