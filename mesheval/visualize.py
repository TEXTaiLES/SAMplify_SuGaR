"""Error-heatmap output."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import open3d as o3d


def heatmap(points, distances, vmax, out_path: Path) -> Path:
    """Colour points green (accurate) to red (far) and write a point cloud."""
    t = np.clip(distances / vmax, 0.0, 1.0)
    points.colors = o3d.utility.Vector3dVector(
        np.stack([t, 1.0 - t, np.zeros_like(t)], axis=1))
    o3d.io.write_point_cloud(str(out_path), points)
    return out_path
