#!/usr/bin/env bash
set -euo pipefail

# ====== ARGS / DATASET ======
DATASET_NAME="${DATASET_NAME:-${1:-}}"
: "${DATASET_NAME:?Usage: $0 DATASET_NAME  (or export DATASET_NAME first)}"

# ====== PATHS ======
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
nephele_PATH="${nephele_PATH:-$SCRIPT_DIR}"
SAM2_PATH="${SAM2_PATH:-${nephele_PATH}/SAM2}"
SUGAR_PATH="${SUGAR_PATH:-${nephele_PATH}/SUGAR/SuGaR}"
COLMAP_OUT_PATH="${COLMAP_OUT_PATH:-${nephele_PATH}/colmap}"

cd "$nephele_PATH"

# Where SAM2 expects input/output INSIDE the container:
IN_MNT_HOST="$SAM2_PATH/data/input"
OUT_MNT_HOST="$SAM2_PATH/data/output"
IN_MNT_CONT="/data/in"
OUT_MNT_CONT="/data/out"

# If you want INPUT to be dataset-specific, put images in: $IN_MNT_HOST/$DATASET_NAME
INPUT_SUBDIR="${INPUT_SUBDIR:-$DATASET_NAME}"
INPUT_CONT="$IN_MNT_CONT/$INPUT_SUBDIR"

# ====== LOGGING ======
LOGDIR="$nephele_PATH/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/${DATASET_NAME}_$(date +%Y%m%d_%H%M%S).log"
exec >>"$LOGFILE" 2>&1

on_error() {
  echo "STATUS: ERROR"
  echo "LOG: $LOGFILE"
  exit 1
}
trap on_error ERR

echo "======================================"
echo " SAM2 + COLMAP + SuGaR pipeline"
echo " Dataset:          $DATASET_NAME"
echo " SAM2_PATH:        $SAM2_PATH"
echo " SUGAR_PATH:       $SUGAR_PATH"
echo " Host IN:          $IN_MNT_HOST"
echo " Host OUT:         $OUT_MNT_HOST"
echo " Container INPUT:  $INPUT_CONT"
echo " Log:              $LOGFILE"
echo "======================================"

# ====== SANITY CHECKS ======
[[ -d "$SAM2_PATH" ]]   || { echo "SAM2 path not found: $SAM2_PATH"; exit 1; }
[[ -d "$SUGAR_PATH" ]]  || { echo "SuGaR path not found: $SUGAR_PATH"; exit 1; }
command -v docker >/dev/null || { echo "Docker not found in PATH."; exit 1; }

HOST_UID=$(id -u)
HOST_GID=$(id -g)
DOCKER_BIN="${DOCKER_BIN:-docker}"   # no sudo
# Export UID/GID so docker-compose can use them (compose variable interpolation)
export HOST_UID HOST_GID

# ====== Ensure mount folders exist (owned by you) ======
mkdir -p "$IN_MNT_HOST" "$OUT_MNT_HOST" "$IN_MNT_HOST/$INPUT_SUBDIR"
chmod -R u+rwX,g+rwX "$IN_MNT_HOST" "$OUT_MNT_HOST" || true

# ====== Image presence (sam2:local); build if missing with host UID/GID ======
if ! $DOCKER_BIN image inspect sam2:local >/dev/null 2>&1; then
  echo "Docker image 'sam2:local' not found. Building..."
  $DOCKER_BIN build \
    --build-arg UID="$HOST_UID" \
    --build-arg GID="$HOST_GID" \
    -t sam2:local "$SAM2_PATH"
fi

# ====== GUI or HEADLESS ======
GUI="${GUI:-1}"          # default GUI on
FRAME_IDX="${FRAME_IDX:-0}"
OBJ_ID="${OBJ_ID:-1}"

DOCKER_GUI_FLAGS=()
if [[ "$GUI" == "1" ]]; then
  if command -v xhost >/dev/null 2>&1; then
    xhost +local:docker >/dev/null 2>&1 || true
  fi
  : "${DISPLAY:=${DISPLAY:-:0}}"
  DOCKER_GUI_FLAGS=( -e DISPLAY="$DISPLAY" -v /tmp/.X11-unix:/tmp/.X11-unix )
