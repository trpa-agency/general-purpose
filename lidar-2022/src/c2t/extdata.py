"""
c2t/extdata.py — the three external datasets cloud2trees uses. Port of get_data(), get_treemap(),
get_foresttype(), get_landfire(), get_url_data(), and find_ext_data().

Default location is `data/raw/ext/<name>/` under the repo (config `c2t.ext_dir`), instead of the R package
directory. Each get_*() downloads a zip, unpacks it, moves nested files to the top of its folder, deletes
the zip, and checks the required files are present. URLs are the ones in cloud2trees v0.8.3 (TreeMap 2022
from a USFS Box link updated 2025-08-21; Forest Type Groups 30 m from Zenodo 14630199; LANDFIRE CBD from
Zenodo 19684623). If a link dies, download by hand and drop the files in the folder; find_ext_data() only
looks for the file names.
"""
from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

DATASETS = {
    "treemap": {
        "url": "https://usfs-public.box.com/shared/static/jaayceyk4i5vl484fuopbnssy017r6v9.zip",
        "files": ("treemap2022_conus.tif", "treemap2022_conus_tree_table.csv"),
        "alt_files": ("treemap2016.tif", "treemap2016_tree_table.csv"),
    },
    "foresttype": {
        "url": "https://zenodo.org/records/14630199/files/foresttype.zip?download=1",
        "files": ("foresttype_lookup.csv", "foresttype.tif"),
    },
    "landfire": {
        "url": "https://zenodo.org/records/19684623/files/landfire_cbd_numeric.zip?download=1",
        "files": ("lc23_cbd_240.tif",),
    },
}


def default_ext_dir() -> Path:
    env = os.environ.get("C2T_EXT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "raw" / "ext"


def _has_files(folder: Path, files) -> bool:
    if not folder or not Path(folder).is_dir():
        return False
    present = {p.name.lower() for p in Path(folder).iterdir()}
    return all(f.lower() in present for f in files)


def _download(url: str, dest_zip: Path, log=None) -> None:
    import urllib.request
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if log: log.info(f"Downloading {url} to {dest_zip}")
    req = urllib.request.Request(url, headers={"User-Agent": "c2t/0.1 (python urllib)"})
    with urllib.request.urlopen(req, timeout=3600) as r, open(dest_zip, "wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)


def _unzip_flat(zip_path: Path, folder: Path, remove_zip: bool = True) -> None:
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(folder)
        for name in z.namelist():
            if name.endswith("/") or "/" not in name:
                continue
            src = folder / name
            if src.exists():
                shutil.move(str(src), str(folder / Path(name).name))
    for p in list(folder.iterdir()):
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    if remove_zip:
        zip_path.unlink(missing_ok=True)


def get_url_data(name: str, savedir=None, force: bool = False, log=None) -> bool:
    meta = DATASETS[name]
    folder = Path(savedir or default_ext_dir()) / name
    if _has_files(folder, meta["files"]) and not force:
        if log: log.info(f"{name} already downloaded to {folder}; use force=True to overwrite")
        return False
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / f"{name}.zip"
    _download(meta["url"], zip_path, log=log)
    _unzip_flat(zip_path, folder)
    missing = [f for f in meta["files"] if not (folder / f).exists()]
    if missing:
        raise FileNotFoundError(f"{name}: expected files missing after download: {missing}")
    return True


def get_treemap(savedir=None, force: bool = False, log=None) -> bool:
    return get_url_data("treemap", savedir, force, log)


def get_foresttype(savedir=None, force: bool = False, log=None) -> bool:
    return get_url_data("foresttype", savedir, force, log)


def get_landfire(savedir=None, force: bool = False, log=None) -> bool:
    return get_url_data("landfire", savedir, force, log)


def get_data(savedir=None, force: bool = False, log=None) -> dict:
    """Download all three. About 7 GB; TreeMap alone is several GB."""
    return {n: get_url_data(n, savedir, force, log) for n in DATASETS}


def find_ext_data(input_treemap_dir=None, input_foresttype_dir=None, input_landfire_dir=None, ext_dir=None) -> dict:
    """
    Where each dataset is. Looks in the explicit input_*_dir first (that folder or a `<name>` subfolder),
    then ext_dir (config c2t.ext_dir or data/raw/ext), then the current directory. None when not found.
    """
    base = Path(ext_dir) if ext_dir else default_ext_dir()
    out = {}
    for name, given in (("treemap", input_treemap_dir), ("foresttype", input_foresttype_dir), ("landfire", input_landfire_dir)):
        files = DATASETS[name]["files"]
        alts = DATASETS[name].get("alt_files")
        candidates = []
        if given:
            candidates += [Path(given), Path(given) / name]
        candidates += [base / name, base, Path.cwd() / name, Path.cwd()]
        found = None
        for c in candidates:
            if _has_files(c, files) or (alts and _has_files(c, alts)):
                found = str(c)
                break
        out[f"{name}_dir"] = found
    return out
