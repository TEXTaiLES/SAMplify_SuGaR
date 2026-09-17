#!/usr/bin/env bash
# Build a photogrammetry reference mesh from an existing COLMAP reconstruction
# using COLMAP dense MVS + Poisson meshing, inside the CUDA colmap image.
#
#   ./make_reference.sh <images_dir> <sparse_dir> <output_dir>
#
# <images_dir>  images COLMAP registered (use the MASKED ones for an object-only
#               mesh; their filenames must match the sparse model).
# <sparse_dir>  folder with cameras.bin/images.bin/points3D.bin (e.g. .../sparse/0).
# <output_dir>  workspace; the result is <output_dir>/meshed-poisson.ply.
#
# The mesh lands in the SAME coordinate frame as the input reconstruction, so a
# splatting mesh built on the same COLMAP compares with `--no-align`.
set -Eeuo pipefail

IMAGES="${1:?usage: $0 <images_dir> <sparse_dir> <output_dir>}"
SPARSE="${2:?usage: $0 <images_dir> <sparse_dir> <output_dir>}"
OUT="${3:?usage: $0 <images_dir> <sparse_dir> <output_dir>}"
IMAGE="${COLMAP_IMAGE:-colmap/colmap:latest}"
MAX_SIZE="${MAX_IMAGE_SIZE:-1600}"

mkdir -p "$OUT"
IMAGES="$(cd "$IMAGES" && pwd)"; SPARSE="$(cd "$SPARSE" && pwd)"; OUT="$(cd "$OUT" && pwd)"

run() {
	docker run --rm --gpus all -u "$(id -u):$(id -g)" -e HOME=/tmp \
		-v "$IMAGES:/images:ro" -v "$SPARSE:/sparse:ro" -v "$OUT:/work" \
		"$IMAGE" "$@"
}

echo "[1/4] undistort images"
run colmap image_undistorter --image_path /images --input_path /sparse \
	--output_path /work --output_type COLMAP --max_image_size "$MAX_SIZE"

echo "[2/4] dense stereo (GPU) — the slow step"
run colmap patch_match_stereo --workspace_path /work --workspace_format COLMAP \
	--PatchMatchStereo.geom_consistency true

echo "[3/4] fuse dense point cloud"
run colmap stereo_fusion --workspace_path /work --workspace_format COLMAP \
	--input_type geometric --output_path /work/fused.ply

echo "[4/4] Poisson mesh"
run colmap poisson_mesher --input_path /work/fused.ply \
	--output_path /work/meshed-poisson.ply

echo
echo "reference mesh -> $OUT/meshed-poisson.ply"
