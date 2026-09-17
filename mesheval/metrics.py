"""Alignment and surface-distance metrics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import open3d as o3d


@dataclass
class Result:
    mean: float
    median: float
    rms: float
    hausdorff: float
    chamfer: float
    within_tol: float          # fraction in [0, 1]
    tolerance: float
    fitness: Optional[float]   # ICP fitness, None when alignment is skipped


def load(path: str) -> o3d.geometry.TriangleMesh:
    mesh = o3d.io.read_triangle_mesh(path)
    if mesh.is_empty():
        raise ValueError(f"empty or unreadable mesh: {path}")
    mesh.compute_vertex_normals()
    return mesh


def align(test, reference, threshold):
    """Refine test -> reference with point-to-plane ICP."""
    src = test.sample_points_uniformly(100_000)
    dst = reference.sample_points_uniformly(100_000)
    reg = o3d.pipelines.registration.registration_icp(
        src, dst, threshold, np.eye(4),
        o3d.pipelines.registration.TransformationEstimationPointToPlane(),
    )
    return test.transform(reg.transformation), reg.fitness


def surface_distance(points, surface) -> np.ndarray:
    """Unsigned distance from each point to the surface."""
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(surface))
    q = np.asarray(points.points, dtype=np.float32)
    return scene.compute_distance(o3d.core.Tensor(q)).numpy()


def evaluate(test, reference, samples=200_000, tolerance=None, do_align=True):
    """Return (Result, sampled test points, per-point distance)."""
    diag = float(np.linalg.norm(
        reference.get_axis_aligned_bounding_box().get_extent()))
    tol = tolerance if tolerance is not None else 0.01 * diag

    fitness = None
    if do_align:
        test, fitness = align(test, reference, threshold=0.02 * diag)

    test_pts = test.sample_points_uniformly(samples)
    ref_pts = reference.sample_points_uniformly(samples)
    d = surface_distance(test_pts, reference)      # test -> reference
    d_back = surface_distance(ref_pts, test)       # reference -> test

    result = Result(
        mean=float(d.mean()),
        median=float(np.median(d)),
        rms=float(np.sqrt((d ** 2).mean())),
        hausdorff=float(max(d.max(), d_back.max())),
        chamfer=float(d.mean() + d_back.mean()),
        within_tol=float((d < tol).mean()),
        tolerance=tol,
        fitness=fitness,
    )
    return result, test_pts, d
