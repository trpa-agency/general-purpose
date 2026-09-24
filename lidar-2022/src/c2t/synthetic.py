"""
c2t/synthetic.py — a fake LiDAR tile for tests and the synthetic notebook run: sloping ground plus cone
crowns with an ALS-like surface. Returns the tree truth table (x, y, height_m).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

CRS = "EPSG:26910"
X0, Y0 = 760000.0, 4310000.0


def synthetic_tile(path: Path, n_trees: int = 40, seed: int = 3, size: float = 120.0, ground_pts_m2: float = 4.0):
    """Sloping ground plus n_trees cones (height 8 to 30 m, crown radius 0.25 x height) as a LAZ file. Returns tree truth."""
    import laspy
    from pyproj import CRS as PCRS
    rng = np.random.default_rng(seed)
    n_g = int(size * size * ground_pts_m2)
    gx = rng.uniform(X0, X0 + size, n_g); gy = rng.uniform(Y0, Y0 + size, n_g)
    def ground(x, y):
        return 1900.0 + 0.05 * (x - X0) + 0.02 * (y - Y0)
    gz = ground(gx, gy) + rng.normal(0, 0.03, n_g)
    gc = np.full(n_g, 2, np.uint8)
    # trees on a jittered grid so crowns rarely touch
    k = int(np.ceil(np.sqrt(n_trees)))
    cx, cy = np.meshgrid(np.linspace(X0 + 12, X0 + size - 12, k), np.linspace(Y0 + 12, Y0 + size - 12, k))
    cx = cx.ravel()[:n_trees] + rng.uniform(-2, 2, n_trees); cy = cy.ravel()[:n_trees] + rng.uniform(-2, 2, n_trees)
    h = rng.uniform(8, 30, n_trees)
    tx, ty, tz, tc = [], [], [], []
    for x, y, ht in zip(cx, cy, h):
        r = 0.25 * ht
        n = int(60 * ht)
        # ALS sees the crown surface: most returns sit on the cone surface, a few penetrate inside;
        # the crown starts at 40 percent of the height (conifer crown ratio about 0.6)
        base = 0.4 * ht
        rad = 0.6 * r * np.sqrt(rng.uniform(0, 1, n))          # the cone ends where it reaches the crown base
        surface = ht * (1 - rad / r)
        inside = rng.uniform(0, 1, n) < 0.25
        z_rel = np.where(inside, base + (surface - base) * rng.uniform(0.0, 1.0, n), surface + rng.normal(0, 0.1, n))
        z_rel = np.clip(z_rel, 0.5, ht)
        ang = rng.uniform(0, 2 * np.pi, n)
        px = x + rad * np.cos(ang); py = y + rad * np.sin(ang)
        tx.append(px); ty.append(py); tz.append(ground(px, py) + z_rel); tc.append(np.full(n, 1, np.uint8))
    x = np.concatenate([gx] + tx); y = np.concatenate([gy] + ty); z = np.concatenate([gz] + tz); c = np.concatenate([gc] + tc)
    # a few noise points high above
    x = np.append(x, [X0 + 5, X0 + 60]); y = np.append(y, [Y0 + 5, Y0 + 60]); z = np.append(z, [2100.0, 2200.0]); c = np.append(c, [1, 1]).astype(np.uint8)
    hdr = laspy.LasHeader(point_format=6, version="1.4")
    hdr.offsets = [X0, Y0, 1900.0]; hdr.scales = [0.001, 0.001, 0.001]
    hdr.add_crs(PCRS.from_user_input(CRS))
    las = laspy.LasData(hdr)
    las.x = x; las.y = y; las.z = z; las.classification = c
    las.return_number = np.ones(len(x), np.uint8); las.number_of_returns = np.ones(len(x), np.uint8)
    las.write(path)
    return pd.DataFrame({"x": cx, "y": cy, "height_m": h})
