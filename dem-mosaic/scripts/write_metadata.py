"""Write ArcGIS-format metadata to the DEM mosaic outputs.

Usage (arcgispro-py3):
    python write_metadata.py                 # write metadata to the three COGs in outputs.dir, then synchronize
    python write_metadata.py --dry-run       # only write the XML files next to the outputs, touch nothing
    python write_metadata.py --dry-run --out C:\\temp\\md   # XML to another folder (no arcpy dataset access needed)
    python write_metadata.py --test          # target the *_test.tif outputs instead

What it does
    1. Reads config.yaml (metadata block, sources, priority, blend) and the run outputs
       (resolved_offsets.yaml, overlap_qa.csv, area_by_source.csv, seam_stats.csv,
       water_surface.csv) so every number in the text is the number that was produced.
    2. Builds ArcGIS metadata XML (the "ArcGIS metadata" format Pro stores internally) with
       title, abstract, purpose, credits, keywords, use limitations, contact, lineage with one
       source paragraph per input and one process step per pipeline stage, and a vertical
       accuracy report.
    3. Assigns it with arcpy.metadata.Metadata.xml, saves, and runs synchronize() so Pro fills
       spatial reference, extent, raster dimensions, and cell size from the dataset itself.

The XML is also written beside each raster as <name>.tif.aux.metadata.xml for review, and is
what --dry-run produces. Element names follow the ArcGIS metadata schema; anything Pro does
not recognize is ignored rather than fatal.
"""

import argparse
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

import pandas as pd
import yaml

TOPIC_ELEVATION = "006"        # ISO 19115 TopicCategoryCode: elevation
ROLE_POINT_OF_CONTACT = "007"  # ISO 19115 RoleCode: pointOfContact
ROLE_ORIGINATOR = "006"        # originator
SCOPE_DATASET = "005"
MAINT_AS_NEEDED = "009"


# --------------------------------------------------------------------------- inputs
def load_run(cfg, test):
    """Gather run outputs into one dict; missing files become None so text degrades gracefully."""
    out = Path(cfg["outputs"]["dir"])
    sfx = "_test" if test else ""

    def csv(name):
        p = out / f"{name}{sfx}.csv"
        return pd.read_csv(p) if p.exists() else None

    offsets = None
    p = out / f"resolved_offsets{sfx}.yaml"
    if p.exists():
        offsets = yaml.safe_load(p.read_text(encoding="utf-8"))
    return dict(out=out, sfx=sfx, offsets=offsets, qa=csv("overlap_qa"), area=csv("area_by_source"),
                seams=csv("seam_stats"), water=csv("water_surface"), inventory=csv("source_inventory"))


def out_file(cfg, key, sfx):
    stem, ext = Path(cfg["outputs"][key]).stem, Path(cfg["outputs"][key]).suffix
    return Path(cfg["outputs"]["dir"]) / f"{stem}{sfx}{ext}"


# --------------------------------------------------------------------------- text
def fmt(v, nd=2, unit=""):
    return "n/a" if v is None or pd.isna(v) else f"{v:,.{nd}f}{unit}"


def offsets_sentence(cfg, run):
    if not run["offsets"]:
        return "Vertical offsets between sources were solved empirically (see lineage)."
    ref = run["offsets"]["reference"]
    parts = []
    for k, v in run["offsets"]["offsets"].items():
        if k == ref:
            continue
        parts.append(f"{cfg['sources'][k]['label']} {v:+.2f} m")
    return (f"All sources were shifted onto the vertical datum of the {cfg['sources'][ref]['label']} "
            f"using median differences over stable ground: " + "; ".join(parts) + ".")


def area_sentence(cfg, run):
    a = run["area"]
    if a is None:
        return ""
    bits = [f"{row['label']} {row['km2']:,.0f} km2 ({row['pct_of_dem']:.0f} percent)" for _, row in a.iterrows()]
    return "Area by winning source: " + "; ".join(bits) + "."


