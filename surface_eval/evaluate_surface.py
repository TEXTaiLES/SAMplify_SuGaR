#!/usr/bin/env python3
"""Point-to-surface mesh comparison (baseline / NBV-style reconstructions).

Unlike mesheval (point-to-point, expects inputs already in a shared frame,
e.g. built on the same COLMAP poses), this measures true distance to the
mesh *surface* via raycasting, and includes an optional pre-ICP alignment
step for inputs coming from a different frame/scale/unit convention (e.g.
a PyBullet simulation vs. a real-world baseline scan).

    python evaluate_surface.py gt.ply test.ply
    python evaluate_surface.py gt.ply test.ply --no-prealign   # already aligned
    python evaluate_surface.py gt.ply test.ply --gui           # interactive view
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import open3d as o3d

RANDOM_SEED = 200345


def seed_everything(seed: int) -> None:
    np.random.seed(seed)
    try:
        o3d.utility.random.seed(seed)
    except AttributeError:
        pass  # older Open3D: sampling stays non-deterministic


def detect_unit_scale(geom, override: str | None) -> float:
    if override == "mm":
        return 0.001
    if override == "m":
        return 1.0
    ext = geom.get_axis_aligned_bounding_box().get_max_extent()
    if ext > 100:
        print(f"  [units] extent={ext:.1f} -> guessing millimetres (pass --units to override)")
        return 0.001
    print(f"  [units] extent={ext:.4f} -> guessing metres (pass --units to override)")
    return 1.0


def clean_preprocess(pcd, voxel_size):
    clean, ind = pcd.remove_statistical_outlier(nb_neighbors=30, std_ratio=1.2)
    removed = len(pcd.points) - len(ind)
    if removed:
        print(f"  [preprocess] removed {removed} outlier points")
    down = clean.voxel_down_sample(voxel_size)
    down.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel_size * 2, max_nn=30))
    return down


def raycasting_scene(mesh):
    t_mesh = o3d.t.geometry.TriangleMesh.from_legacy(mesh)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(t_mesh)
    return scene


def point_to_surface(scene, points) -> np.ndarray:
    q = o3d.core.Tensor(points.astype(np.float32))
    return scene.compute_distance(q).numpy()


def prealign(test_mesh, gt_mesh, args):
    """Domain-specific heuristic pre-alignment for PyBullet-vs-baseline
    inputs: fixed X rotation, recenter, then scale the test mesh so its
    bounding-box extent matches the (rescaled) GT extent. Skip with
    --no-prealign when both inputs already share a frame/scale."""
    rad = math.radians(args.rotation_x)
    R = test_mesh.get_rotation_matrix_from_xyz((rad, 0.0, 0.0))
    test_mesh.rotate(R, center=test_mesh.get_center())
    gt_mesh.translate(-gt_mesh.get_center())
    test_mesh.translate(-test_mesh.get_center())

    gt_extent = gt_mesh.get_axis_aligned_bounding_box().get_max_extent()
    gt_scaled = gt_extent * args.scale
    test_extent = test_mesh.get_axis_aligned_bounding_box().get_max_extent()
    auto_scale = gt_scaled / test_extent
    print(f"  [prealign] gt extent {gt_extent:.5f} -> {gt_scaled:.5f} (x{args.scale})")
    print(f"  [prealign] test extent {test_extent:.5f} -> scale x{auto_scale:.6f}")

    gt_mesh.scale(args.scale, center=(0, 0, 0))
    test_mesh.scale(auto_scale, center=(0, 0, 0))


def icp_align(test_mesh, gt_mesh, voxel_size, n_points):
    src = clean_preprocess(test_mesh.sample_points_uniformly(n_points), voxel_size)
    dst = clean_preprocess(gt_mesh.sample_points_uniformly(n_points), voxel_size)
    reg = o3d.pipelines.registration.registration_icp(
        src, dst, voxel_size * 5, np.eye(4),
        o3d.pipelines.registration.TransformationEstimationPointToPoint(),
    )
    test_mesh.transform(reg.transformation)
    print(f"  [icp] fitness={reg.fitness:.4f}  inlier RMSE={reg.inlier_rmse:.5f}")
    return reg.fitness


def quality_report(dist_fwd, dist_bwd, t_perfect, t_medium) -> dict:
    pct_perfect = float(np.mean(dist_fwd <= t_perfect) * 100)
    pct_medium = float(np.mean((dist_fwd > t_perfect) & (dist_fwd <= t_medium)) * 100)
    pct_bad = float(np.mean(dist_fwd > t_medium) * 100)
    pct_covered = float(np.mean(dist_bwd <= t_medium) * 100)
    rmse = float(np.sqrt(np.mean(dist_fwd ** 2)))
    h_fwd, h_bwd = float(dist_fwd.max()), float(dist_bwd.max())
    h95_fwd, h95_bwd = float(np.percentile(dist_fwd, 95)), float(np.percentile(dist_bwd, 95))

    print("\n" + "=" * 60)
    print("MODEL QUALITY REPORT (point-to-surface)")
    print("=" * 60)
    print(f"  accuracy  perfect(<{t_perfect*1000:.1f}mm) {pct_perfect:6.2f}%   "
          f"tolerable {pct_medium:6.2f}%   large(>{t_medium*1000:.1f}mm) {pct_bad:6.2f}%")
    print(f"  coverage  {pct_covered:6.2f}%  (holes {100-pct_covered:6.2f}%)")
    print(f"  mean {np.mean(dist_fwd)*1000:.3f}mm  median {np.median(dist_fwd)*1000:.3f}mm  rmse {rmse*1000:.3f}mm")
    print(f"  hausdorff max  fwd {h_fwd*1000:.3f}mm  bwd {h_bwd*1000:.3f}mm")
    print(f"  hausdorff p95  fwd {h95_fwd*1000:.3f}mm  bwd {h95_bwd*1000:.3f}mm  (robust to single outliers)")
    print("=" * 60 + "\n")

    return dict(pct_perfect=pct_perfect, pct_medium=pct_medium, pct_bad=pct_bad,
                pct_covered=pct_covered, mean=float(np.mean(dist_fwd)), rmse=rmse,
                hausdorff_max=max(h_fwd, h_bwd), hausdorff_p95=max(h95_fwd, h95_bwd))


def jet_colors(values: np.ndarray) -> np.ndarray:
    """Blue->cyan->green->yellow->red ramp, no matplotlib dependency."""
    v = np.clip(values, 0, 1) * 4.0
    r = np.clip(np.minimum(v - 1.5, 4.5 - v), 0, 1)
    g = np.clip(np.minimum(v - 0.5, 3.5 - v), 0, 1)
    b = np.clip(np.minimum(v + 0.5, 2.5 - v), 0, 1)
    return np.stack([r, g, b], axis=1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gt", help="ground-truth / baseline mesh")
    ap.add_argument("test", help="mesh to evaluate")
    ap.add_argument("-o", "--out-dir", type=Path, default=Path("results"))
    ap.add_argument("-n", "--samples", type=int, default=100_000)
    ap.add_argument("--units", choices=["auto", "mm", "m"], default="auto")
    ap.add_argument("--no-prealign", action="store_true",
                     help="skip the fixed rotation/scale step (inputs already share a frame)")
    ap.add_argument("--rotation-x", type=float, default=-90.0,
                     help="prealign: degrees to rotate the test mesh about X (default: -90)")
    ap.add_argument("--scale", type=float, default=0.015,
                     help="prealign: GT scale factor, e.g. PyBullet units (default: 0.015)")
    ap.add_argument("--voxel-size", type=float, default=0.002)
    ap.add_argument("--y-threshold", type=float, default=None,
                     help="drop points with y below this (e.g. a PyBullet floor plane)")
    ap.add_argument("--gui", action="store_true", help="open an interactive Open3D window")
    args = ap.parse_args()

    seed_everything(RANDOM_SEED)

    print("loading meshes...")
    gt_mesh = o3d.io.read_triangle_mesh(args.gt)
    test_mesh = o3d.io.read_triangle_mesh(args.test)
    if gt_mesh.is_empty() or test_mesh.is_empty():
        raise SystemExit("empty or unreadable mesh — check the paths")
    if len(gt_mesh.triangles) == 0 or len(test_mesh.triangles) == 0:
        raise SystemExit("one input has no faces (it's a point cloud, not a mesh) — "
                          "use mesheval or evaluate_surface on a meshed reconstruction")
    print(f"  gt   {len(gt_mesh.vertices):,} verts, {len(gt_mesh.triangles):,} faces")
    print(f"  test {len(test_mesh.vertices):,} verts, {len(test_mesh.triangles):,} faces")

    gt_mesh.scale(detect_unit_scale(gt_mesh, args.units if args.units != "auto" else None), center=(0, 0, 0))
    test_mesh.scale(detect_unit_scale(test_mesh, args.units if args.units != "auto" else None), center=(0, 0, 0))

    if not args.no_prealign:
        prealign(test_mesh, gt_mesh, args)

    fitness = icp_align(test_mesh, gt_mesh, args.voxel_size, args.samples)

    gt_scale = gt_mesh.get_axis_aligned_bounding_box().get_max_extent()
    t_perfect, t_medium = gt_scale * 0.005, gt_scale * 0.015

    print("sampling + measuring point-to-surface distance...")
    test_pts = np.asarray(test_mesh.sample_points_uniformly(args.samples).points)
    gt_pts = np.asarray(gt_mesh.sample_points_uniformly(args.samples).points)
    dist_fwd = point_to_surface(raycasting_scene(gt_mesh), test_pts)     # test -> gt
    dist_bwd = point_to_surface(raycasting_scene(test_mesh), gt_pts)     # gt -> test

    if args.y_threshold is not None:
        mask_fwd = test_pts[:, 1] > args.y_threshold
        mask_bwd = gt_pts[:, 1] > args.y_threshold
        dropped = len(mask_fwd) - mask_fwd.sum()
        print(f"  [y-filter] dropped {dropped}/{len(mask_fwd)} test points below y={args.y_threshold}")
        dist_fwd, dist_bwd = dist_fwd[mask_fwd], dist_bwd[mask_bwd]
        if len(dist_fwd) == 0 or len(dist_bwd) == 0:
            raise SystemExit("nothing left after y-filtering — try a different --y-threshold")

    report = quality_report(dist_fwd, dist_bwd, t_perfect, t_medium)
    report["icp_fitness"] = fitness

    # colour the actual mesh (per-vertex), not just the sampled cloud
    vert_dist = point_to_surface(raycasting_scene(gt_mesh), np.asarray(test_mesh.vertices))
    colors = jet_colors(vert_dist / (t_medium * 1.5))
    if args.y_threshold is not None:
        below = np.asarray(test_mesh.vertices)[:, 1] <= args.y_threshold
        colors[below] = [0.2, 0.2, 0.2]
    test_mesh.vertex_colors = o3d.utility.Vector3dVector(colors)
    test_mesh.compute_vertex_normals()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    heatmap_path = args.out_dir / "surface_heatmap.ply"
    o3d.io.write_triangle_mesh(str(heatmap_path), test_mesh)
    print(f"heatmap mesh -> {heatmap_path}")

    if args.gui:
        gt_vis = gt_mesh
        gt_vis.paint_uniform_color([0.75, 0.75, 0.75])
        gt_vis.compute_vertex_normals()
        o3d.visualization.draw_geometries(
            [test_mesh, gt_vis],
            window_name=f"surface heatmap | fitness={fitness:.3f} | rmse={report['rmse']*1000:.2f}mm",
            width=1280, height=800,
        )
    else:
        renderer = o3d.visualization.rendering.OffscreenRenderer(1000, 1000)
        renderer.scene.set_background([1, 1, 1, 1])
        mat = o3d.visualization.rendering.MaterialRecord()
        mat.shader = "defaultLit"
        renderer.scene.add_geometry("test", test_mesh, mat)
        bb = test_mesh.get_axis_aligned_bounding_box()
        c, ext = bb.get_center(), bb.get_extent()
        diag = float(np.linalg.norm(ext))
        renderer.setup_camera(60.0, c, [c[0], c[1], c[2] + diag * 1.4], [0, -1, 0])
        png_path = args.out_dir / "surface_heatmap.png"
        o3d.io.write_image(str(png_path), renderer.render_to_image())
        print(f"preview      -> {png_path}  (pass --gui for an interactive view instead)")


if __name__ == "__main__":
    main()
