"""Self-contained HTML report — same idea as mesheval/report.py."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np

_PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"

# Palette/type borrowed from the TEXTaiLES portal (Lexend Deca, #265d72).
_CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lexend+Deca:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --primary: #265d72; --primary-light: #e8eef1; --text: #1c2b33;
    --muted: #6b7c85; --border: #e2e8ec; --bg: #f7f9fa;
  }
  * { box-sizing: border-box; }
  body { font-family: 'Lexend Deca', -apple-system, system-ui, sans-serif;
         margin: 0; background: var(--bg); color: var(--text); }
  .wrap { max-width: 1040px; margin: 0 auto; padding: 32px 20px 48px; }
  header.top { display: flex; align-items: center; gap: 14px; margin-bottom: 24px;
               border-bottom: 3px solid var(--primary); padding-bottom: 16px; }
  header.top .badge { width: 42px; height: 42px; border-radius: 10px; background: var(--primary);
                       display: flex; align-items: center; justify-content: center;
                       color: #fff; font-weight: 700; font-size: 1.2rem; flex-shrink: 0; }
  h1 { font-size: 1.5rem; margin: 0; font-weight: 600; letter-spacing: -.01em; }
  h2 { font-size: .95rem; margin: 0 0 12px; font-weight: 600; color: var(--primary); }
  .muted { color: var(--muted); margin: 2px 0 0; font-size: .85rem; }
  .meta { display: flex; flex-wrap: wrap; gap: 10px 28px; margin-bottom: 22px; }
  .meta div { display: flex; flex-direction: column; }
  .meta span { font-size: .68rem; text-transform: uppercase; letter-spacing: .06em;
               color: var(--muted); font-weight: 600; margin-bottom: 3px; }
  .meta code { font-family: ui-monospace, Menlo, monospace; font-size: .82rem; color: var(--text);
               background: var(--primary-light); padding: 2px 7px; border-radius: 5px; }
  .grid { display: grid; grid-template-columns: 300px 1fr; gap: 18px; margin-bottom: 18px; }
  .card { background: #fff; border: 1px solid var(--border); border-radius: 12px;
          padding: 18px 20px; box-shadow: 0 1px 3px rgba(38, 93, 114, .06); }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 9px 4px; border-bottom: 1px solid var(--border); font-size: .9rem; }
  tr:last-child td { border-bottom: none; }
  td:last-child { text-align: right; font-variant-numeric: tabular-nums;
                  font-weight: 700; color: var(--text); }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
</style>
"""


def html_report(report: dict, points: np.ndarray, distances: np.ndarray,
                 gt_path: str, test_path: str, t_medium: float,
                 out_path: Path, max_points: int = 40_000) -> Optional[Path]:
    """Write an interactive HTML report. Returns None if plotly is unavailable."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return None

    pts, d = points, distances
    if len(pts) > max_points:
        idx = np.random.default_rng(0).choice(len(pts), max_points, replace=False)
        pts, d = pts[idx], d[idx]

    vmax = 1.5 * t_medium
    view = go.Figure(go.Scatter3d(
        x=pts[:, 0], y=pts[:, 1], z=pts[:, 2], mode="markers", hoverinfo="skip",
        marker=dict(size=1.6, color=d, colorscale="Turbo", cmin=0, cmax=vmax,
                    colorbar=dict(title="dist (m)")),
    ))
    view.update_layout(scene=dict(aspectmode="data"),
                        margin=dict(l=0, r=0, t=0, b=0), height=560)

    hist = go.Figure(go.Histogram(x=d, nbinsx=60, marker_color="#265d72"))
    hist.add_vline(x=t_medium, line_dash="dash", line_color="#e45756", annotation_text="tol")
    hist.update_layout(margin=dict(l=48, r=10, t=10, b=40), height=300, bargap=0.02,
                        xaxis_title="surface distance (m)", yaxis_title="points",
                        font=dict(family="Lexend Deca, sans-serif"))

    view_div = view.to_html(full_html=False, include_plotlyjs=False)
    hist_div = hist.to_html(full_html=False, include_plotlyjs=False)

    rows = [
        ("mean", f"{report['mean']*1000:.3f} mm"),
        ("RMSE", f"{report['rmse']*1000:.3f} mm"),
        ("Hausdorff (max)", f"{report['hausdorff_max']*1000:.3f} mm"),
        ("Hausdorff (p95)", f"{report['hausdorff_p95']*1000:.3f} mm"),
        ("perfect", f"{report['pct_perfect']:.2f}%"),
        ("tolerable", f"{report['pct_medium']:.2f}%"),
        ("large error", f"{report['pct_bad']:.2f}%"),
        ("coverage", f"{report['pct_covered']:.2f}%"),
    ]
    body_rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    fitness = f"{report['icp_fitness']:.3f}" if report.get("icp_fitness") is not None else "—"

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Surface evaluation</title>
<script src="{_PLOTLY_CDN}"></script>{_CSS}</head>
<body><div class="wrap">
<header class="top">
  <div class="badge">3D</div>
  <div><h1>Surface evaluation</h1><p class="muted">{date.today().isoformat()}</p></div>
</header>
<section class="meta">
  <div><span>ground truth</span><code>{gt_path}</code></div>
  <div><span>test</span><code>{test_path}</code></div>
  <div><span>ICP fitness</span><code>{fitness}</code></div>
</section>
<section class="grid">
  <div class="card"><h2>Metrics</h2><table>{body_rows}</table></div>
  <div class="card"><h2>Error distribution</h2>{hist_div}</div>
</section>
<section class="card"><h2>Error heatmap (drag to rotate)</h2>{view_div}</section>
</div></body></html>"""

    out_path.write_text(html, encoding="utf-8")
    return out_path