def abstract(cfg, run, kind):
    m = cfg["metadata"]
    t = cfg["target"]
    srcs = cfg["sources"]
    order_land = ", ".join(srcs[k]["label"] for k in cfg["priority"]["land"])
    order_water = ", ".join(srcs[k]["label"] for k in cfg["priority"]["water"])
    feather = cfg["blend"]["feather_m"]
    zones = ", ".join(cfg["blend"].get("feather_zones") or ["land", "water"])

    core = (
        f"Bare-earth digital elevation model of the Lake Tahoe Basin, {t['cell_size_m']:g} m cell size, "
        f"covering the TRPA jurisdictional boundary and the full lake bottom. Built by mosaicking "
        f"{len(srcs)} elevation sources with a per-zone priority order in which the first source with valid data at "
        f"a cell wins: on land {order_land}; within the lake high-water polygon {order_water}. "
        f"{offsets_sentence(cfg, run)} Hydro-flattened water surfaces were removed from terrestrial lidar "
        f"inside the lake polygon so that the topobathymetric lidar and sonar supply the lake bottom. "
        f"Seams in the {zones} zone are blended over {feather} m. Horizontal reference NAD 1983 UTM Zone 10N, "
        f"meters. Vertical datum: {m['vertical_datum']}. NoData value {t['nodata']}. "
        f"{area_sentence(cfg, run)}"
    )
    if kind == "dem":
        return core
    if kind == "source_id":
        ids = "; ".join(f"{s['id']} = {s['label']} ({s['year']}, native {s['native_cell_m']:g} m)"
                        for s in sorted(srcs.values(), key=lambda s: s["id"]))
        return (f"Source-ID raster accompanying the Lake Tahoe Basin bare-earth DEM mosaic. Each cell holds "
                f"the integer ID of the elevation source that supplied the DEM value at that cell: {ids}. "
                f"Where seams are feathered the ID is that of the higher-priority source. Use this raster to "
                f"judge the effective resolution and vintage of any DEM cell. Companion to: {m['title']}. "
                f"Mosaic summary: {core}")
    if kind == "hillshade":
        return (f"Hillshade (azimuth 315, altitude 45, z factor 1) of the Lake Tahoe Basin bare-earth DEM "
                f"mosaic, provided for visual quality assessment and cartography. Not an elevation dataset. "
                f"Companion to: {m['title']}. Mosaic summary: {core}")
    raise ValueError(kind)


def purpose(kind):
    return {
        "dem": ("Single continuous land-and-lakebed elevation surface for basin-wide terrain analysis, "
                "hydrologic modeling, shoreline and nearshore studies, and cartography, replacing the need "
                "to combine separate terrestrial, nearshore, and deep-water products."),
        "source_id": ("Provenance layer so users of the DEM mosaic can see which source, vintage, and native "
                      "resolution informs any location, and can mask or weight areas accordingly."),
        "hillshade": "Visual check of seams, artifacts, and terrain texture in the DEM mosaic.",
    }[kind]


def lineage_statement(cfg, run):
    q = run["qa"]
    s = run["seams"]
    lines = [
        "The mosaic was produced with dem-mosaic/scripts/build_dem_mosaic.py (TRPA general-purpose "
        "repository), an eight-step, resumable arcpy pipeline. Every parameter is recorded in the "
        "accompanying config.yaml. The 2022 lidar was read from USGS OPR bare-earth tiles through one "
        "mosaic dataset per UTM zone; the 2010 lidar, 2018 topobathymetric lidar, and USGS bathymetry "
        "were read from the TRPA enterprise geodatabase.",
    ]
    if q is not None:
        rows = []
        for _, r in q.iterrows():
            role = r.get("role", "solve")
            if pd.isna(r.get("median")):
                rows.append(f"{r['source']} vs {r['reference']}: unsolved")
                continue
            rows.append(f"{r['source']} vs {r['reference']} ({r['zone']}, {role}): median {r['median']:+.3f} m, "
                        f"IQR {r['iqr']:.3f} m, n={int(r['n_used'])}")
        lines.append("Overlap comparisons used to solve vertical offsets: " + "; ".join(rows) + ".")
    if s is not None and "seam" in s.columns:
        try:
            bg = s[s["seam"] == 0].iloc[0]
            sm = s[s["seam"] == 1].iloc[0]
            lines.append(f"Post-mosaic seam check (3x3 elevation range at random points): seam cells median "
                         f"{sm['50%']:.2f} m, 99th percentile {sm['99%']:.2f} m (n={int(sm['count'])}); "
                         f"all other cells median {bg['50%']:.2f} m, 99th percentile {bg['99%']:.2f} m.")
        except (IndexError, KeyError):
            pass
    lines.append("Known limitation: shallower than about 13 m the 1999 USGS bathymetric grid disagrees with "
                 "the 2018 topobathymetric lidar by an amount that grows with depth, about 3 m per 10 m "
                 "basin-wide; the product does not document whether that band is interpolated or older "
                 "lidar. Where the 2018 lidar ends, the mosaic ramps into the USGS grid over 300 m, and "
                 "differences of 2 to 3 m between the two were measured at the handoff depth.")
    return " ".join(lines)


