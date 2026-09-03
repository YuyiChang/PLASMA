"""Lightweight counter-based clock sync between an MSense CSV and a
YAMS-produced .txt reference file.

The YAMS .txt file carries a linearized Unix-clock estimate per packet
(t_unixc_lin); rows are matched to the CSV by hardware Counter value and
t_unixc_lin is used directly as the anchor timestamp for interpolation.

  1. `--csv` an 'ac.csv' file: the fitted CDCT -> t_unixc_lin interpolant is
     also propagated onto the sibling 'ppg.csv' next to it (AC and PPG share
     the device clock/counter).
  2. `--csv` anything else (e.g. an ECG file, which carries its own Counter +
     CDCT): only that file is synced.

Pure: no Gradio. matplotlib is imported lazily inside the plot helpers and
skipped entirely when `make_plots=False`.
"""
import os
from glob import glob

import numpy as np
import pandas as pd
from scipy import interpolate


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_yams_txt(txt_path):
    """Load a YAMS .txt file: whitespace-separated, no header.
    Columns: ENMO, Counter, t_unixc_lin
    """
    return pd.read_csv(txt_path, sep=r'\s+', header=None,
                       names=['ENMO', 'Counter', 't_unixc_lin'])


def load_csv(csv_path):
    df = pd.read_csv(csv_path)
    if 'CDCT' not in df.columns:
        df['CDCT'] = df['Timestamp']
    return df


# ---------------------------------------------------------------------------
# AC/PPG sibling discovery
# ---------------------------------------------------------------------------

def is_ac_file(csv_path):
    return os.path.basename(csv_path).lower().endswith('ac.csv')


def find_sibling_ppg(csv_path):
    """Look for a 'ppg.csv' file next to an 'ac.csv' file."""
    dirname = os.path.dirname(csv_path)
    basename = os.path.basename(csv_path)
    if not basename.lower().endswith('ac.csv'):
        return None

    prefix = basename[:-len('ac.csv')]
    guess = os.path.join(dirname, prefix + 'ppg.csv')
    if os.path.exists(guess):
        return guess

    candidates = sorted(glob(os.path.join(dirname, f'{prefix}*ppg.csv')))
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Counter matching
# ---------------------------------------------------------------------------

def match_counters(valid_csv, df_txt):
    """Match YAMS .txt rows to CSV rows by Counter value using a monotonic
    forward search (each CSV row matched at most once).

    valid_csv : (N, 2) array of (CDCT, Counter) from the CSV
    df_txt    : DataFrame with Counter, t_unixc_lin columns

    Returns (matched_t_unix, match_stats, matched_points).
    """
    matched_t_unix = np.full(len(valid_csv), np.nan)
    matched_points = []
    curr_idx = 0
    n_matched = 0

    for _, row in df_txt.iterrows():
        hits = np.where(valid_csv[:, 1] == row['Counter'])[0]
        hits = hits[hits >= curr_idx]
        if len(hits):
            idx = hits[0]
            matched_t_unix[idx] = row['t_unixc_lin']
            matched_points.append(valid_csv[idx])
            curr_idx = idx
            n_matched += 1

    n_txt = len(df_txt)
    match_stats = {
        'txt_packets':  n_txt,
        'matched':      n_matched,
        'match_rate_%': round(100 * n_matched / n_txt, 1) if n_txt else 0.0,
    }
    matched_points = np.array(matched_points) if matched_points else np.empty((0, 2))
    return matched_t_unix, match_stats, matched_points


# ---------------------------------------------------------------------------
# Timestamp interpolation
# ---------------------------------------------------------------------------

def interpolate_timestamps(df_csv):
    """Fit a linear interpolant CDCT -> t_unixc_lin from matched anchors."""
    anchors = df_csv.dropna(subset=['t_unix_anchor'])
    n_total = len(df_csv)

    t_min, t_max = anchors['CDCT'].min(), anchors['CDCT'].max()
    n_extrap = ((df_csv['CDCT'] < t_min) | (df_csv['CDCT'] > t_max)).sum()

    print(f'  anchors:           {len(anchors)}/{n_total}')
    print(f'  extrapolated rows: {n_extrap} ({100 * n_extrap / n_total:.1f}%)')
    if n_total and n_extrap / n_total > 0.05:
        print('  [WARNING] >5% of rows outside matched range')

    f = interpolate.interp1d(anchors['CDCT'], anchors['t_unix_anchor'],
                             fill_value='extrapolate')
    return f(df_csv['CDCT']), anchors


# ---------------------------------------------------------------------------
# Plots (matplotlib imported lazily)
# ---------------------------------------------------------------------------

