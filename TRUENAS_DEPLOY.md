# TrueNAS Deployment

This repository is already wired for a TrueNAS SCALE custom-app deployment using a published GHCR image:

- image: `ghcr.io/natex-corporation/insider-trading:latest`
- branch: `main`
- publish workflow: [.github/workflows/publish-image.yml](.github/workflows/publish-image.yml)

## What You Still Need

You still need three things that cannot be completed safely from this machine:

1. fresh Alpaca paper-trading credentials
2. a TrueNAS dataset path such as `/mnt/tank/apps/insider-trading`
3. a push to GitHub so the image is published to GHCR

## What Is Already Prepared

- [Dockerfile](Dockerfile) builds the runtime image
- [monitoring.py](monitoring.py) exposes `/`, `/status`, `/metrics`, and `/healthz`
- [scripts/healthcheck.py](scripts/healthcheck.py) marks the container unhealthy if heartbeats stop
- [truenas-compose.yaml](truenas-compose.yaml) is a ready template for TrueNAS
- [scripts/render_truenas_compose.py](scripts/render_truenas_compose.py) generates a final YAML with your dataset path and secrets

## Generate The Final YAML

Set your environment variables, then run:

```bash
export ALPACA_API_KEY="your-new-paper-key"
export ALPACA_SECRET_KEY="your-new-paper-secret"
export TRUENAS_DATASET_PATH="/mnt/tank/apps/insider-trading"
python scripts/render_truenas_compose.py
```

That writes `truenas-compose.generated.yaml`.

On PowerShell:

```powershell
$env:ALPACA_API_KEY="your-new-paper-key"
$env:ALPACA_SECRET_KEY="your-new-paper-secret"
$env:TRUENAS_DATASET_PATH="/mnt/tank/apps/insider-trading"
python scripts/render_truenas_compose.py
```

If you need a different monitoring port:

```powershell
$env:TRUENAS_HOST_PORT="8081"
python scripts/render_truenas_compose.py
```

## Publish The Image

Push the repository to GitHub:

```bash
git add .
git commit -m "Prepare TrueNAS deployment"
git push origin main
```

After the GitHub Actions workflow finishes, TrueNAS can pull:

`ghcr.io/natex-corporation/insider-trading:latest`

If the package is private, add GHCR registry credentials in TrueNAS first. If possible, make the package public to simplify the deployment.

## Install In TrueNAS

1. Create the dataset directory you want to mount.
2. Open `Apps` in the TrueNAS UI.
3. Choose `Install via YAML`.
4. Paste the contents of `truenas-compose.generated.yaml`.
5. Save and wait for the app to start.
6. Open `http://<truenas-ip>:8080/` to view bot status.

## Check The Deployment

Expected endpoints:

- `/` HTML status page
- `/healthz` container liveness
- `/status` JSON status
- `/metrics` Prometheus-style metrics

Expected persisted files in the mounted dataset:

- `trade_history.csv`
- `pending_orders.json`
- `seen_insider_trades.log`
- `heartbeat.txt`
- `logs/app.log` or `app.log`