else
  echo "[i] GUI=0 → running without X display."
fi

# ====== RUN SAM2 (picker + propagation) ======

echo "[*] Running SAM2 for dataset: $DATASET_NAME"
echo "[*] INPUT (container): $INPUT_CONT"
echo "[*] OUT   (container): $OUT_MNT_CONT"

mkdir -p "$SAM2_PATH/data/input/$DATASET_NAME" "$SAM2_PATH/data/output"
chmod -R u+rwX,g+rwX "$SAM2_PATH/data/input" "$SAM2_PATH/data/output" || true

# Auto-pick a free port starting at 8092
WEB_PORT="${WEB_PORT:-8092}"
if ss -ltn | awk '{print $4}' | grep -q ":${WEB_PORT}\$"; then
  for p in $(seq 8092 8110); do
    if ! ss -ltn | awk '{print $4}' | grep -q ":${p}\$"; then
      WEB_PORT=$p; break
    fi
  done
fi

# If a sam2 container is running and already maps container port 5000,
# prefer that host port so we don't pick a different port than the container exposes.
if $DOCKER_BIN compose ps -q sam2 >/dev/null 2>&1; then
  if [[ "$($DOCKER_BIN inspect -f '{{.State.Running}}' sam2 2>/dev/null || echo false)" == "true" ]]; then
    MAPPED="$($DOCKER_BIN compose port sam2 5000 2>/dev/null || true)"
    if [[ -n "$MAPPED" ]]; then
      # MAPPED looks like 0.0.0.0:8092 or [::]:8092 — extract the port after the last ':'
      HOST_PORT="${MAPPED##*:}"
      if [[ -n "$HOST_PORT" ]]; then
        echo "[*] sam2 is already running and maps container:5000 -> host:${HOST_PORT}; using that port"
        WEB_PORT="$HOST_PORT"
      fi
    fi
  fi
fi

echo "[*] Using WEB_PORT=$WEB_PORT"

# Export chosen WEB_PORT so docker compose uses the same host port mapping
export WEB_PORT

PICKER_NAME="sam2picker_${DATASET_NAME}_${WEB_PORT}"
$DOCKER_BIN rm -f "$PICKER_NAME" >/dev/null 2>&1 || true

# ====== INDEXED / FLAGS ======
INDEX_SUFFIX="${INDEX_SUFFIX:-_indexed}"
INDEXED_NAME="${INPUT_SUBDIR}${INDEX_SUFFIX}"
INDEXED_DIR="$OUT_MNT_HOST/${INDEXED_NAME}"
mkdir -p "$INDEXED_DIR"
chmod 775 "$INDEXED_DIR" || true

PROMPTS_HOST="${PROMPTS_HOST:-$INDEXED_DIR/prompts.json}"
DONE_FLAG="${DONE_FLAG:-$INDEXED_DIR/__picker_done.flag}"
USE_EXISTING_FLAG="${USE_EXISTING_FLAG:-$INDEXED_DIR/__use_existing.flag}"
rm -f "$DONE_FLAG" "$USE_EXISTING_FLAG"

echo "[*] Starting Flask point picker for '$DATASET_NAME' on http://localhost:${WEB_PORT}/ ..."

PICKER_SERVICE="sam2"