def plot_counter_matching(valid_csv, matched_points, match_stats, out_dir, tag):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(valid_csv[:, 0], valid_csv[:, 1], color='lightgray', lw=0.8, label='CSV counter')
    if len(matched_points):
        ax.plot(matched_points[:, 0], matched_points[:, 1], 'o', ms=3, color='C0',
                label='matched (YAMS .txt)')
    ax.set_title(f"Counter matching — {tag}\n"
                 f"{match_stats['matched']}/{match_stats['txt_packets']} "
                 f"({match_stats['match_rate_%']}%)")
    ax.set_xlabel('CDCT')
    ax.set_ylabel('Counter')
    ax.legend()
    fig.tight_layout()
    path = os.path.join(out_dir, f'counter_matching_{tag}.png')
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_interpolation_quality(df_csv, anchors, out_dir, tag):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(df_csv['t_unixc_lin'], lw=0.8, label='interpolated')
    ax.plot(anchors.index, anchors['t_unix_anchor'], 'o', ms=3, label='anchors')
    ax.set_title(f'CDCT -> t_unixc_lin interpolation — {tag}')
    ax.set_xlabel('Row index')
    ax.set_ylabel('t_unixc_lin (s)')
    ax.legend()
    fig.tight_layout()
    path = os.path.join(out_dir, f'interpolation_{tag}.png')
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def save_synced(df_csv, csv_path, out_dir):
    out_path = os.path.join(out_dir, os.path.basename(csv_path).replace('.csv', '_synced.csv'))
    df_csv.to_csv(out_path, index=False)
    print(f'  saved: {os.path.basename(out_path)}')
    return out_path


def sync_csv_to_yams(csv_path, txt_path, out_dir, *, make_plots=True):
    """Match csv_path against txt_path by Counter, fit the CDCT -> t_unixc_lin
    interpolant, apply it, and save. Returns (df_synced, f_interp, plot_paths).
    """
    tag = os.path.splitext(os.path.basename(csv_path))[0]
    print(f'CSV: {os.path.basename(csv_path)}')
    print(f'TXT: {os.path.basename(txt_path)}')

    df_csv = load_csv(csv_path)
    df_txt = load_yams_txt(txt_path)

    valid_csv = np.column_stack((df_csv['CDCT'].to_numpy(), df_csv['Counter'].to_numpy()))

    matched_t_unix, match_stats, matched_points = match_counters(valid_csv, df_txt)
    df_csv['t_unix_anchor'] = matched_t_unix

    print('\n  Matching statistics:')
    print(f"    txt_packets={match_stats['txt_packets']}  "
          f"matched={match_stats['matched']}  "
          f"match_rate={match_stats['match_rate_%']}%")
    if match_stats['match_rate_%'] < 50:
        print('  [WARNING] low match rate')

    plot_paths = []
    if make_plots:
        plot_paths.append(plot_counter_matching(valid_csv, matched_points, match_stats, out_dir, tag))

    print('\n  Interpolation:')
    df_csv['t_unixc_lin'], anchors = interpolate_timestamps(df_csv)
    if make_plots:
        plot_paths.append(plot_interpolation_quality(df_csv, anchors, out_dir, tag))

    f_interp = interpolate.interp1d(anchors['CDCT'], anchors['t_unix_anchor'],
                                    fill_value='extrapolate')

    df_csv = df_csv.drop(columns=['t_unix_anchor'])
    save_synced(df_csv, csv_path, out_dir)

    return df_csv, f_interp, plot_paths


def apply_interp_to_csv(csv_path, f_interp, out_dir):
    """Propagate an already-fitted CDCT -> t_unixc_lin interpolant onto
    another CSV from the same device (e.g. PPG alongside AC)."""
    print(f'\nPropagating to: {os.path.basename(csv_path)}')
    df_csv = load_csv(csv_path)
    df_csv['t_unixc_lin'] = f_interp(df_csv['CDCT'])
    return save_synced(df_csv, csv_path, out_dir)


def sync_paths(csv_path, txt_path, out_dir, *, ppg_path=None, make_plots=True):
    """CLI-shaped convenience: sync `csv_path`, and if it's an ac.csv also
    propagate onto its sibling ppg.csv. Returns the list of written paths."""
    os.makedirs(out_dir, exist_ok=True)
    _, f_interp, plots = sync_csv_to_yams(csv_path, txt_path, out_dir, make_plots=make_plots)
    written = list(plots)
    written.append(os.path.join(out_dir, os.path.basename(csv_path).replace('.csv', '_synced.csv')))
    if is_ac_file(csv_path):
        sib = ppg_path or find_sibling_ppg(csv_path)
        if sib:
            written.append(apply_interp_to_csv(sib, f_interp, out_dir))
        else:
            print('\n[INFO] no sibling PPG file found next to the AC CSV — skipping propagation')
    else:
        print('\n[INFO] input is not an AC file — synced alone, no PPG propagation attempted')
    return written
