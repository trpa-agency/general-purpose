"""
Headless run of 00a_las_to_chm (and optionally 00_lidar) on the server.
Executes the notebooks in place with nbconvert so the notebook stays the canonical logic and
the executed copy in outputs/ is the run record. Safe to rerun: finished tiles are skipped.

Run on the server from the repo root with the ArcGIS Pro Python:
  "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" scripts\run_lidar.py
Options:
  --with-chm-metrics   also run 00_lidar (ITD, rumple) after 00a
  --with-taos          also run 00b_taos (tree objects) after 00_lidar
  --timeout SECONDS    per-cell timeout (default 7 days)
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.io import load_config, get_logger


def run_notebook(name: str, timeout: int, log) -> None:
    src = Path("notebooks") / f"{name}.ipynb"
    out = Path("outputs") / f"{name}_run_{datetime.now():%Y%m%d_%H%M}.ipynb"
    cmd = [sys.executable, "-m", "jupyter", "nbconvert", "--to", "notebook", "--execute",
           f"--ExecutePreprocessor.timeout={timeout}", "--output", str(out.resolve()), str(src)]
    log.info(" ".join(cmd))
    subprocess.run(cmd, check=True)
    log.info(f"{name} complete; executed copy at {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-chm-metrics", action="store_true")
    ap.add_argument("--with-taos", action="store_true", help="also run 00b_taos after 00_lidar")
    ap.add_argument("--timeout", type=int, default=7 * 24 * 3600)
    args = ap.parse_args()
    cfg = load_config()
    log = get_logger("run_lidar")
    log.info("=" * 60)
    log.info(f"Starting LiDAR processing | las_dir={cfg['lidar']['las_dir']} | workers={cfg['lidar']['workers']} | synthetic={cfg['run']['synthetic']}")
    log.info("=" * 60)
    if cfg["run"]["synthetic"]:
        log.warning("run.synthetic is true; set it to false in config.yaml for the real archive")
    try:
        run_notebook("00a_las_to_chm", args.timeout, log)
        if args.with_chm_metrics:
            run_notebook("00_lidar", args.timeout, log)
        if args.with_taos:
            run_notebook("00b_taos", args.timeout, log)
        log.info("LiDAR processing complete")
    except Exception:
        log.exception("LiDAR processing failed; rerun to resume from the last finished tile")
        raise


if __name__ == "__main__":
    main()
