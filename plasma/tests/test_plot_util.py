"""plasma.plot_util.decimate_minmax — the on-screen trace downsampler."""
import numpy as np

from plasma.plot_util import decimate_minmax


def test_noop_under_threshold():
    x = np.arange(500)
    y = np.sin(x / 10.0)
    xd, yd = decimate_minmax(x, y, max_points=3000)
    assert np.array_equal(xd, x) and np.array_equal(yd, y)


def test_reduces_point_count():
    x = np.arange(100_000)
    y = np.sin(x / 50.0)
    xd, yd = decimate_minmax(x, y, max_points=3000)
    assert len(xd) == len(yd) <= 3000
    assert len(xd) >= 2000                       # ~max_points, not a big undershoot


def test_preserves_a_spike():
    x = np.arange(100_000, dtype=float)
    y = np.zeros_like(x)
    y[54_321] = 999.0                            # one-sample R-peak-like spike
    y[54_322] = -999.0
    xd, yd = decimate_minmax(x, y, max_points=2000)
    assert yd.max() == 999.0 and yd.min() == -999.0


def test_endpoints_and_monotonic_x():
    x = np.linspace(0.0, 10.0, 40_000)
    y = np.cos(x)
    xd, _ = decimate_minmax(x, y, max_points=2000)
    assert xd[0] == x[0]
    assert xd[-1] <= x[-1]
    assert np.all(np.diff(xd) >= 0)              # time order preserved