def process_steps(cfg, run):
    t = cfg["target"]
    srcs = cfg["sources"]
    off = (run["offsets"] or {}).get("offsets", {})
    water = run["water"]
    steps = [
        ("Inventory", "Read horizontal CRS, cell size, extent, value range, and pixel type of each source; "
                      "confirmed units (three sources in meters, the USGS bathymetric grid in feet) and that "
                      "no source carries a vertical CRS in its metadata."),
        ("Standardize", f"Projected each source to NAD 1983 UTM Zone 10N with bilinear resampling at "
                        f"{t['cell_size_m']:g} m, snapped to a grid registered at integer meters defined by the "
                        f"{srcs[cfg['priority']['land'][0]]['label']}. Converted feet to meters. The bathymetric "
                        f"grid was smoothed with a 3 x 3 focal mean at its native 10 m before resampling. Values "
                        f"outside {t['plausible_z_m'][0]} to {t['plausible_z_m'][1]} m were set to NoData."),
        ("Solve vertical offsets", "For each pair in the offset chain, sampled the difference raster at random "
                                   "points over stable ground (land pairs: outside the lake polygon and on slopes "
                                   f"under {cfg['qa']['max_slope_deg']} degrees; water pairs: inside it), fitted "
                                   "a plane to detect tilt, and took the negated median as the source's shift "
                                   "relative to the reference. " + offsets_sentence(cfg, run)),
        ("Zones", "Rasterized the TRPA boundary and lake high-water polygon; zone 1 is land inside the "
                  "boundary and outside the lake polygon, zone 2 is inside the lake polygon."),
    ]
    clean = ("Applied the solved offsets. Detected each terrestrial lidar product's water-surface elevation "
             "as the median of its values more than "
             f"{cfg['cleaning']['offshore_erode_m']} m offshore, and set cells inside the lake polygon at or "
             "below that elevation plus a tolerance to NoData, keeping exposed beach.")
    if water is not None:
        bits = []
        for _, r in water.iterrows():
            if pd.notna(r.get("water_surface_m")):
                bits.append(f"{srcs[r['key']]['label']}: {r['water_surface_m']:.2f} m (MAD {r['mad_m']:.3f} m)")
            elif "note" in r and isinstance(r["note"], str):
                bits.append(f"{srcs[r['key']]['label']}: {r['note']}")
        if bits:
            clean += " Detected surfaces: " + "; ".join(bits) + "."
    clean += " The bathymetric grid was restricted to the lake polygon, discarding its topographic skin."
    steps.append(("Clean", clean))
    steps.append(("Mosaic", f"Per zone, took the first source with valid data in priority order (land: "
                            f"{', '.join(srcs[k]['label'] for k in cfg['priority']['land'])}; water: "
                            f"{', '.join(srcs[k]['label'] for k in cfg['priority']['water'])}), recording the "
                            f"winning source ID. In the {', '.join(cfg['blend'].get('feather_zones') or ['land', 'water'])} "
                            f"zone each winner was blended into its fallback over {cfg['blend']['feather_m']} m "
                            f"from the winner's data edge using a Euclidean-distance weight."))
    steps.append(("Export", f"Wrote Cloud Optimized GeoTIFFs with {t['compression']} compression and pyramids: "
                            f"DEM (32-bit float, NoData {t['nodata']}), source ID (8-bit unsigned, NoData 0), "
                            f"and hillshade; plus source_id_lookup.csv."))
    steps.append(("Quality check", "Computed area by winning source, NoData cells inside the area of interest, "
                                   "and 3 x 3 elevation range on seam cells versus all other cells."))
    return steps


