"""Dump SDE workspace schema to JSON for ERD rendering.

READ-ONLY. This script never writes to any SDE. It uses only arcpy.Describe,
arcpy.ListFields, and arcpy.env.workspace (which is a pointer assignment, not
a write). No SearchCursor/UpdateCursor/InsertCursor; no Create/Update/Delete;
no schema mutation. The only filesystem writes are to local schemas/*.json
and dump.log.

Stage 1 of the build-erd pipeline. Iterates each configured SDE workspace via
arcpy.Describe(ws).children, captures feature datasets, feature classes, tables,
and relationship classes, applies regex filters, writes one JSON per workspace.

Per arcpy guidance, arcpy.da.Walk() is avoided (broken for SDE feature datasets,
performance degrades sharply with type filters). arcpy.Describe(ws).children is
the idiomatic 2.x+ pattern.

Usage:
    python dump_sde_schema.py [--workspace NAME] [--config PATH]

Output:
    schemas/<name>_schema.json  (one per workspace)
    dump.log                    (run log)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import datetime as _dt
from pathlib import Path
from typing import Any

import yaml

import arcpy

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_PATH = SCRIPT_DIR / "dump.log"

logger = logging.getLogger("dump_sde_schema")


def setup_logging() -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(sh)


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def unqualify(name: str) -> str:
    """Strip OWNER.SCHEMA. prefix, leaving just the object name."""
    return name.rsplit(".", 1)[-1] if "." in name else name


def compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in (patterns or [])]


def is_filtered_out(name: str, excludes: list[re.Pattern], includes: list[re.Pattern]) -> bool:
    bare = unqualify(name)
    if any(p.search(bare) for p in excludes):
        return True
    if includes and not any(p.search(bare) for p in includes):
        return True
    return False


def describe_field(f) -> dict:
    return {
        "name": f.name,
        "type": f.type,
        "length": getattr(f, "length", None),
        "nullable": getattr(f, "isNullable", None),
        "alias": getattr(f, "aliasName", None),
        "domain": getattr(f, "domain", None) or None,
    }


def describe_feature_class(path: str, parent_fds: str | None) -> dict:
    desc = arcpy.Describe(path)
    fields = [describe_field(f) for f in arcpy.ListFields(path)]
    sr = getattr(desc, "spatialReference", None)
    sr_name = getattr(sr, "name", None) if sr else None
    return {
        "name": unqualify(desc.name),
        "qualified_name": desc.name,
        "feature_dataset": parent_fds,
        "kind": "feature_class",
        "shape_type": getattr(desc, "shapeType", None),
        "has_z": getattr(desc, "hasZ", False),
        "has_m": getattr(desc, "hasM", False),
        "spatial_reference": sr_name,
        "object_id_field": getattr(desc, "OIDFieldName", None),
        "global_id_field": getattr(desc, "globalIDFieldName", None) or None,
        "subtype_field": getattr(desc, "subtypeFieldName", None) or None,
        "fields": fields,
    }


def describe_table(path: str) -> dict:
    desc = arcpy.Describe(path)
    fields = [describe_field(f) for f in arcpy.ListFields(path)]
    return {
        "name": unqualify(desc.name),
        "qualified_name": desc.name,
        "feature_dataset": None,
        "kind": "table",
        "object_id_field": getattr(desc, "OIDFieldName", None),
        "global_id_field": getattr(desc, "globalIDFieldName", None) or None,
        "subtype_field": getattr(desc, "subtypeFieldName", None) or None,
        "fields": fields,
    }


def describe_feature_dataset(path: str) -> dict:
    desc = arcpy.Describe(path)
    sr = getattr(desc, "spatialReference", None)
    sr_name = getattr(sr, "name", None) if sr else None
    return {
        "name": unqualify(desc.name),
        "qualified_name": desc.name,
        "spatial_reference": sr_name,
        "feature_classes": [],  # populated as we iterate children
    }


def describe_relationship_class(path: str, parent_fds: str | None) -> dict:
    desc = arcpy.Describe(path)
    # originClassKeys / destinationClassKeys are lists of (field_name, key_role) tuples
    origin_keys = [list(k) for k in getattr(desc, "originClassKeys", []) or []]
    dest_keys = [list(k) for k in getattr(desc, "destinationClassKeys", []) or []]
    return {
        "name": unqualify(desc.name),
        "qualified_name": desc.name,
        "feature_dataset": parent_fds,
        "cardinality": getattr(desc, "cardinality", None),
        "is_attributed": getattr(desc, "isAttributed", False),
        "is_composite": getattr(desc, "isComposite", False),
        "key_type": getattr(desc, "keyType", None),
        "forward_label": getattr(desc, "forwardPathLabel", None) or None,
        "backward_label": getattr(desc, "backwardPathLabel", None) or None,
        "origin_class_names": [unqualify(n) for n in (getattr(desc, "originClassNames", []) or [])],
        "origin_class_keys": origin_keys,
        "destination_class_names": [unqualify(n) for n in (getattr(desc, "destinationClassNames", []) or [])],
        "destination_class_keys": dest_keys,
    }


def dump_workspace(ws_cfg: dict, filters: dict, schemas_dir: Path) -> Path:
    name = ws_cfg["name"]
    label = ws_cfg.get("label", name)
    sde_path = ws_cfg["sde_path"]
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    logger.info("=" * 60)
    logger.info("Dumping workspace '%s' (%s)", name, label)
    logger.info("SDE: %s", sde_path)

    if not Path(sde_path).exists():
        raise FileNotFoundError(f"SDE connection file not found: {sde_path}")

    excludes = compile_patterns(filters.get("exclude_patterns", []))
    includes = compile_patterns(filters.get("include_patterns", []))

    arcpy.env.workspace = sde_path

    feature_datasets: list[dict] = []
    feature_classes: list[dict] = []
    tables: list[dict] = []
    relationship_classes: list[dict] = []
    skipped_by_pattern: list[str] = []
    skipped_by_error: list[dict] = []

    try:
        ws_desc = arcpy.Describe(sde_path)
    except Exception as e:
        logger.error("Failed to Describe workspace %s: %s", sde_path, e)
        raise

    children = list(ws_desc.children)
    logger.info("Top-level children: %d", len(children))

    for child in children:
        bare = unqualify(child.name)
        dt = child.dataType
        if is_filtered_out(child.name, excludes, includes):
            skipped_by_pattern.append(bare)
            continue
        try:
            if dt == "FeatureDataset":
                fds_path = os.path.join(sde_path, child.name)
                fds_record = describe_feature_dataset(fds_path)
                # Recurse into the feature dataset for nested FCs & RCs
                try:
                    fds_children = list(arcpy.Describe(fds_path).children)
                except Exception as e:
                    logger.warning("Could not enumerate FDS '%s': %s", child.name, e)
                    fds_children = []
                for sub in fds_children:
                    sub_bare = unqualify(sub.name)
                    if is_filtered_out(sub.name, excludes, includes):
                        skipped_by_pattern.append(sub_bare)
                        continue
                    sub_path = os.path.join(fds_path, sub.name)
                    try:
                        if sub.dataType == "FeatureClass":
                            fc = describe_feature_class(sub_path, parent_fds=fds_record["name"])
                            feature_classes.append(fc)
                            fds_record["feature_classes"].append(fc["name"])
                        elif sub.dataType == "RelationshipClass":
                            rc = describe_relationship_class(sub_path, parent_fds=fds_record["name"])
                            relationship_classes.append(rc)
                        else:
                            logger.debug("Skipping FDS child '%s' of type %s", sub.name, sub.dataType)
                    except Exception as e:
                        logger.warning("Failed on FDS child '%s': %s", sub.name, e)
                        skipped_by_error.append({"name": sub_bare, "type": sub.dataType, "error": str(e)})
                feature_datasets.append(fds_record)
                logger.info("FDS '%s': %d feature classes", fds_record["name"], len(fds_record["feature_classes"]))

            elif dt == "FeatureClass":
                fc_path = os.path.join(sde_path, child.name)
                feature_classes.append(describe_feature_class(fc_path, parent_fds=None))

            elif dt == "Table":
                tbl_path = os.path.join(sde_path, child.name)
                tables.append(describe_table(tbl_path))

            elif dt == "RelationshipClass":
                rc_path = os.path.join(sde_path, child.name)
                relationship_classes.append(describe_relationship_class(rc_path, parent_fds=None))

            else:
                logger.debug("Skipping top-level '%s' of type %s", child.name, dt)

        except Exception as e:
            logger.warning("Failed on '%s' (%s): %s", child.name, dt, e)
            skipped_by_error.append({"name": bare, "type": dt, "error": str(e)})

    try:
        arcpy_version = arcpy.GetInstallInfo().get("Version", "unknown")
    except Exception:
        arcpy_version = "unknown"

    out = {
        "workspace": {
            "name": name,
            "label": label,
            "sde_path": sde_path,
            "dumped_at": started_at,
            "arcpy_version": arcpy_version,
        },
        "feature_datasets": feature_datasets,
        "feature_classes": feature_classes,
        "tables": tables,
        "relationship_classes": relationship_classes,
        "skipped": {
            "by_pattern": sorted(set(skipped_by_pattern)),
            "by_error": skipped_by_error,
        },
    }

    schemas_dir.mkdir(parents=True, exist_ok=True)
    out_path = schemas_dir / f"{name}_schema.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    logger.info(
        "Wrote %s — FDS=%d, FC=%d, Tables=%d, RC=%d, skipped(pattern)=%d, skipped(error)=%d",
        out_path,
        len(feature_datasets),
        len(feature_classes),
        len(tables),
        len(relationship_classes),
        len(out["skipped"]["by_pattern"]),
        len(skipped_by_error),
    )
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", help="Dump only this workspace name (default: all configured)")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.yaml"), help="Path to config.yaml")
    args = parser.parse_args()

    setup_logging()
    arcpy.SetLogHistory(False)

    cfg = load_config(Path(args.config))
    schemas_dir = (SCRIPT_DIR / cfg["output"]["schemas_dir"]).resolve()
    filters = cfg.get("filters", {})

    workspaces = cfg.get("workspaces", [])
    if args.workspace:
        workspaces = [w for w in workspaces if w["name"] == args.workspace]
        if not workspaces:
            logger.error("No workspace named '%s' in config", args.workspace)
            sys.exit(2)

    failures = 0
    for ws in workspaces:
        try:
            dump_workspace(ws, filters, schemas_dir)
        except Exception as e:
            logger.exception("Workspace '%s' dump failed: %s", ws["name"], e)
            failures += 1

    if failures:
        logger.error("%d workspace(s) failed", failures)
        sys.exit(1)


if __name__ == "__main__":
    main()