# Check whether the service/container already exists
SERVICE_ID="$($DOCKER_BIN compose ps -q "$PICKER_SERVICE" 2>/dev/null || true)"
if [[ -n "$SERVICE_ID" ]]; then
  # Container exists — check whether it's running
  if [[ "$($DOCKER_BIN inspect -f '{{.State.Running}}' "$PICKER_SERVICE" 2>/dev/null || echo false)" == "true" ]]; then
    echo "[*] $PICKER_SERVICE already running; picker should be at http://localhost:$WEB_PORT/"
    # Start the picker only if it's not already running inside the container
    if $DOCKER_BIN compose exec -T "$PICKER_SERVICE" bash -lc 'pgrep -f point_picker_flask.py >/dev/null 2>&1 || ps aux | grep -v grep | grep -q point_picker_flask.py' >/dev/null 2>&1; then
      echo "[*] Picker process already running inside $PICKER_SERVICE"
    else
      echo "[*] Starting picker inside running $PICKER_SERVICE container"
      $DOCKER_BIN compose exec -T "$PICKER_SERVICE" bash -lc "
        export DATASET_NAME=\"$DATASET_NAME\"
        export INPUT=\"/data/in/$DATASET_NAME\"
        export OUT=\"/data/out\"
        export INDEX_SUFFIX=\"$INDEX_SUFFIX\"
        export HF_HOME=/data/out/.cache/huggingface
        umask 0002
        nohup python3 -u /workspace/app/point_picker_flask.py > /workspace/logs/picker_${DATASET_NAME}.log 2>&1 &
      "
    fi
  else
    echo "[*] $PICKER_SERVICE exists but is stopped — starting it"
    $DOCKER_BIN compose start "$PICKER_SERVICE"
    echo "[*] Started $PICKER_SERVICE; starting picker"
    $DOCKER_BIN compose exec -T "$PICKER_SERVICE" bash -lc "
      export DATASET_NAME=\"$DATASET_NAME\"
      export INPUT=\"/data/in/$DATASET_NAME\"
      export OUT=\"/data/out\"
      export INDEX_SUFFIX=\"$INDEX_SUFFIX\"
      export HF_HOME=/data/out/.cache/huggingface
      umask 0002
      nohup python3 -u /workspace/app/point_picker_flask.py > /workspace/logs/picker_${DATASET_NAME}.log 2>&1 &
    "
  fi
else
  echo "[*] $PICKER_SERVICE not found — creating and starting it"
  $DOCKER_BIN compose up -d "$PICKER_SERVICE"
  echo "[*] Created and started $PICKER_SERVICE; starting picker"
  $DOCKER_BIN compose exec -T "$PICKER_SERVICE" bash -lc "
    export DATASET_NAME=\"$DATASET_NAME\"
    export INPUT=\"/data/in/$DATASET_NAME\"
    export OUT=\"/data/out\"
    export INDEX_SUFFIX=\"$INDEX_SUFFIX\"
    export HF_HOME=/data/out/.cache/huggingface
    umask 0002
    nohup python3 -u /workspace/app/point_picker_flask.py > /workspace/logs/picker_${DATASET_NAME}.log 2>&1 &
  "
fi

echo "[*] Open to select points: http://localhost:${WEB_PORT}/" | tee /dev/tty

echo "[*] Waiting for decision/save → $DONE_FLAG"
while :; do
  if [[ -f "$DONE_FLAG" ]]; then
    echo "[*] Picker signaled DONE_FLAG. Proceeding..."
    break
  fi
  sleep 1
done

$DOCKER_BIN stop "$PICKER_NAME" >/dev/null 2>&1 || true
$DOCKER_BIN rm -f "$PICKER_NAME" >/dev/null 2>&1 || true

# Use Existing vs Create New
if [[ -f "$USE_EXISTING_FLAG" ]]; then
  if [[ ! -f "$PROMPTS_HOST" ]]; then
    echo "[!] You chose 'Use existing' but prompts.json not found at: $PROMPTS_HOST"
    exit 1
  fi
  echo "[*] Using existing prompts: $PROMPTS_HOST"
else
  if [[ ! -f "$PROMPTS_HOST" ]]; then
    echo "[!] No prompts.json saved. Aborting."
    exit 1
  fi
  echo "[*] New prompts saved at: $PROMPTS_HOST"
fi

rm -f "$DONE_FLAG" "$USE_EXISTING_FLAG"

echo "[*] Running SAM2 propagation using saved prompts..."
# Run propagation inside the already-running sam2 container so we reuse the image/container
$DOCKER_BIN compose exec -T sam2 bash -lc "
  export DATASET_NAME=\"$DATASET_NAME\"
  export INPUT=\"/data/in/$DATASET_NAME\"
  export OUT=\"/data/out\"
  export INDEX_SUFFIX=\"$INDEX_SUFFIX\"
  export QUIET=0
  export MPLBACKEND=Agg
  export HF_HOME=/data/out/.cache/huggingface
  umask 0002
  python3 -u /workspace/app/video_predict.py
"
echo "[*] SAM2 finished successfully (until here)."

# ====== COLMAP STAGE ======
$DOCKER_BIN pull colmap/colmap