def source_paragraphs(cfg, run):
    inv = run["inventory"]
    off = (run["offsets"] or {}).get("offsets", {})
    paras = []
    for key, s in sorted(cfg["sources"].items(), key=lambda kv: kv[1]["id"]):
        cite = cfg["metadata"]["source_citations"].get(key, "")
        tech = f"Native cell {s['native_cell_m']:g} m; horizontal reference {s['h_datum']}; units {s['z_units']}."
        if inv is not None and key in inv["key"].values:
            r = inv[inv["key"] == key].iloc[0]
            tech += f" Value range {r['zmin']:.1f} to {r['zmax']:.1f} {s['z_units']}."
        shift = f" Vertical shift applied: {off[key]:+.3f} m." if key in off else ""
        paras.append((s["label"], f"{cite} {tech} Source vertical datum as documented: {s['v_datum']}.{shift}"))
    return paras


# --------------------------------------------------------------------------- xml
def sub(parent, tag, text=None, **attrs):
    e = ET.SubElement(parent, tag, attrs)
    if text is not None:
        e.text = str(text)
    return e


def contact(parent, tag, cfg, role_code):
    m = cfg["metadata"]
    c = sub(parent, tag)
    sub(c, "rpIndName", m["contact_name"])
    sub(c, "rpOrgName", m["organization"])
    info = sub(c, "rpCntInfo")
    if m.get("contact_phone"):
        sub(sub(info, "cntPhone"), "voiceNum", m["contact_phone"])
    addr = sub(info, "cntAddress", addressType="both")
    sub(addr, "eMailAdd", m["contact_email"])
    sub(sub(c, "role"), "RoleCd", value=role_code)


