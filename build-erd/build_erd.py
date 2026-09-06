"""Render Mermaid ERD markdown from dumped SDE schema JSON.

Stage 2 of the build-erd pipeline. Pure Python, no arcpy. Loads every
schemas/<name>_schema.json, emits one markdown per workspace plus a
cross-workspace overview and an index.

Usage:
    python build_erd.py [--workspace NAME] [--config PATH]

Output:
    erd/<name>_erd.md            (one per workspace)
    erd/cross_workspace_overview.md
    erd/index.md
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent

CARDINALITY_MAP = {
    "OneToOne": "||--||",
    "OneToMany": "||--o{",
    "ManyToMany": "}o--o{",
}

TYPE_MAP = {
    "OID": "int",
    "Integer": "int",
    "SmallInteger": "int",
    "BigInteger": "int",
    "Double": "float",
    "Single": "float",
    "Float": "float",
    "String": "string",
    "Date": "date",
    "DateOnly": "date",
    "TimeOnly": "time",
    "TimestampOffset": "datetime",
    "GUID": "guid",
    "GlobalID": "guid",
    "Geometry": "geom",
    "Blob": "blob",
    "Raster": "blob",
    "Xml": "xml",
}


def mermaid_safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name or "")


def map_field_type(field: dict, shape_type: str | None = None) -> str:
    raw = field.get("type") or "unknown"
    base = TYPE_MAP.get(raw, raw.lower())
    if base == "geom" and shape_type:
        base = f"geom_{shape_type.lower()}"
    if base == "string":
        length = field.get("length")
        if length and length <= 100:
            base = f"string_{length}"
    return mermaid_safe(base)


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_schemas(schemas_dir: Path) -> dict[str, dict]:
    schemas: dict[str, dict] = {}
    for jf in sorted(schemas_dir.glob("*_schema.json")):
        data = json.loads(jf.read_text(encoding="utf-8"))
        schemas[data["workspace"]["name"]] = data
    return schemas


def build_global_index(schemas: dict[str, dict]) -> dict[str, str]:
    """Map unqualified entity name -> workspace name where it lives."""
    idx: dict[str, str] = {}
    for ws_name, data in schemas.items():
        for fc in data.get("feature_classes", []):
            idx.setdefault(fc["name"], ws_name)
        for tbl in data.get("tables", []):
            idx.setdefault(tbl["name"], ws_name)
    return idx


def collect_pk_fk_fields(
    entity_name: str, rcs: list[dict]
) -> tuple[set[str], set[str]]:
    """Return (pk_fields, fk_fields) for an entity based on RC role tags."""
    pks: set[str] = set()
    fks: set[str] = set()
    for rc in rcs:
        if entity_name in rc.get("origin_class_names", []):
            for field, role in rc.get("origin_class_keys", []):
                if "Primary" in role:
                    pks.add(field)
                elif "Foreign" in role:
                    fks.add(field)
        if entity_name in rc.get("destination_class_names", []):
            for field, role in rc.get("destination_class_keys", []):
                if "Primary" in role:
                    pks.add(field)
                elif "Foreign" in role:
                    fks.add(field)
    return pks, fks


def render_entity_block(
    entity: dict,
    rcs: list[dict],
    pk_field_names: set[str],
    max_fields: int,
    fields_only_pk_fk: bool = False,
) -> list[str]:
    name = mermaid_safe(entity["name"])
    fields = entity.get("fields", []) or []
    role_pks, role_fks = collect_pk_fk_fields(entity["name"], rcs)
    forced_pks = {f.upper() for f in pk_field_names}
    shape_type = entity.get("shape_type")

    def is_pk(fname: str) -> bool:
        return fname in role_pks or fname.upper() in forced_pks

    def is_fk(fname: str) -> bool:
        return fname in role_fks

    def marker(fname: str) -> str:
        if is_pk(fname):
            return " PK"
        if is_fk(fname):
            return " FK"
        return ""

    lines = [f"    {name} {{"]
    if not fields:
        lines.append("    }")
        return lines

    selected = fields
    truncated = 0
    if fields_only_pk_fk:
        selected = [f for f in fields if is_pk(f["name"]) or is_fk(f["name"])]
        truncated = len(fields) - len(selected)
    elif len(fields) > max_fields:
        priority = [f for f in fields if is_pk(f["name"]) or is_fk(f["name"])]
        seen = {f["name"] for f in priority}
        rest = [f for f in fields if f["name"] not in seen]
        selected = priority + rest[: max_fields - len(priority)]
        truncated = len(fields) - len(selected)

    for f in selected:
        ftype = map_field_type(f, shape_type=shape_type)
        fname = mermaid_safe(f["name"])
        lines.append(f"        {ftype} {fname}{marker(f['name'])}")
    if truncated > 0:
        lines.append(f"        %% +{truncated} more fields")
    lines.append("    }")
    return lines


def render_relationship_lines(
    rcs: list[dict], visible_entities: set[str]
) -> list[str]:
    """Render only RCs where both endpoints are in visible_entities."""
    lines: list[str] = []
    for rc in rcs:
        origins = rc.get("origin_class_names") or []
        dests = rc.get("destination_class_names") or []
        if not origins or not dests:
            continue
        origin = origins[0]
        dest = dests[0]
        if origin not in visible_entities or dest not in visible_entities:
            continue
        arrow = CARDINALITY_MAP.get(rc.get("cardinality") or "", "||--o{")
        label = rc.get("forward_label") or rc.get("name") or ""
        label_safe = re.sub(r'"', "'", label)
        lines.append(
            f"    {mermaid_safe(origin)} {arrow} {mermaid_safe(dest)} : \"{label_safe}\""
        )
    return lines


def chunk_entities(entities: list[dict], max_per: int) -> list[list[dict]]:
    if len(entities) <= max_per:
        return [entities]
    chunks = []
    sorted_ents = sorted(entities, key=lambda e: e["name"].lower())
    for i in range(0, len(sorted_ents), max_per):
        chunks.append(sorted_ents[i : i + max_per])
    return chunks


def render_diagram(
    title: str,
    entities: list[dict],
    rcs: list[dict],
    pk_field_names: list[str],
    max_fields: int,
    fields_only_pk_fk: bool = False,
) -> str:
    if not entities:
        return f"### {title}\n\n_(no entities)_\n"
    visible = {e["name"] for e in entities}
    lines = [f"### {title}", "", "```mermaid", "erDiagram"]
    for e in entities:
        lines.extend(
            render_entity_block(
                e,
                rcs,
                set(pk_field_names),
                max_fields,
                fields_only_pk_fk=fields_only_pk_fk,
            )
        )
    lines.extend(render_relationship_lines(rcs, visible))
    lines.append("```")
    return "\n".join(lines) + "\n"


def classify_rc(rc: dict, host_ws: str, global_index: dict[str, str]) -> str:
    """Return 'in_workspace' if both endpoints are in host_ws, else 'cross_workspace'."""
    origins = rc.get("origin_class_names") or []
    dests = rc.get("destination_class_names") or []
    if not origins or not dests:
        return "cross_workspace"
    o_ws = global_index.get(origins[0])
    d_ws = global_index.get(dests[0])
    if o_ws == host_ws and d_ws == host_ws:
        return "in_workspace"
    return "cross_workspace"


def render_workspace_md(
    ws_name: str,
    schema: dict,
    global_index: dict[str, str],
    render_cfg: dict,
) -> str:
    ws = schema["workspace"]
    label = ws["label"]
    fdses = schema.get("feature_datasets", [])
    fcs = schema.get("feature_classes", [])
    tables = schema.get("tables", [])
    all_rcs = schema.get("relationship_classes", [])
    skipped = schema.get("skipped", {})

    in_ws_rcs = [rc for rc in all_rcs if classify_rc(rc, ws_name, global_index) == "in_workspace"]
    cross_rcs = [rc for rc in all_rcs if classify_rc(rc, ws_name, global_index) == "cross_workspace"]

    pk_names = render_cfg.get("pk_field_names", ["OBJECTID", "GLOBALID"])
    max_per = render_cfg.get("max_entities_per_diagram", 60)
    max_fields = render_cfg.get("max_fields_per_entity", 40)
    overview_rels_only = render_cfg.get("overview_relationships_only", True)

    fcs_by_fds: dict[str, list[dict]] = defaultdict(list)
    standalone: list[dict] = []
    for fc in fcs:
        if fc.get("feature_dataset"):
            fcs_by_fds[fc["feature_dataset"]].append(fc)
        else:
            standalone.append(fc)

    parts: list[str] = []
    parts.append(f"# {label} — Schema ERD\n")
    parts.append(
        f"Generated **{ws['dumped_at']}** from `{ws['sde_path']}`. "
        f"arcpy {ws.get('arcpy_version', 'unknown')}.\n"
    )
    parts.append(
        f"**Counts:** {len(fdses)} feature datasets · {len(fcs)} feature classes · "
        f"{len(tables)} tables · {len(all_rcs)} relationship classes "
        f"({len(in_ws_rcs)} in-workspace, {len(cross_rcs)} cross-workspace).\n"
    )

    # Stale warning
    try:
        dumped = datetime.fromisoformat(ws["dumped_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - dumped > timedelta(days=7):
            parts.append(
                "> [!WARNING] This dump is more than 7 days old. "
                f"Re-run `python dump_sde_schema.py --workspace {ws_name}` to refresh.\n"
            )
    except Exception:
        pass

    # TOC
    toc = ["## Contents\n"]
    for fds in fdses:
        toc.append(f"- [Feature dataset: {fds['name']}](#feature-dataset-{mermaid_safe(fds['name']).lower()})")
    if standalone or tables:
        toc.append("- [Standalone feature classes & tables](#standalone)")
    toc.append("- [Relationship overview](#relationship-overview)")
    if cross_rcs:
        toc.append("- [Cross-workspace relationships](#cross-workspace-relationships)")
    if skipped.get("by_pattern") or skipped.get("by_error"):
        toc.append("- [Skipped objects](#skipped-objects)")
    parts.append("\n".join(toc) + "\n")

    # Per-FDS
    for fds in fdses:
        fds_name = fds["name"]
        members = fcs_by_fds.get(fds_name, [])
        anchor = mermaid_safe(fds_name).lower()
        parts.append(f"## Feature dataset: {fds_name} <a id=\"feature-dataset-{anchor}\"></a>\n")
        sr = fds.get("spatial_reference") or "—"
        parts.append(f"Spatial reference: `{sr}`. Members: {len(members)}.\n")
        if not members:
            parts.append("_(no feature classes — likely all filtered)_\n")
            continue
        chunks = chunk_entities(members, max_per)
        for i, chunk in enumerate(chunks, 1):
            title = fds_name if len(chunks) == 1 else f"{fds_name} ({i}/{len(chunks)})"
            parts.append(render_diagram(title, chunk, in_ws_rcs, pk_names, max_fields))

    # Standalone
    if standalone or tables:
        parts.append("## Standalone feature classes & tables <a id=\"standalone\"></a>\n")
        combined = sorted(standalone + tables, key=lambda e: e["name"].lower())
        chunks = chunk_entities(combined, max_per)
        for i, chunk in enumerate(chunks, 1):
            title = "Standalone" if len(chunks) == 1 else f"Standalone ({i}/{len(chunks)})"
            parts.append(render_diagram(title, chunk, in_ws_rcs, pk_names, max_fields))

    # Overview
    parts.append("## Relationship overview <a id=\"relationship-overview\"></a>\n")
    rc_entity_names: set[str] = set()
    for rc in in_ws_rcs:
        rc_entity_names.update(rc.get("origin_class_names") or [])
        rc_entity_names.update(rc.get("destination_class_names") or [])
    name_to_entity = {e["name"]: e for e in fcs + tables}
    overview_entities = [name_to_entity[n] for n in sorted(rc_entity_names) if n in name_to_entity]
    if overview_entities:
        parts.append(
            render_diagram(
                "All in-workspace relationships",
                overview_entities,
                in_ws_rcs,
                pk_names,
                max_fields,
                fields_only_pk_fk=overview_rels_only,
            )
        )
    else:
        parts.append("_(no in-workspace relationship classes)_\n")

    # Cross-workspace pointers
    if cross_rcs:
        parts.append("## Cross-workspace relationships <a id=\"cross-workspace-relationships\"></a>\n")
        parts.append(
            "These relationship classes have at least one endpoint in a different workspace. "
            "See [cross_workspace_overview.md](cross_workspace_overview.md) for the combined diagram.\n"
        )
        parts.append("| RC | Origin (workspace) | Destination (workspace) | Cardinality |")
        parts.append("|---|---|---|---|")
        for rc in cross_rcs:
            origins = rc.get("origin_class_names") or ["?"]
            dests = rc.get("destination_class_names") or ["?"]
            o_ws = global_index.get(origins[0], "?")
            d_ws = global_index.get(dests[0], "?")
            parts.append(
                f"| `{rc['name']}` | `{origins[0]}` ({o_ws}) | `{dests[0]}` ({d_ws}) | {rc.get('cardinality') or '—'} |"
            )
        parts.append("")

    # Skipped
    if skipped.get("by_pattern") or skipped.get("by_error"):
        parts.append("## Skipped objects <a id=\"skipped-objects\"></a>\n")
        if skipped.get("by_pattern"):
            parts.append(f"**By filter pattern:** {len(skipped['by_pattern'])} objects (e.g. `GDB_*`, archive shadows).\n")
            preview = ", ".join(f"`{n}`" for n in skipped["by_pattern"][:20])
            more = "" if len(skipped["by_pattern"]) <= 20 else f" … +{len(skipped['by_pattern']) - 20} more"
            parts.append(preview + more + "\n")
        if skipped.get("by_error"):
            parts.append(f"**By error:** {len(skipped['by_error'])} objects could not be described.\n")
            parts.append("| Object | Type | Error |")
            parts.append("|---|---|---|")
            for e in skipped["by_error"]:
                err = (e.get("error") or "").replace("|", "\\|").replace("\n", " ")[:200]
                parts.append(f"| `{e.get('name')}` | {e.get('type', '?')} | {err} |")
            parts.append("")

    return "\n".join(parts)


def render_cross_workspace_md(
    schemas: dict[str, dict],
    global_index: dict[str, str],
    render_cfg: dict,
) -> str:
    """Render combined diagram of RCs that span workspaces."""
    cross_rcs: list[tuple[str, dict]] = []  # (host_ws, rc)
    for ws_name, data in schemas.items():
        for rc in data.get("relationship_classes", []):
            if classify_rc(rc, ws_name, global_index) == "cross_workspace":
                cross_rcs.append((ws_name, rc))

    parts = ["# Cross-workspace relationship classes\n"]
    if not cross_rcs:
        parts.append("_(no relationship classes span workspaces — every RC has both endpoints in the same SDE)_\n")
        return "\n".join(parts)

    parts.append(
        f"{len(cross_rcs)} relationship class(es) link entities across SDE workspaces. "
        "Entity names are workspace-prefixed (e.g. `sde__Parcel`, `tabular__Inventory`) to disambiguate.\n"
    )

    # Collect entities
    visible: set[str] = set()
    name_to_entity: dict[str, dict] = {}
    for ws_name, data in schemas.items():
        for ent in data.get("feature_classes", []) + data.get("tables", []):
            name_to_entity[ent["name"]] = ent

    block_lines = ["```mermaid", "erDiagram"]
    rendered_entities: set[tuple[str, str]] = set()
    for host_ws, rc in cross_rcs:
        origins = rc.get("origin_class_names") or []
        dests = rc.get("destination_class_names") or []
        if not origins or not dests:
            continue
        origin = origins[0]
        dest = dests[0]
        o_ws = global_index.get(origin, "external")
        d_ws = global_index.get(dest, "external")
        for ent_name, ws in [(origin, o_ws), (dest, d_ws)]:
            key = (ws, ent_name)
            if key in rendered_entities:
                continue
            rendered_entities.add(key)
            label = mermaid_safe(f"{ws}__{ent_name}")
            block_lines.append(f"    {label} {{ }}")
        arrow = CARDINALITY_MAP.get(rc.get("cardinality") or "", "||--o{")
        rc_label = (rc.get("forward_label") or rc.get("name") or "").replace('"', "'")
        block_lines.append(
            f"    {mermaid_safe(o_ws + '__' + origin)} {arrow} "
            f"{mermaid_safe(d_ws + '__' + dest)} : \"{rc_label}\""
        )
    block_lines.append("```")
    parts.append("\n".join(block_lines) + "\n")

    # Inventory
    parts.append("## Inventory\n")
    parts.append("| Host workspace | RC | Origin (workspace) | Destination (workspace) | Cardinality |")
    parts.append("|---|---|---|---|---|")
    for host_ws, rc in cross_rcs:
        origins = rc.get("origin_class_names") or ["?"]
        dests = rc.get("destination_class_names") or ["?"]
        o_ws = global_index.get(origins[0], "external")
        d_ws = global_index.get(dests[0], "external")
        parts.append(
            f"| {host_ws} | `{rc['name']}` | `{origins[0]}` ({o_ws}) | "
            f"`{dests[0]}` ({d_ws}) | {rc.get('cardinality') or '—'} |"
        )
    parts.append("")
    return "\n".join(parts)


def render_index_md(schemas: dict[str, dict]) -> str:
    parts = ["# build-erd — Generated ERDs\n"]
    parts.append("| Workspace | FDS | Feature classes | Tables | Relationship classes | Dumped |")
    parts.append("|---|---:|---:|---:|---:|---|")
    for ws_name, data in schemas.items():
        ws = data["workspace"]
        parts.append(
            f"| [{ws['label']}]({ws_name}_erd.md) | "
            f"{len(data.get('feature_datasets', []))} | "
            f"{len(data.get('feature_classes', []))} | "
            f"{len(data.get('tables', []))} | "
            f"{len(data.get('relationship_classes', []))} | "
            f"{ws.get('dumped_at', '—')} |"
        )
    parts.append("")
    parts.append("- [Cross-workspace overview](cross_workspace_overview.md)")
    parts.append("")
    parts.append("Regenerate with:")
    parts.append("")
    parts.append("```")
    parts.append("python dump_sde_schema.py        # rebuild JSON cache")
    parts.append("python build_erd.py              # render markdown")
    parts.append("```")
    parts.append("")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", help="Render only this workspace (default: all)")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.yaml"))
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    schemas_dir = (SCRIPT_DIR / cfg["output"]["schemas_dir"]).resolve()
    erd_dir = (SCRIPT_DIR / cfg["output"]["erd_dir"]).resolve()
    erd_dir.mkdir(parents=True, exist_ok=True)
    render_cfg = cfg.get("render", {})

    schemas = load_schemas(schemas_dir)
    if not schemas:
        print(f"No schema JSON files found in {schemas_dir}. Run dump_sde_schema.py first.")
        return

    global_index = build_global_index(schemas)

    targets = list(schemas.keys()) if not args.workspace else [args.workspace]
    for ws_name in targets:
        if ws_name not in schemas:
            print(f"Skipping '{ws_name}': no schema JSON loaded")
            continue
        md = render_workspace_md(ws_name, schemas[ws_name], global_index, render_cfg)
        out_path = erd_dir / f"{ws_name}_erd.md"
        out_path.write_text(md, encoding="utf-8")
        print(f"Wrote {out_path}")

    cross_path = erd_dir / "cross_workspace_overview.md"
    cross_path.write_text(render_cross_workspace_md(schemas, global_index, render_cfg), encoding="utf-8")
    print(f"Wrote {cross_path}")

    index_path = erd_dir / "index.md"
    index_path.write_text(render_index_md(schemas), encoding="utf-8")
    print(f"Wrote {index_path}")


if __name__ == "__main__":
    main()