if [ -f "$COLMAP_OUT_PATH/run_colmap.sh" ]; then
  chmod +x "$COLMAP_OUT_PATH/run_colmap.sh"
else
  echo "[*] run_colmap.sh not found in $COLMAP_OUT_PATH (skipping copy)"
fi

cd "$COLMAP_OUT_PATH"

# Ensure COLMAP dirs exist and are writable by you
mkdir -p "$COLMAP_OUT_PATH/input" "$COLMAP_OUT_PATH/output"
chmod -R u+rwX,g+rwX "$COLMAP_OUT_PATH/input" || true

install -d -m 775 \
  "$COLMAP_OUT_PATH/input/$DATASET_NAME" \
  "$COLMAP_OUT_PATH/input/${DATASET_NAME}_indexed"

# ---- paths ----
IMAGES_SRC="$SAM2_PATH/data/input/${DATASET_NAME}_indexed"
MASKS_SRC="$SAM2_PATH/data/output/${DATASET_NAME}_indexed"
IMAGES_DST="$COLMAP_OUT_PATH/input/${DATASET_NAME}"
MASKS_DST="$COLMAP_OUT_PATH/input/${DATASET_NAME}_indexed"
OUT_DST="$COLMAP_OUT_PATH/output/${DATASET_NAME}"

# ---- ensure dest dirs ----
mkdir -p "$IMAGES_DST" "$MASKS_DST" "$OUT_DST"

# ---- copy only indexed images (jpg/jpeg/png) ----
rsync -a --delete \
  --include '*/' --include '*.jpg' --include '*.jpeg' --include '*.png' --exclude '*' \
  "${IMAGES_SRC}/" "${IMAGES_DST}/"

# ---- copy only binary masks, exclude preview overlays ----
rsync -a --delete \
  --include '*/' \
  --exclude 'preview/**' \
  --include '*.jpg' --include '*.jpeg' --include '*.png' --exclude '*' \
  "${MASKS_SRC}/" "${MASKS_DST}/"

echo "Copied images: $(find "$IMAGES_DST" -maxdepth 1 -type f | wc -l)"
echo "Copied masks : $(find "$MASKS_DST" -maxdepth 1 -type f | wc -l)"

# ---- run COLMAP ----
bash "$COLMAP_OUT_PATH/run_colmap.sh" \
  "$IMAGES_DST" \
  "$MASKS_DST" \
  "$OUT_DST" \
  exhaustive

# --- optionally stage helper files ---
if [ -f "$nephele_PATH/run_sugar_pipeline_with_sam.sh" ]; then
  echo "[*] Copying run_sugar_pipeline_with_sam.sh to $SUGAR_PATH"
  cp -f "$nephele_PATH/run_sugar_pipeline_with_sam.sh" "$SUGAR_PATH"
  chmod +x "$SUGAR_PATH/run_sugar_pipeline_with_sam.sh"
else
  echo "[*] run_sugar_pipeline_with_sam.sh not found in $nephele_PATH (skipping copy)"
fi

if [ -f "$nephele_PATH/Dockerfile_final" ]; then
  echo "[*] Copying Dockerfile and helpers to $SUGAR_PATH"
  cp -f "$nephele_PATH/Dockerfile_final" "$SUGAR_PATH"
  cp -f "$nephele_PATH/train.py" "$SUGAR_PATH/gaussian_splatting/"
  cp -f "$nephele_PATH/coarse_mesh.py" "$SUGAR_PATH/sugar_extractors/coarse_mesh.py"
else
  echo "[*] Dockerfile/train.py/coarse_mesh.py not found in $nephele_PATH (skipping copy)"
fi

# --- run SUGAR (pass DATASET_NAME as env) ---
echo "[*] Running SuGaR pipeline for dataset: $DATASET_NAME..."
cd "$SUGAR_PATH"

DATASET_NAME="$DATASET_NAME" \
SUGAR_PATH="$SUGAR_PATH" \
nephele_PATH="$nephele_PATH" \
bash ./run_sugar_pipeline_with_sam.sh "$DATASET_NAME"

echo "[*] Pipeline completed successfully!"
echo "Pipeline completed. Check log: $LOGFILE"
echo "[*] Pipeline completed" | tee /dev/tty
