"""
Headless run of 00c_cloud2trees on the server (same pattern as run_lidar.py: the notebook stays the
canonical logic, the executed copy in outputs/ is the run record).

Run from the repo root with the ArcGIS Pro Python:
  "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe" scripts\\run_c2t.py
Options:
  --timeout SECONDS    per-cell timeout (default 7 days)
  --test               run the synthetic test suite instead (python -m pytest tests -q)
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.io import load_config, get_logger


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=7 * 24 * 3600)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    log = get_logger("run_c2t")
    if args.test:
        subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], check=True)
        return
    log.info("=" * 60)
    log.info(f"Starting 00c_cloud2trees | input_las={cfg['c2t']['input_las']} | workers={cfg['c2t']['workers']} | synthetic={cfg['run']['synthetic']}")
    log.info("=" * 60)
    src = Path("notebooks") / "00c_cloud2trees.ipynb"
    out = Path("outputs") / f"00c_cloud2trees_run_{datetime.now():%Y%m%d_%H%M}.ipynb"
    cmd = [sys.executable, "-m", "jupyter", "nbconvert", "--to", "notebook", "--execute",
           f"--ExecutePreprocessor.timeout={args.timeout}", "--output", str(out.resolve()), str(src)]
    log.info(" ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
        log.info(f"00c complete; executed copy at {out}")
    except Exception:
        log.exception("00c failed")
        raise


if __name__ == "__main__":
    main()
