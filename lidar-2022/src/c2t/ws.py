"""
c2t/ws.py — window size functions for individual tree detection (R: itd_ws_functions()).

Each takes tree height (m, array-like) and returns the local-maximum window diameter (m). The three
defaults are copied from cloud2trees v0.8.3 including their clamps. `log_fn` is the package default.
"""
from __future__ import annotations

import numpy as np


def lin_fn(x):
    x = np.asarray(x, dtype=float)
    y = 0.75 + x * 0.14
    y = np.where(x < 2, 1.0, y)
    y = np.where(x > 30, 5.0, y)
    y = np.where(x < 0, 0.001, y)
    return np.where(np.isnan(x), 0.001, y)


def exp_fn(x):
    x = np.asarray(x, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        y = np.exp(0.0446 * x + np.power(np.where(x > 0, x, np.nan), -0.555))
    y = np.where(x < 3.6, 0.9 + x * 0.24, y)
    y = np.where(x > 32.5, 5.0, y)
    y = np.where(x < 0, 1e-3, y)
    return np.where(np.isnan(x), 1e-3, y)


def log_fn(x):
    x = np.asarray(x, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        y = np.exp(-(3.5 * (1.0 / np.where(x > 0, x, np.nan))) + np.power(np.where(x > 0, x, np.nan), 0.17))
    y = np.where(x < 2, 0.6, y)
    y = np.where(x > 26.5, 5.0, y)
    y = np.where(x < 0, 0.001, y)
    return np.where(np.isnan(x), 0.001, y)


def constant_fn(ws: float):
    """A fixed window, like passing a number to lidR::lmf(ws=)."""
    def f(x):
        return np.full(np.shape(x), float(ws))
    return f


def itd_ws_functions() -> dict:
    return {"lin_fn": lin_fn, "exp_fn": exp_fn, "log_fn": log_fn}


def as_ws_function(ws):
    """Accept a number, a callable, or a name from itd_ws_functions(). Returns a callable of height."""
    if callable(ws):
        return ws
    if isinstance(ws, str):
        return itd_ws_functions()[ws]
    return constant_fn(float(ws))
