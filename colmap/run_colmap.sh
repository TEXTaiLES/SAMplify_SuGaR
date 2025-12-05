#!/usr/bin/env bash
set -euo pipefail

IMAGES=${1:-"$nephele_PATH/colmap/input/${DATASET_NAME}"}
MASKS=${2:-"$nephele_PATH/colmap/input/${DATASET_NAME}_indexed"}
OUT=${3:-"$nephele_PATH/colmap/output/${DATASET_NAME}"}
MATCHER=${4:-exhaustive}  # or "sequential"


mkdir -p "$OUT"

# Build the container-side script into a variable so we can try `docker compose run`
# and fall back to `docker run --gpus all` on systems where compose run lacks GPU support.
CMD_SCRIPT=$(cat <<'INNER'
set -e

# --- Prepare masks in the naming COLMAP expects: <image_filename>.png ---
mkdir -p /tmp/masks_colmap
shopt -s nullglob
for img in /images/*.{jpg,JPG,jpeg,JPEG,png,PNG}; do
  [ -e "$img" ] || continue
  base="$(basename "$img")"
  stem="${base%.*}"

  if   [ -f "/masks/${base}.png" ]; then
    ln -sf "/masks/${base}.png" "/tmp/masks_colmap/${base}.png"
  elif [ -f "/masks/${base}" ]; then
    ln -sf "/masks/${base}" "/tmp/masks_colmap/${base}.png"
  elif [ -f "/masks/${stem}.png" ]; then
    ln -sf "/masks/${stem}.png" "/tmp/masks_colmap/${base}.png"
  elif [ -f "/masks/${stem}.jpg" ] || [ -f "/masks/${stem}.JPG" ]; then
    src="/masks/${stem}.jpg"
    [ -f "$src" ] || src="/masks/${stem}.JPG"
    ln -sf "$src" "/tmp/masks_colmap/${base}.png"
  fi
done
echo "Prepared $(ls -1 /tmp/masks_colmap | wc -l) mask links for COLMAP."

# --- COLMAP pipeline ---
colmap feature_extractor \
  --database_path /output/database.db \
  --image_path /images \
  --ImageReader.mask_path /tmp/masks_colmap \
  --SiftExtraction.max_num_features 10000 \
  --ImageReader.single_camera 1

if [ "__MATCHER__" = "exhaustive" ]; then
  colmap exhaustive_matcher --database_path /output/database.db
else
  colmap sequential_matcher --database_path /output/database.db \
    --SequentialMatching.loop_detection 0
fi

mkdir -p /output/sparse
colmap mapper \
  --database_path /output/database.db \
  --image_path /images \
  --output_path /output/sparse

INNER
)

# Inject the chosen matcher into the script
CMD_SCRIPT="${CMD_SCRIPT//__MATCHER__/$MATCHER}"

# Try docker compose run first if the compose file exists
set +e
COMPOSE_FILE="${nephele_PATH:-.}/docker-compose.yml"
if [ -f "$COMPOSE_FILE" ]; then
  echo "[i] Using compose file: $COMPOSE_FILE"
  docker compose -f "$COMPOSE_FILE" run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$IMAGES:/images:ro" \
    -v "$MASKS:/masks:ro" \
    -v "$OUT:/output" \
    colmap bash -lc "$CMD_SCRIPT"
  RC=$?
else
  echo "[i] Compose file not found at $COMPOSE_FILE — skipping docker compose run"
  RC=1
fi
set -e

if [ $RC -ne 0 ]; then
  echo "[!] docker compose run failed or unavailable (rc=$RC). Falling back to docker run --gpus all using image 'colmap/colmap'..."
  docker run --gpus all -it --rm \
    --user "$(id -u):$(id -g)" \
    -v "$IMAGES:/images:ro" \
    -v "$MASKS:/masks:ro" \
    -v "$OUT:/output" \
    colmap/colmap bash -lc "$CMD_SCRIPT"
fi

