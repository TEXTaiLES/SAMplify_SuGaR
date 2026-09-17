#!/usr/bin/env bash

set -Eeuo pipefail

# ===== BASIC SAFE DEFAULTS (robust paths) =====

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Where the SuGaR repo lives: this script is inside it

SUGAR_PATH="${SUGAR_PATH:-$SCRIPT_DIR}"

# Monorepo root is two levels up from SuGaR (…/SUGAR/SuGaR -> …/)

MONO_ROOT="${nephele_PATH:-$(cd "$SUGAR_PATH/../.." && pwd)}"

# Other components relative to repo root (can be overridden from env)

SAM2_PATH="${SAM2_PATH:-$MONO_ROOT/SAM2}"

COLMAP_OUT_PATH="${COLMAP_OUT_PATH:-$MONO_ROOT/colmap}"

DOCKER_BIN="${DOCKER_BIN:-docker}"

UMASK="${UMASK:-0002}"

umask "$UMASK"

HOST_UID="$(id -u)"

HOST_GID="$(id -g)"

DATASET_NAME="${DATASET_NAME:-${1:-}}"

: "${DATASET_NAME:?Usage: $0 DATASET_NAME}"

REFINEMENT_TIME="${REFINEMENT_TIME:-medium}"

# Outputs go straight into outputs/<dataset>/ by default.
# Set OUTPUT_GROUP=<name> if you want to group multiple datasets together
# (e.g. OUTPUT_GROUP=primitive → outputs/primitive/<dataset>/).
# If DATASET_NAME already contains a folder (e.g., "myset/foo"), preserve it.

OUTPUT_GROUP="${OUTPUT_GROUP:-}"

