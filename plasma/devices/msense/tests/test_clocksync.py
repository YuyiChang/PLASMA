"""Clock-sync core: counter matching + interpolation."""
import io
from contextlib import redirect_stdout

import numpy as np
import pandas as pd

from plasma.devices.msense.extract.clocksync import (
    interpolate_timestamps,
    match_counters,
)


def test_match_counters_is_monotonic_forward():
    # CSV counter runs 0,2,4,...; txt references a subset of those counters.
    cdct = np.arange(50, dtype=float)
    counter = np.arange(0, 100, 2, dtype=float)
    valid_csv = np.column_stack((cdct, counter))
    df_txt = pd.DataFrame({"Counter": [0, 10, 40, 98], "t_unixc_lin": [1000., 1005., 1020., 1049.]})

    matched, stats, points = match_counters(valid_csv, df_txt)

    assert stats["matched"] == 4
    idx = np.where(~np.isnan(matched))[0]
    assert list(idx) == sorted(idx)                 # forward-only
    assert matched[np.where(counter == 10)[0][0]] == 1005.
    # each row matched at most once
    assert (~np.isnan(matched)).sum() == len(set(idx))


def test_match_counters_no_hits():
    valid_csv = np.column_stack((np.arange(5.), np.arange(5.)))
    df_txt = pd.DataFrame({"Counter": [999], "t_unixc_lin": [1.]})
    matched, stats, points = match_counters(valid_csv, df_txt)
    assert stats["matched"] == 0
    assert np.isnan(matched).all()
    assert points.shape == (0, 2)


def test_interpolate_timestamps_warns_on_extrapolation():
    # anchors cover only the middle third -> >5% extrapolated
    n = 100
    df = pd.DataFrame({"CDCT": np.arange(n, dtype=float)})
    anchor = np.full(n, np.nan)
    anchor[40:60] = np.arange(40, 60) * 2.0
    df["t_unix_anchor"] = anchor

    buf = io.StringIO()
    with redirect_stdout(buf):
        out, anchors = interpolate_timestamps(df)

    assert "WARNING" in buf.getvalue()
    assert len(out) == n
    assert len(anchors) == 20
    # linear fit -> value ~ 2*CDCT everywhere
    np.testing.assert_allclose(out, df["CDCT"].to_numpy() * 2.0, rtol=1e-9)
