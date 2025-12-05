Compose-first workflow for SAMplify_SuGaR

Overview
- Use Docker Compose to manage persistent containers for SAM2 (picker), COLMAP and SuGaR.
- Start services once, then run pipeline steps that `exec` into existing containers so they are reused.

Files
- `docker-compose.yml` — services: `sam2`, `colmap`, `sugar`.
- `.env.example` — copy to `.env` and set `DATASET_NAME` / `WEB_PORT`.
- `run_pipeline.sh` — orchestrates the pipeline; uses Compose to start services and `exec` for steps.

Quick start
1. Copy example env and edit values (optional):

```bash
cd /home/vaia/NEPHELE
cp .env.example .env
# edit .env to set DATASET_NAME and WEB_PORT if desired
```

2. Build images and start persistent services (run once):

```bash
docker compose build
# start persistent services
docker compose up -d sam2 colmap sugar
# confirm
docker compose ps
```

3. Run the pipeline (this script will still start sam2 if needed):

```bash
./run_pipeline.sh <DATASET_NAME>
```

Notes & tips
- Picker UI: the SAM2 picker will be served on the host port set in `.env` (`WEB_PORT`, default 8092). The script prints the URL to the terminal and log.
- Reuse containers: `docker compose exec -T <service>` is used to run commands inside running containers. This reuses the container and preserves any caches or downloads.
- GPU support: make sure NVIDIA Container Toolkit is installed. `docker compose` must be run on a system with GPU support and the Compose file uses `runtime: nvidia`.
- If a port is already in use, change `WEB_PORT` in `.env` before starting services.

If you want I can also add a short helper script to: build, start services and tail logs.