def build_xml(cfg, run, kind):
    m = cfg["metadata"]
    now = dt.datetime.now()
    titles = {"dem": m["title"],
              "source_id": f"{m['title']} - source ID",
              "hillshade": f"{m['title']} - hillshade"}
    root = ET.Element("metadata", {"xml:lang": "en"})
    esri = sub(root, "Esri")
    sub(esri, "CreaDate", now.strftime("%Y%m%d"))
    sub(esri, "CreaTime", now.strftime("%H%M%S00"))
    sub(esri, "ArcGISFormat", "1.0")
    sub(esri, "ArcGISstyle", "ISO 19139 Metadata Implementation Specification")
    sub(esri, "ArcGISProfile", "ISO19139")
    sub(esri, "ModDate", now.strftime("%Y%m%d"))
    sub(esri, "ModTime", now.strftime("%H%M%S00"))

    did = sub(root, "dataIdInfo")
    cit = sub(did, "idCitation")
    sub(cit, "resTitle", titles[kind])
    sub(sub(cit, "date"), "pubDate", m["publish_date"])
    contact(cit, "citRespParty", cfg, ROLE_ORIGINATOR)
    sub(did, "idAbs", abstract(cfg, run, kind))
    sub(did, "idPurp", purpose(kind))
    sub(did, "idCredit", " ".join(m["credits"].split()))
    keys = sub(did, "searchKeys")
    for k in ["DEM", "digital elevation model", "bare earth", "bathymetry", "topobathymetric", "lidar",
              "multibeam sonar", "Lake Tahoe", "Tahoe Basin", "TRPA", "elevation", "terrain", "lakebed"]:
        sub(keys, "keyword", k)
    theme = sub(did, "themeKeys")
    for k in ["elevation", "bathymetry", "lidar"]:
        sub(theme, "keyword", k)
    place = sub(did, "placeKeys")
    for k in ["Lake Tahoe", "California", "Nevada", "Tahoe Basin"]:
        sub(place, "keyword", k)
    cons = sub(sub(did, "resConst"), "Consts")
    sub(cons, "useLimit", " ".join(m["use_limitations"].split()))
    contact(did, "idPoC", cfg, ROLE_POINT_OF_CONTACT)
    sub(sub(did, "dataLang"), "languageCode", value="eng")
    sub(sub(did, "tpCat"), "TopicCatCd", value=TOPIC_ELEVATION)
    sub(sub(sub(did, "resMaint"), "maintFreq"), "MaintFreqCd", value=MAINT_AS_NEEDED)
    sub(sub(did, "spatRpType"), "SpatRepTypCd", value="002")  # grid

    dq = sub(root, "dqInfo")
    sub(sub(sub(dq, "dqScope"), "scpLvl"), "ScopeCd", value=SCOPE_DATASET)
    lin = sub(dq, "dataLineage")
    sub(lin, "statement", lineage_statement(cfg, run))
    for label, text in source_paragraphs(cfg, run):
        ds = sub(lin, "dataSource")
        sub(ds, "srcDesc", text)
        sub(sub(ds, "srcCitatn"), "resTitle", label)
    for i, (label, text) in enumerate(process_steps(cfg, run), 1):
        st = sub(lin, "prcStep")
        sub(st, "stepDesc", f"Step {i}, {label}: {text}")
        sub(st, "stepDateTm", m["publish_date"])
    rep = sub(dq, "report", type="DQAbsExtPosAcc", dimension="vertical")
    sub(rep, "measDesc", "Relative vertical agreement between sources after alignment, measured as the "
                         "median and interquartile range of differences at random points over stable "
                         "ground; see lineage statement. Absolute accuracy inherits from the 2022 lidar.")

    contact(root, "mdContact", cfg, ROLE_POINT_OF_CONTACT)
    sub(root, "mdDateSt", now.strftime("%Y%m%d"))
    sub(sub(root, "mdLang"), "languageCode", value="eng")
    sub(sub(root, "mdHrLv"), "ScopeCd", value=SCOPE_DATASET)
    dist = sub(sub(root, "distInfo"), "distFormat")
    sub(dist, "formatName", "Cloud Optimized GeoTIFF")
    sub(dist, "formatVer", "1.0")
    return minidom.parseString(ET.tostring(root, encoding="unicode")).toprettyxml(indent="  ")


# --------------------------------------------------------------------------- apply
def apply(target, xml_text, sync=True):
    import arcpy  # noqa: deferred so --dry-run --out works without touching datasets
    md = arcpy.metadata.Metadata(str(target))
    if md.isReadOnly:
        raise SystemExit(f"{target}: metadata is read-only (file locked or unsupported)")
    md.xml = xml_text
    md.save()
    if sync:
        md.synchronize("ALWAYS")
        md.save()
    return md.title


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml"))
    ap.add_argument("--test", action="store_true", help="target the *_test outputs")
    ap.add_argument("--dry-run", action="store_true", help="write XML files only; do not touch the rasters")
    ap.add_argument("--out", help="folder for the XML files (default: next to the rasters)")
    ap.add_argument("--no-sync", action="store_true", help="skip arcpy synchronize after writing")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    todo = [k for k in ("contact_email", "vertical_datum") if cfg["metadata"].get(k) == "TODO"]
    if todo:
        print(f"WARNING: metadata.{', metadata.'.join(todo)} still TODO in config; text will say TODO")
    run = load_run(cfg, args.test)
    xml_dir = Path(args.out) if args.out else run["out"]
    xml_dir.mkdir(parents=True, exist_ok=True)

    for kind, key in (("dem", "dem"), ("source_id", "source_id"), ("hillshade", "hillshade")):
        target = out_file(cfg, key, run["sfx"])
        xml_text = build_xml(cfg, run, kind)
        xml_path = xml_dir / f"{target.name}.metadata.xml"
        xml_path.write_text(xml_text, encoding="utf-8")
        print(f"{kind}: XML -> {xml_path}")
        if args.dry_run:
            continue
        if not target.exists():
            print(f"{kind}: {target} not found, skipped")
            continue
        title = apply(target, xml_text, sync=not args.no_sync)
        print(f"{kind}: metadata written to {target} ('{title}')")


if __name__ == "__main__":
    main()
