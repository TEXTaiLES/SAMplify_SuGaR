# surface_eval

Point-to-**surface** mesh comparison, for inputs that don't already share a
frame/scale — e.g. a PyBullet-simulated reconstruction vs. a real-world
baseline scan. Adapted from an NBV-comparison script; cleaned up into a CLI
tool with a few correctness fixes.

## How this differs from `../mesheval`

`mesheval` assumes both meshes are already in the same frame (e.g. built on
the same COLMAP poses) and measures distance directly. `surface_eval` adds an
optional **pre-alignment** step (fixed rotation + bounding-box scale match)
before ICP, for inputs coming from genuinely different coordinate systems.
Use `mesheval` for SuGaR/PGSR/Fast-PGSR comparisons; use `surface_eval` for
cross-system comparisons like this one.

## Usage

```bash
python evaluate_surface.py gt.ply test.ply
python evaluate_surface.py gt.ply test.ply --no-prealign        # already aligned
python evaluate_surface.py gt.ply test.ply --scale 1.0 --rotation-x 90 --rotation-y 180
python evaluate_surface.py gt.ply test.ply --y-threshold -0.29  # drop a floor plane
python evaluate_surface.py gt.ply test.ply --gui                # interactive window (needs a display)
```

`--scale`: the default `0.015` is a PyBullet-specific convention — for two
real-world-scale meshes (e.g. a purchased/scanned ground-truth vs. a
pipeline reconstruction), use `--scale 1.0` instead, which keeps the
reference at its real size.

`--rotation-x/-y/-z`: an externally sourced reference mesh's up-axis/export
convention is rarely known up front. If ICP fitness comes back low (≲ 0.3)
on the first try, search a coarse grid and keep the rotation with the
highest fitness:
```bash
for rx in 0 90 180 270; do for ry in 0 90 180 270; do
  echo "x=$rx y=$ry:"
  python evaluate_surface.py gt.ply test.ply --scale 1.0 \
    --rotation-x "$rx" --rotation-y "$ry" --no-html 2>&1 | grep '\[icp\]'
done; done
```
A low fitness even at the best rotation usually means the two meshes
genuinely don't cover the same extent (e.g. a partial reconstruction vs. a
complete reference) rather than a remaining alignment problem — check
`coverage` in the report and the heatmap before assuming it's a rotation bug.

Default output (headless-safe): `results/surface_heatmap.ply` (the test mesh,
vertex-colored by distance — open in CloudCompare/MeshLab) and
`results/surface_heatmap.png` (a quick preview, rendered offscreen so it also
works on a GPU box with no display).

## What changed vs. the original script

- CLI args instead of hardcoded constants + edit-and-rerun.
- `--units` overrides the mm/m auto-detect; the heuristic now prints a loud
  warning instead of silently guessing (a wrong guess makes every reported
  mm figure meaningless).
- Reports **95th-percentile Hausdorff** alongside the raw max — the raw max
  is one stray outlier point away from being useless.
- Seeded sampling (numpy + Open3D RNG where available) for reproducible runs.
- Validates both inputs are real meshes with faces, not point clouds.
- No `matplotlib` dependency (small built-in jet-style colormap).
- Headless by default (`OffscreenRenderer`); `--gui` opens an interactive
  window only when you actually have a display.
