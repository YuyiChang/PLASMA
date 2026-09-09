"""Small helpers shared by the live plot panels.

The live figures (Data Dashboard, MSense Signal-Quality, IMU) are rebuilt and
re-serialised on a timer; Gradio 5's ``gr.Plot`` recreates the whole Plotly
``<div>`` on every update (gradio#10252), so the cost of a big figure is paid
in browser memory on every tick. Decimating the on-screen trace to a few
thousand points keeps that bounded without touching the data on disk.
"""
from __future__ import annotations

import numpy as np

__all__ = ["decimate_minmax"]


def decimate_minmax(x, y, max_points: int = 3000):
    """Downsample ``(x, y)`` for display, preserving peaks.

    When ``len(y) <= max_points`` the arrays are returned unchanged. Otherwise
    the signal is split into ``max_points // 2`` contiguous buckets and the
    **min and max** sample of each bucket is emitted in time order — so a
    single-sample spike (an ECG R-peak, a motion artefact) survives the
    downsample, unlike plain striding. Output length is ``~max_points``.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n <= max_points or max_points < 4:
        return np.asarray(x, dtype=float), y

    x = np.asarray(x, dtype=float)
    nbuckets = max_points // 2
    # even split; the last bucket absorbs the remainder
    edges = np.linspace(0, n, nbuckets + 1, dtype=int)

    out_x = np.empty(nbuckets * 2, dtype=float)
    out_y = np.empty(nbuckets * 2, dtype=float)
    for b in range(nbuckets):
        lo, hi = edges[b], edges[b + 1]
        if hi <= lo:
            hi = lo + 1
        seg = y[lo:hi]
        i_min = lo + int(np.argmin(seg))
        i_max = lo + int(np.argmax(seg))
        # keep chronological order within the bucket
        a, c = (i_min, i_max) if i_min <= i_max else (i_max, i_min)
        out_x[2 * b], out_y[2 * b] = x[a], y[a]
        out_x[2 * b + 1], out_y[2 * b + 1] = x[c], y[c]
    return out_x, out_y