if [[ "$DATASET_NAME" == */* ]]; then

DATASET_OUTPUT_PATH="$DATASET_NAME"

elif [[ -n "$OUTPUT_GROUP" ]]; then

DATASET_OUTPUT_PATH="$OUTPUT_GROUP/$DATASET_NAME"

else

DATASET_OUTPUT_PATH="$DATASET_NAME"

fi

# ========= LOGGING (μόνο σε αρχείο) =========

LOGDIR="$SUGAR_PATH/logs"

mkdir -p "$LOGDIR"

LOGFILE="$LOGDIR/${DATASET_OUTPUT_PATH}_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$(dirname "$LOGFILE")"

exec 3>&1 4>&2

restore_fds() { exec 1>&3 2>&4; }

on_error(){ restore_fds; echo "STATUS: ERROR" >&2; echo "LOG: $LOGFILE" >&2; exit 1; }

trap on_error ERR

trap restore_fds EXIT

exec >"$LOGFILE" 2>&1

echo "======================================"

echo " Running SuGaR pipeline with SAM2 outputs"

echo " Dataset: $DATASET_NAME"

echo " Refinement time: $REFINEMENT_TIME"

echo " Log: $LOGFILE"

echo "======================================"

# ========= HELPERS =========

owner_uid() { stat -c %u "$1" 2>/dev/null || echo -1; }

is_owner() { [[ "$(owner_uid "$1")" == "$HOST_UID" ]]; }

ensure_dir() {

local d="$1"

[[ -d "$d" ]] || mkdir -p "$d"

# setgid + group-writable ΜΟΝΟ αν είσαι owner (για να μη βγάζει errors)

if is_owner "$d"; then

chmod 2775 "$d" 2>/dev/null || true # drwxrwsr-x

fi

}

write_test() {

local d="$1"

local tmp="$d/.__w_test__"

if ! ( : > "$tmp" ) 2>/dev/null; then

echo "⚠️ No write access to $d. Fix host perms/ACLs."

else

rm -f "$tmp" || true

fi

}

# ========= 0. Paths & inputs =========

COLMAP_SPARSE_DIR="${COLMAP_SPARSE_DIR:-${COLMAP_OUT_PATH}/output/${DATASET_NAME}}"

# Consistent SuGaR working dirs

SUGAR_DATA_ROOT="$SUGAR_PATH/data/${DATASET_NAME}_masked"

SUGAR_OUT_ROOT="$SUGAR_PATH/outputs/${DATASET_OUTPUT_PATH}"

SUGAR_CACHE="$SUGAR_PATH/cache"

ensure_dir "$SUGAR_PATH/data"

ensure_dir "$SUGAR_PATH/outputs"

ensure_dir "$SUGAR_CACHE"

ensure_dir "$SUGAR_DATA_ROOT/images_sugar"

ensure_dir "$SUGAR_DATA_ROOT/distorted/sparse/0"

ensure_dir "$SUGAR_DATA_ROOT/input"

ensure_dir "$SUGAR_OUT_ROOT"

write_test "$SUGAR_OUT_ROOT"

write_test "$SUGAR_CACHE"

# ========= 1. Copy masked images from SAM2 =========

echo "[*] STEP 1: Copy SAM2 masked images → SuGaR input"

SAM2_MASK_DIR="$SAM2_PATH/data/output/${DATASET_NAME}_indexed_masked"

if [[ -d "$SAM2_MASK_DIR" ]]; then

shopt -s nullglob

for img in "$SAM2_MASK_DIR"/*; do

cp -f "$img" "$SUGAR_DATA_ROOT/input/" || true

cp -f "$img" "$SUGAR_DATA_ROOT/images_sugar/" || true

done

shopt -u nullglob

else

echo "[!] SAM2 masked dir not found: $SAM2_MASK_DIR (continuing)"

fi

# ========= 1b. Sync COLMAP sparse model =========

echo "[*] STEP 1b: Sync COLMAP sparse → SuGaR distorted"

DEST_DISTORTED="$SUGAR_DATA_ROOT/distorted"

ensure_dir "$DEST_DISTORTED/sparse/0"

write_test "$DEST_DISTORTED"

if [[ -d "$COLMAP_SPARSE_DIR/sparse/0" ]]; then

rsync -a --delete "${COLMAP_SPARSE_DIR}/" "${DEST_DISTORTED}/" || true

else

echo "[!] Expected COLMAP sparse at: $COLMAP_SPARSE_DIR/sparse/0 (continuing)"

fi

echo " files in distorted/sparse/0: $(ls -1 "${DEST_DISTORTED}/sparse/0" 2>/dev/null | wc -l)"

echo "[*] STEP 1: Directories prepared"

# ========= PRE-FLIGHT: Ensure images are JPG and COLMAP references match =========

echo "[*] PRE-FLIGHT: Ensuring all images are JPG and COLMAP references match"

SUGAR_DATA_ROOT="$SUGAR_DATA_ROOT" python3 - <<'PREFLIGHT'
import os, glob, shutil, re

data_root = os.environ.get('SUGAR_DATA_ROOT', '')
images_dir = os.path.join(data_root, 'images_sugar')
sparse_dir = os.path.join(data_root, 'distorted', 'sparse', '0')

# 1. Auto-convert any non-JPG images in images_sugar/ to JPG
all_imgs = glob.glob(os.path.join(images_dir, '*.*'))
non_jpgs = [f for f in all_imgs if os.path.splitext(f)[1].lower() not in ('.jpg', '.jpeg')]

if non_jpgs:
    from PIL import Image  # lazy import — only needed when conversion is required
    print(f"  Auto-converting {len(non_jpgs)} non-JPG image(s) to JPG...")
    for src in sorted(non_jpgs):
        jpg_path = os.path.splitext(src)[0] + '.jpg'
        Image.open(src).convert('RGB').save(jpg_path, 'JPEG', quality=95)
        os.remove(src)
        print(f"    {os.path.basename(src)} -> {os.path.basename(jpg_path)}")

jpg_imgs = (glob.glob(os.path.join(images_dir, '*.jpg'))
            + glob.glob(os.path.join(images_dir, '*.jpeg')))
print(f"  images_sugar: {len(jpg_imgs)} JPG(s)")

if not jpg_imgs:
    print("  [!] WARNING: No images in images_sugar/ — did SAM2/copy complete?")

# 2. Patch COLMAP to reference .jpg if it uses other extensions
images_bin = os.path.join(sparse_dir, 'images.bin')
images_txt = os.path.join(sparse_dir, 'images.txt')

if os.path.exists(images_bin):
    with open(images_bin, 'rb') as f:
        data = f.read()
    patched, count = data, 0
    for ext in (b'.png', b'.PNG'):  # same length as .jpg -> safe binary swap
        n = patched.count(ext + b'\x00')
        if n:
            patched = patched.replace(ext + b'\x00', b'.jpg\x00')
            count += n
    if count:
        shutil.copy2(images_bin, images_bin + '.preflight_orig')
        with open(images_bin, 'wb') as f:
            f.write(patched)
        print(f"  Patched images.bin: {count} filename(s) -> .jpg")
    else:
        print("  images.bin: already references .jpg — OK")
elif os.path.exists(images_txt):
    with open(images_txt) as f:
        content = f.read()
    new_content = re.sub(r'\.(png|tif|tiff|bmp)(?=(\s|$))', '.jpg', content, flags=re.IGNORECASE)
    if new_content != content:
        shutil.copy2(images_txt, images_txt + '.preflight_orig')
        with open(images_txt, 'w') as f:
            f.write(new_content)
        print("  Patched images.txt: non-JPG extensions -> .jpg")
    else:
        print("  images.txt: already references .jpg — OK")
else:
    print(f"  [!] WARNING: No images.bin or images.txt in {sparse_dir}")

print("  PRE-FLIGHT done.")
PREFLIGHT

# ========= 2. Build sugar-final image if missing =========

echo "[*] STEP 2: Build sugar-final (if needed)"

cd "$SUGAR_PATH"

if ! $DOCKER_BIN image inspect sugar-final >/dev/null 2>&1; then

$DOCKER_BIN build \
	--build-arg USER_ID=$(id -u) \
	--build-arg GROUP_ID=$(id -g) \
	-t sugar-final -t sugar:local -f Dockerfile_final .

else

echo "[i] sugar-final image already present."

fi

# ========= Common container env/volumes =========

VOL_DATA="-v $SUGAR_DATA_ROOT:/app/data"

VOL_OUT="-v $SUGAR_OUT_ROOT:/app/output"

VOL_CACHE="-v $SUGAR_CACHE:/app/.cache"

ENV_COMMON=(

-e HOME=/app

-e XDG_CACHE_HOME=/app/.cache

-e TORCH_EXTENSIONS_DIR=/app/.cache/torch_extensions

-e HF_HOME=/app/.cache/hf

-e TRANSFORMERS_CACHE=/app/.cache/hf

-e HUGGINGFACE_HUB_CACHE=/app/.cache/hf

-e MPLBACKEND=Agg

)

DOCKER_COMMON=(

--gpus all --rm --ipc=host

--user "${HOST_UID}:${HOST_GID}"

$VOL_DATA $VOL_OUT $VOL_CACHE

"${ENV_COMMON[@]}"

)

# Compose fallback: prefer docker compose run if a compose file exists in MONO_ROOT

if [ -z "${COMPOSE_FILE:-}" ]; then COMPOSE_FILE="${MONO_ROOT:-.}/docker-compose.yml"; fi

# Flags for compose run (omit --gpus and --ipc which may not be supported)

DOCKER_COMMON_COMPOSE=(

--rm

--user "${HOST_UID}:${HOST_GID}"

$VOL_DATA $VOL_OUT $VOL_CACHE

"${ENV_COMMON[@]}"

)

run_in_sugar() {

local script="$1"

# Redirect workspace output → /app/output so all steps write to SUGAR_OUT_ROOT

local REDIRECT_OUT='mkdir -p /app/output && rm -rf /workspace/output && ln -sfn /app/output /workspace/output'

local full_script="${REDIRECT_OUT}

${script}"

# Handle colon-separated COMPOSE_FILE (base.yml:override.yml)

local -a CF_FLAGS=()

local _cf _cf_ok=true

IFS=':' read -ra _CF_PARTS <<< "$COMPOSE_FILE"

for _cf in "${_CF_PARTS[@]}"; do

[ -f "$_cf" ] && CF_FLAGS+=(-f "$_cf") || { _cf_ok=false; break; }

done

set +e

if $_cf_ok && [ ${#CF_FLAGS[@]} -gt 0 ]; then

echo "[i] Running via docker compose: $COMPOSE_FILE"

docker compose "${CF_FLAGS[@]}" run "${DOCKER_COMMON_COMPOSE[@]}" sugar bash -lc "$full_script"

RC=$?

else

echo "[i] Compose file issue: $COMPOSE_FILE; skipping compose run"

RC=1

fi

set -e

if [ $RC -ne 0 ]; then

echo "[i] Falling back to docker run --gpus all with image 'sugar-final' (rc=$RC)"

docker run --gpus all "${DOCKER_COMMON[@]}" sugar-final bash -lc "$full_script"

fi

}

# Run a project python helper inside the sugar image (which has numpy/PIL/scipy)
# instead of the host python3, which does not. Mounts the same data/output dirs
# as the training container plus the helper script itself (so a stale prebuilt
# image still runs the current override).
run_py_in_sugar() {
	local script="$1"; shift
	$DOCKER_BIN run --rm -u "${HOST_UID}:${HOST_GID}" -e HOME=/tmp \
		-v "$SUGAR_DATA_ROOT:/app/data" \
		-v "$SUGAR_OUT_ROOT:/app/output" \
		-v "$SUGAR_PATH/$script:/app/$script:ro" \
		sugar:local bash -lc \
		"source /opt/conda/etc/profile.d/conda.sh && conda activate sugar && python /app/$script $*"
}

# ========= 3. Convert (COLMAP format → SuGaR) =========

echo "[*] STEP 3: convert.py"

run_in_sugar '

set -e

umask 0002

mkdir -p /app/.cache/hf /app/output

/app/run_with_xvfb.sh python /app/gaussian_splatting/convert.py -s /app/data --skip_matching

'

echo "======================================"

echo " DONE! Dataset conversion completed."

# ========= 3b. Convert undistorted images to RGBA PNG (alpha-mask background) =========

echo "[*] STEP 3b: Convert undistorted images to RGBA PNG"

run_py_in_sugar convert_to_rgba.py \
	/app/data/images \
	/app/data/sparse/0/images.bin \
	2 "${BLACK_THRESHOLD:-15}"

# ========= 4. SuGaR training =========

echo "[*] STEP 4: SuGaR training"

run_in_sugar \
"/app/run_with_xvfb.sh python train_full_pipeline.py \
-s /app/data -r dn_consistency --refinement_time \"$REFINEMENT_TIME\" \
--export_obj True --postprocess_mesh True --postprocess_density_threshold 0.1 --postprocess_iterations 5"

echo "======================================"

echo " DONE! SuGaR training completed."

# ========= 5. Extract refined textured mesh =========

echo "[*] STEP 5: Extract refined textured mesh"

run_in_sugar '

set -e

umask 0002

# Search in both /app/output (direct docker run) and ./output (compose with /workspace CWD)

REF=$(find /app/output/refined/ ./output/refined/ -type f -name "*.pt" 2>/dev/null | sort -t/ -k1 | tail -n1 || true)

if [ -z "$REF" ]; then

echo "[!] No refined checkpoint found, skipping mesh extraction."

exit 0

fi

# Resolve to absolute path to prevent circular symlink (./output → /tmp/output → ./output)

REF=$(realpath "$REF" 2>/dev/null || readlink -f "$REF")

echo "Using refined checkpoint: $REF"

# Resolve output root from checkpoint path (strip /refined/...)

OUT_ROOT=$(echo "$REF" | sed "s|/refined/.*||")

echo "[i] Output root: $OUT_ROOT"

VANILLA_GS="$OUT_ROOT/vanilla_gs/data"

cp -r /app/sugar_utils /tmp/sugar_utils

sed -i "s/RasterizeGLContext()/RasterizeCudaContext()/g" /tmp/sugar_utils/mesh_rasterization.py

ln -sfn "$OUT_ROOT" /tmp/output

cd /tmp

PYTHONPATH=/tmp:/app:/app/gaussian_splatting:$PYTHONPATH \
python -m extract_refined_mesh_with_texture \
-s /app/data \
-c "$VANILLA_GS" \
-m "$REF" \
-o "$OUT_ROOT/refined_mesh/data" \
--square_size 24

'

# ========= 5b. Remove small disconnected mesh components + base crop =========

echo "[*] STEP 5b: Mesh cleanup (small components + base crop)"

# BOTTOM_CROP_PCT: bottom % of Z-axis to crop (removes Poisson base skirt)

BOTTOM_CROP_PCT="${BOTTOM_CROP_PCT:-22}"

MESH_OBJ=$(find "$SUGAR_OUT_ROOT/refined_mesh" -name "*_postprocessed.obj" | grep -v artifacts | grep -v before_cleanup | head -n1 || true)

if [[ -n "$MESH_OBJ" && -f "$SUGAR_PATH/cleanup_mesh.py" ]]; then

MESH_OBJ_CONT="/app/output/${MESH_OBJ#"$SUGAR_OUT_ROOT"/}"
run_py_in_sugar cleanup_mesh.py "$MESH_OBJ_CONT" 0.05 "$BOTTOM_CROP_PCT" 2

elif [[ -z "$MESH_OBJ" ]]; then

echo "[!] No postprocessed .obj found in refined_mesh, skipping cleanup"

else

echo "[i] cleanup_mesh.py not present — skipping optional mesh cleanup (refined mesh already written)"

fi

restore_fds

echo "STATUS: MESH DONE"

echo "LOG: $LOGFILE"
