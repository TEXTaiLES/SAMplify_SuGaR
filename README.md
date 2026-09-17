# mesheval

Geometric-accuracy evaluation for reconstructed meshes. Compare a mesh from a
Gaussian-splatting backend (SuGaR / PGSR / Fast-PGSR) against a more-trusted
reference — another backend, or a COLMAP-dense photogrammetry mesh — and get
quantitative metrics plus a visual error heatmap.

## Install

```bash
pip install -e .
```

Or run against the Open3D that already ships in the pipeline image, without
installing anything:

```bash
docker run --rm -v "$PWD:/w" -w /w fastpgsr:local \
  bash -lc 'source /opt/conda/etc/profile.d/conda.sh && conda activate fast-pgsr && \
            python -m mesheval reference.ply test.obj'
```

## Usage

```bash
evaluate-meshes reference.ply test.obj                 # ICP align + full metrics
evaluate-meshes ref.ply test.obj -t 0.005 -n 300000    # custom tolerance / sampling
evaluate-meshes ref.ply test.obj --no-align            # meshes already share a frame
```

Equivalent: `python -m mesheval reference.ply test.obj`.

## Output

| Metric | Meaning |
|--------|---------|
| mean / median / RMS | average surface distance, test → reference |
| Hausdorff | worst-case local error (floaters, missing parts) |
| Chamfer | bidirectional mean distance (standard 3DGS benchmark metric) |
| within tol | % of surface within the accuracy threshold |

Written to `results/`:

- `report.html` — self-contained page: metrics table, error histogram, and an
  **interactive 3D heatmap** (drag to rotate). Open it in any browser.
- `error_heatmap.ply` — the coloured point cloud, for CloudCompare / MeshLab.

Pass `--no-html` to skip the report.

## Layout

```
mesheval/
├── metrics.py      # alignment + surface-distance metrics
├── visualize.py    # error-heatmap point cloud
├── report.py       # self-contained HTML report
└── cli.py          # command-line entry point
```

## Photogrammetry reference (`make_reference.sh`)

Build a classic photogrammetry mesh from an existing COLMAP reconstruction —
COLMAP dense MVS + Poisson meshing — to compare *against* the splatting meshes.
Runs inside the CUDA `colmap/colmap:latest` image (no build needed):

```bash
./make_reference.sh <images_dir> <sparse_dir> <output_dir>
# e.g. object-only reference for the statue, same frame as the splat meshes:
./make_reference.sh \
  ../SAMplify_SuGaR/SAM2/data/output/statue_indexed_masked \
  ../SAMplify_SuGaR/colmap/output/statue/sparse/0 \
  results/statue_reference
```

Output: `<output_dir>/meshed-poisson.ply`. Because it reuses the same COLMAP
poses, it's in the same frame as SuGaR/PGSR/Fast-PGSR meshes, so:

```bash
python -m mesheval results/statue_reference/meshed-poisson.ply \
  ../SAMplify_SuGaR/FASTPGSR/outputs/statue/mesh/tsdf_fusion_post.obj --no-align
```

Needs a CUDA GPU (dense stereo is GPU-only). Use the **masked** images so the
reference is object-only and comparable to the background-free splat meshes.

## Notes

- Meshes from SuGaR / PGSR / Fast-PGSR built on the **same COLMAP** already
  share scale and pose — `--no-align`, or let the light ICP refine.
- The reference is a *reference*, not certified ground truth, unless it comes
  from a real scanner (LiDAR / structured light).
- Scale is consistent across backends but only **metric** if the COLMAP
  reconstruction was scaled to a real-world reference.
