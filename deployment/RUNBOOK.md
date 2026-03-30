# AGR AI Curation Deployment Runbook

Target: `curation-test.geneontology.io`

---

## 1. Prerequisites

Before starting you need:

- **AWS credentials** with EC2/VPC/EIP permissions in account `162763300381`, region `us-east-1`
- **SSH key pair** (public key file for import, e.g. `go-ssh.pub`)
- **GitHub OAuth App** registered at `https://github.com/settings/developers` with callback URL `https://curation-test.geneontology.io/api/auth/callback`. Note the Client ID and Client Secret.
- **Cloudflare account** with DNS zone for `geneontology.io` (partial DNS setup with Route 53 as authoritative)
- **OpenAI API key**
- **A JWT signing secret** (any random string, e.g. `openssl rand -hex 32`)
- **GitHub users.yaml URL** for authorization, e.g. `https://raw.githubusercontent.com/geneontology/go-site/master/metadata/users.yaml`

Set your AWS credentials for every shell session:

```bash
export AWS_SHARED_CREDENTIALS_FILE=/home/sjcarbon/local/share/secrets/go/credentials/go-aws-credentials
export AWS_REGION=us-east-1
```

---

## 2. Deploy

### 2.1 Import SSH key pair

```bash
aws ec2 import-key-pair --key-name go-ssh \
  --public-key-material fileb:///home/sjcarbon/local/share/secrets/go/ssh-keys/go-ssh.pub \
  --region us-east-1
```

### 2.2 Create security group

Must be in the `go-production` VPC.

```bash
SG_ID=$(aws ec2 create-security-group --group-name agr-ai-curation-test \
  --description "AGR AI Curation test deployment" \
  --vpc-id vpc-0926e26ef0721bf97 --region us-east-1 \
  --query 'GroupId' --output text)
echo "Security Group: $SG_ID"

aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --region us-east-1 --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=128.3.112.0/21,Description=LBL-SSH}]" \
  "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=HTTP}]" \
  "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description=HTTPS}]"
```

### 2.3 Launch EC2 instance

```bash
INSTANCE_ID=$(aws ec2 run-instances --region us-east-1 \
  --image-id ami-0462ececcfe0a450f --instance-type t3.xlarge \
  --key-name go-ssh --security-group-ids "$SG_ID" \
  --subnet-id subnet-0a09e8ea837f8606b --associate-public-ip-address \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":100,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=agr-ai-curation-test}]' \
  --query 'Instances[0].InstanceId' --output text)
echo "Instance: $INSTANCE_ID"

aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region us-east-1
```

### 2.4 Allocate and associate Elastic IP

```bash
ALLOC_ID=$(aws ec2 allocate-address --domain vpc --region us-east-1 \
  --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Name,Value=agr-ai-curation-test}]' \
  --query 'AllocationId' --output text)
EIP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" --region us-east-1 \
  --query 'Addresses[0].PublicIp' --output text)
echo "Elastic IP: $EIP (Allocation: $ALLOC_ID)"

ASSOC_ID=$(aws ec2 associate-address --instance-id "$INSTANCE_ID" \
  --allocation-id "$ALLOC_ID" --region us-east-1 \
  --query 'AssociationId' --output text)
echo "Association: $ASSOC_ID"
```

### 2.5 DNS setup

**Route 53**: Create a CNAME record for `curation-test.geneontology.io` pointing to Cloudflare (this is the partial-DNS setup where Route 53 is authoritative but traffic is proxied through Cloudflare).

**Cloudflare**:

1. Add the site/domain with **partial (CNAME) DNS setup** (not full nameserver delegation).
2. Add a CNAME record for `curation-test` pointing to the Elastic IP (use an A record equivalent via Cloudflare, proxied).
3. **DCV Delegation**: Cloudflare needs a DCV delegation CNAME in Route 53 for edge certificate provisioning. Add the CNAME Cloudflare tells you (typically `_acme-challenge.curation-test.geneontology.io` pointing to a Cloudflare DCV target). This can take a few minutes to validate.
4. **SSL mode**: Set to **Full** (not Flexible, not Full Strict). The origin uses a self-signed cert, so Full Strict will fail.

### 2.6 Bootstrap the EC2 instance

SSH in:

```bash
ssh -i /home/sjcarbon/local/share/secrets/go/ssh-keys/go-ssh ubuntu@$EIP
```

Install dependencies:

```bash
sudo apt-get update && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
sudo apt-get install -y docker-compose-plugin nginx git
newgrp docker
```

### 2.7 Clone the repo and run installer

```bash
cd ~
git clone https://github.com/geneontology/agr_ai_curation.git
cd agr_ai_curation
git checkout <your-branch>
```

Run the installer with skips (interactive stdin can truncate keys, so we fix them after):

```bash
bash scripts/install/install.sh \
  --package-profile core-plus-alliance \
  --skip-auth-setup \
  --skip-group-setup \
  --skip-pdfx-setup \
  --skip-start-verify
```

### 2.8 Fix environment variables

The installer's interactive stdin prompts can silently truncate long values like API keys. Edit the env file directly:

```bash
nano ~/.agr_ai_curation/.env
```

Verify/fix these values:

```
OPENAI_API_KEY=sk-...  (must be complete, not truncated)
AUTH_PROVIDER=github
GITHUB_CLIENT_ID=<your-github-oauth-client-id>
GITHUB_CLIENT_SECRET=<your-github-oauth-client-secret>
GITHUB_JWT_SECRET=<random-hex-string>
GITHUB_REDIRECT_URI=https://curation-test.geneontology.io/api/auth/callback
GITHUB_USERS_YAML_URL=https://raw.githubusercontent.com/geneontology/go-site/master/metadata/users.yaml
```

### 2.9 Add GitHub env vars to docker-compose.production.yml

The compose file does not pass GitHub auth env vars by default. Add them to the `backend` service `environment` section:

```yaml
    environment:
      # ... existing vars ...
      GITHUB_CLIENT_ID: "${GITHUB_CLIENT_ID:-}"
      GITHUB_CLIENT_SECRET: "${GITHUB_CLIENT_SECRET:-}"
      GITHUB_JWT_SECRET: "${GITHUB_JWT_SECRET:-}"
      GITHUB_REDIRECT_URI: "${GITHUB_REDIRECT_URI:-}"
      GITHUB_USERS_YAML_URL: "${GITHUB_USERS_YAML_URL:-}"
```

> **Warning (re-deploy):** If these vars have already been committed to `docker-compose.production.yml` upstream, do NOT re-add them after `git pull`. Running `sed` or appending duplicate keys causes YAML parse errors (duplicate key `GITHUB_CLIENT_ID` etc.). Always check whether the keys already exist in the file before adding.

### 2.10 Build backend and frontend from source

We run from our fork, not from the published upstream ECR images. The upstream images don't have our GitHub auth provider, logout fixes, or frontend connection-status fixes. **You must build locally and you must get the image tags right**, or Docker Compose will silently pull the upstream images and things will break in confusing ways (wrong auth provider, false connection warnings, etc.).

**Step 1: Read the tags that Compose expects.**

```bash
grep '_IMAGE_TAG=' ~/.agr_ai_curation/.env
```

The installer typically sets these to `latest`. Whatever they say, your `docker build -t` must use the same tag.

**Step 2: Build both images with matching tags.**

```bash
cd ~/agr_ai_curation

# Read the expected tags
BACKEND_TAG=$(grep 'BACKEND_IMAGE_TAG=' ~/.agr_ai_curation/.env | cut -d= -f2)
FRONTEND_TAG=$(grep 'FRONTEND_IMAGE_TAG=' ~/.agr_ai_curation/.env | cut -d= -f2)
BACKEND_IMAGE=$(grep 'BACKEND_IMAGE=' ~/.agr_ai_curation/.env | grep -v TAG | cut -d= -f2)
FRONTEND_IMAGE=$(grep 'FRONTEND_IMAGE=' ~/.agr_ai_curation/.env | grep -v TAG | cut -d= -f2)

echo "Building backend as ${BACKEND_IMAGE}:${BACKEND_TAG}"
echo "Building frontend as ${FRONTEND_IMAGE}:${FRONTEND_TAG}"

docker build -t "${BACKEND_IMAGE}:${BACKEND_TAG}" ./backend
docker build --build-arg VITE_DEV_MODE=false -t "${FRONTEND_IMAGE}:${FRONTEND_TAG}" ./frontend
```

**Step 3: Verify the local images exist and Compose won't pull from ECR.**

```bash
docker images | grep -E 'backend|frontend' | grep -v TAG
```

You should see your locally built images with timestamps from just now.

> **Why this matters:** Docker Compose resolves images by `name:tag`. If no local image matches, it pulls from the registry. The upstream ECR images lack our fork's changes. The failure mode is silent — containers start fine but run the wrong code. We've been burned by this on both backend (auth provider rejected `github`) and frontend (false Weaviate/curation DB warnings).
>
> **Clean rebuilds:** If source changes aren't being picked up even with the right tag, prune the buildx cache first:
>
> ```bash
> docker builder prune -af
> ```

> **Note:** The alliance package has its own virtualenv with its own dependencies, defined in `packages/alliance/requirements/runtime.txt` (not `backend/requirements.txt`). This includes `noctua-py` and other tool-specific libraries. These are installed automatically by the package runner when the backend starts, but see the venv cache note below.

### 2.10a Register new agents in package.yaml

Any agent that is not listed in `packages/alliance/package.yaml` under `agent_bundles` will be **deactivated** by `system_agent_sync` on startup. If you have added a new agent (e.g. `noctua`), confirm it appears in the list:

```bash
grep -A2 'name: noctua' ~/agr_ai_curation/packages/alliance/package.yaml
```

If missing, add it to the `agent_bundles` list in `packages/alliance/package.yaml` and copy the updated package to the runtime directory:

```bash
cp -r ~/agr_ai_curation/packages/alliance ~/.agr_ai_curation/runtime/packages/
```

### 2.10b Clear package runner venv cache (if dependencies changed)

The package runner creates an isolated virtualenv at `~/.agr_ai_curation/runtime/state/package_runner/`. If you have changed `packages/alliance/requirements/runtime.txt` (added/removed dependencies like `noctua-py`), you must clear this cache so it gets rebuilt on next startup:

```bash
sudo rm -rf ~/.agr_ai_curation/runtime/state/package_runner/
```

`sudo` is required because the venv is owned by the Docker container user, not the host `ubuntu` user.

### 2.11 Bootstrap the database

The DB bootstrap does not run automatically on first start (the entrypoint check may not trigger). Run it manually:

```bash
cd ~/agr_ai_curation

# Start just postgres first
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml up -d postgres
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml exec postgres pg_isready -U postgres

# Start backend with deps
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml up -d backend

# Run bootstrap manually
docker exec backend python -c \
  "from src.lib.runtime_entrypoint import maybe_bootstrap_database, maybe_run_database_migrations; maybe_bootstrap_database(); maybe_run_database_migrations()"
```

### 2.12 Start the full stack

```bash
cd ~/agr_ai_curation
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml up -d
```

Verify all containers are healthy:

```bash
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml ps
```

### 2.13 Configure nginx

Generate a self-signed certificate (Cloudflare terminates the public TLS; this is for Cloudflare-to-origin encryption in Full mode):

```bash
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/ssl/private/selfsigned.key \
  -out /etc/ssl/certs/selfsigned.crt \
  -subj "/CN=curation-test.geneontology.io"
```

Write the nginx config:

```bash
sudo tee /etc/nginx/sites-available/agr-ai-curation <<'NGINX'
server {
    listen 443 ssl;
    server_name curation-test.geneontology.io;

    ssl_certificate     /etc/ssl/certs/selfsigned.crt;
    ssl_certificate_key /etc/ssl/private/selfsigned.key;

    location / {
        proxy_pass http://127.0.0.1:3002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
        proxy_buffering off;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/agr-ai-curation /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx
```

Key points about this nginx config:
- **Listen 443 only** (no port 80 listener). Cloudflare handles HTTP-to-HTTPS redirect.
- **Self-signed cert** is fine because Cloudflare SSL mode is Full (not Strict).
- **proxy_read_timeout 600s** because LLM agent calls can take several minutes.
- **proxy_buffering off** so streaming responses are not buffered.

### 2.14 Deploy PDFX on a separate instance

The PDF extraction service runs on its own EC2 instance (t3.xlarge minimum — t3.large OOMs loading marker models). The main app reaches it via private IP within the same VPC.

#### 2.14a Launch the PDFX instance

```bash
PDFX_ID=$(aws ec2 run-instances --region us-east-1 \
  --image-id ami-0462ececcfe0a450f --instance-type t3.xlarge \
  --key-name go-ssh --security-group-ids "$SG_ID" \
  --subnet-id subnet-0a09e8ea837f8606b --associate-public-ip-address \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=agr-ai-curation-pdfx}]' \
  --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$PDFX_ID" --region us-east-1

PDFX_PRIVATE_IP=$(aws ec2 describe-instances --instance-ids "$PDFX_ID" --region us-east-1 \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)
PDFX_PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$PDFX_ID" --region us-east-1 \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "PDFX instance: $PDFX_ID at private=$PDFX_PRIVATE_IP public=$PDFX_PUBLIC_IP"
```

#### 2.14b Add SG rule for internal PDFX access

The main app needs to reach PDFX on port 5000 via private IP. Add a self-referencing security group rule (only needed once):

```bash
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --region us-east-1 \
  --ip-permissions "IpProtocol=tcp,FromPort=5000,ToPort=5000,UserIdGroupPairs=[{GroupId=$SG_ID,Description=PDFX-internal}]"
```

#### 2.14c Bootstrap the PDFX instance

```bash
ssh -i /path/to/key ubuntu@$PDFX_PUBLIC_IP

# On the PDFX instance:
sudo apt-get update && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
sudo apt-get install -y docker-compose-plugin git
# Log out and back in (or use `sg docker`) for docker group to take effect
```

#### 2.14d Clone and configure PDFX

```bash
cd /opt
sudo git clone https://github.com/alliance-genome/agr_pdf_extraction_service.git
sudo chown -R ubuntu:ubuntu agr_pdf_extraction_service
cd agr_pdf_extraction_service
mkdir -p data/{cache,uploads,models,model_cache} logs

cat > .env <<'EOF'
OPENAI_API_KEY=<your-openai-key>
DOCLING_DEVICE=cpu
MARKER_DEVICE=cpu
CONSENSUS_ENABLED=true
PDFX_SELECTED_METHODS=grobid,docling,marker
PDFX_DEFAULT_MERGE=true
PDFX_GPU_ENABLED=false
PDFX_WORKER_CPUS=7.0
PDFX_WORKER_MEM_LIMIT=24g
EOF
chmod 600 .env
```

#### 2.14e Apply GROBID cgroup v2 fix

On Ubuntu 24.04 (cgroup v2), GROBID's JVM crashes without this fix. Add to the grobid service in `deploy/docker-compose.yml`:

```yaml
    environment:
      JAVA_TOOL_OPTIONS: "-XX:-UseContainerSupport"
```

Or via sed:

```bash
sed -i '/pdfx-grobid/,/healthcheck/{/init: true/a\    environment:\n      JAVA_TOOL_OPTIONS: "-XX:-UseContainerSupport"
}' deploy/docker-compose.yml
```

#### 2.14f Increase Celery time limits for CPU marker

Marker on CPU takes 15-30+ minutes per paper. The default Celery soft time limit (1800s) will kill the worker mid-extraction. Edit `celery_app.py` to increase both limits:

```bash
cd /opt/agr_pdf_extraction_service
sed -i 's/task_soft_time_limit=1800/task_soft_time_limit=7200/' celery_app.py
sed -i 's/task_time_limit=2100/task_time_limit=7500/' celery_app.py
```

Verify:

```bash
grep time_limit celery_app.py
```

> **This edit must be done before the first `docker compose up`**, or the images must be rebuilt with `--build` after editing.

#### 2.14g Start the PDFX stack

```bash
docker compose -f deploy/docker-compose.yml up -d
```

#### 2.14g Run PDFX database migrations

The PDFX service has its own Postgres database. The schema must be initialized before extraction tracking works:

```bash
docker exec pdfx-app alembic upgrade head
```

> **If you skip this step**, PDF uploads will appear to submit but the service can't track extraction progress. The app logs will show `relation "extraction_run" does not exist`.

#### 2.14h Verify PDFX health

```bash
curl http://localhost:5000/api/v1/health
```

Should return `{"status": "ok", "checks": {"grobid": "ok", "redis": "ok", "service": "ok", "workers": 1}}`.

#### 2.14i Point the main app at PDFX

On the **main app instance**, update `.env` and **recreate** (not restart) the backend:

```bash
# Set the PDFX URL, methods (must include all 3 for merge), and timeout
cat >> ~/.agr_ai_curation/.env <<EOF
PDF_EXTRACTION_SERVICE_URL=http://${PDFX_PRIVATE_IP}:5000
PDF_EXTRACTION_METHODS=grobid,docling,marker
PDF_EXTRACTION_TIMEOUT=7200
EOF

# Recreate the backend container (restart does NOT pick up .env changes)
cd ~/agr_ai_curation
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml stop backend
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml rm -f backend
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml up -d backend
```

> **Critical: `docker compose restart` does NOT apply .env changes.** You must stop, remove, and recreate the container. This applies any time you change `.env` values.

Verify the backend sees the PDFX service:

```bash
docker exec agr_ai_curation-backend-1 python -c "import os; print(os.environ.get('PDF_EXTRACTION_SERVICE_URL'))"
```

#### 2.14j First PDF upload is slow

The first PDF upload triggers marker model downloads (~1.3 GB). This takes 1-3 minutes and consumes significant memory. Subsequent uploads reuse cached models and are faster. The full extraction pipeline (GROBID + marker + consensus merge) takes ~5-15 minutes per paper on CPU.

### 2.15 Verify

```bash
curl -k https://localhost/
curl https://curation-test.geneontology.io/
```

---

## 3. Teardown

Run on the EC2 instance first:

```bash
cd ~/agr_ai_curation
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml down -v

# If the PDFX stack was deployed, tear it down too
cd ~/agr_pdf_extraction_service 2>/dev/null && docker compose down -v || true
```

Then from your local machine (substitute your actual resource IDs):

```bash
export AWS_SHARED_CREDENTIALS_FILE=/home/sjcarbon/local/share/secrets/go/credentials/go-aws-credentials

# 4. Release Elastic IP
aws ec2 disassociate-address --association-id eipassoc-0d389a15f3a27f4c1 --region us-east-1
aws ec2 release-address --allocation-id eipalloc-0463d1408c59aadbd --region us-east-1

# 3. Terminate instance
aws ec2 terminate-instances --instance-ids i-07d4432be2e8afea8 --region us-east-1
aws ec2 wait instance-terminated --instance-ids i-07d4432be2e8afea8 --region us-east-1

# 2. Delete security group (after instance is terminated)
aws ec2 delete-security-group --group-id sg-063c1ef568e5f5d79 --region us-east-1

# 1. Delete key pair
aws ec2 delete-key-pair --key-name go-ssh --region us-east-1
```

Also clean up:
- **Cloudflare**: Remove the DNS record and site (if partial setup).
- **Route 53**: Remove the CNAME record and any DCV delegation CNAMEs.
- **GitHub**: Delete or deactivate the OAuth App.

---

## 4. Updating users.yaml Authorization

The GitHub auth provider fetches and caches `users.yaml` from the URL specified in `GITHUB_USERS_YAML_URL` with a **1-hour TTL**.

After updating `users.yaml` in the `go-site` repo:

- **Option A**: Wait up to 1 hour for the cache to expire and refresh automatically.
- **Option B**: Restart the backend container to force an immediate refresh:

```bash
cd ~/agr_ai_curation
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml restart backend
```

---

## 5. Common Issues

### Published frontend image is empty

The ECR image `public.ecr.aws/v4p5b7m9/agr-ai-curation-frontend` tagged `smoke-20260310-final` (and possibly other tags) contains an empty Alpine scratch filesystem with no application code. You **must build from source** (see step 2.10).

### Frontend VITE_DEV_MODE must be false at build time

Vite bakes environment variables into the bundle at build time. If you forget `--build-arg VITE_DEV_MODE=false`, the frontend will run in dev mode (which may bypass auth or show debug UI). This cannot be fixed at runtime.

### docker-compose.production.yml missing GitHub env vars

Older versions of the compose file did not include `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_JWT_SECRET`, `GITHUB_REDIRECT_URI`, or `GITHUB_USERS_YAML_URL` in the backend service environment. These have since been committed upstream. If you are on an older checkout, add them manually per step 2.9. On a fresh clone from `main`, they should already be present -- do not add duplicates.

### DB bootstrap does not run on first start

Even though `RUN_DB_BOOTSTRAP_ON_START` defaults to `true`, the automatic bootstrap may not execute on the very first start (race condition or entrypoint timing). Run it manually per step 2.11.

### Cloudflare DCV Delegation delay

When using Cloudflare partial (CNAME) DNS setup, edge certificate provisioning requires a DCV Delegation CNAME in Route 53. After adding it, certificate validation can take **a few minutes**. During this time, HTTPS requests to the domain will fail with Cloudflare error pages.

### Cloudflare SSL mode must be Full

- **Flexible**: Cloudflare connects to origin over HTTP. This fails because nginx only listens on 443.
- **Full (Strict)**: Cloudflare validates the origin cert. This fails because the origin cert is self-signed.
- **Full**: Cloudflare connects over HTTPS but does not validate the origin cert. This is the correct setting.

### Empty GITHUB_USERS_YAML_URL overrides the default

If `GITHUB_USERS_YAML_URL` is set to an empty string in the env file (e.g. `GITHUB_USERS_YAML_URL=`), it overrides any default value in the code. The auth provider will fail to fetch the users list. Either set it explicitly to the correct URL or remove the line entirely from `.env` so the code default is used.

### Installer stdin truncates long values

The installer script reads API keys interactively from stdin. Long values (like OpenAI API keys) can get silently truncated. Always verify the values in `~/.agr_ai_curation/.env` after running the installer.

### Package runner venv isolation

Tool dependencies (e.g. `noctua-py`, `requests`) used by alliance package tools must go in `packages/alliance/requirements/runtime.txt`, **not** `backend/requirements.txt`. The package runner executes tools in an isolated virtualenv built from the package's own requirements file. If a tool import fails at runtime with `ModuleNotFoundError`, check the package requirements file first.

### New agents deactivated by system_agent_sync

If you add a new agent bundle (e.g. `noctua`) but forget to list it in `packages/alliance/package.yaml` under `agent_bundles`, the `system_agent_sync` process will mark it as `inactive` on startup. The agent will not appear in the UI. Add the entry and restart the backend.

### Package runner venv owned by Docker user

The cached package virtualenv at `~/.agr_ai_curation/runtime/state/package_runner/` is created by the container process, so it is owned by the Docker container's UID. To clear it from the host, you need `sudo`:

```bash
sudo rm -rf ~/.agr_ai_curation/runtime/state/package_runner/
```

After clearing, restart the backend so it rebuilds the venv.

### Local build ignored — Compose pulls upstream image

If you build a Docker image locally but Compose keeps using the old upstream image, check two things:

1. **Tag mismatch**: The `.env` file sets `BACKEND_IMAGE_TAG` (e.g. `latest`). Your `docker build -t` must use the same tag. If you build with a different tag, Compose won't find your local image and will pull from ECR.

2. **Stale buildx cache**: Even `--no-cache` may not help because buildx has its own layer cache. Run `docker builder prune -af` before rebuilding.

To verify which image a container is actually using:

```bash
docker inspect <container-name> --format "{{.Image}}"
docker run --rm <image>:<tag> grep "something" /app/backend/src/config.py
```

If the `docker run` test shows the right code but the container doesn't, the container was created from an older image with the same tag. Stop, remove the container, and `docker compose up -d` again.

### GROBID cgroup v2 crash

On hosts running cgroup v2 (most modern Ubuntu/Debian), GROBID's JVM crashes because the JVM incorrectly reads container memory limits. Fix by setting this environment variable on the GROBID container:

```
JAVA_TOOL_OPTIONS=-XX:-UseContainerSupport
```

In a PDFX compose file, add it to the GROBID service's `environment` section. Without this, GROBID will segfault or OOM-kill shortly after starting.

### Duplicate keys in docker-compose.production.yml after git pull

If env vars (like `GITHUB_CLIENT_ID`) were previously added manually via `sed` and have since been committed upstream, running `git pull` followed by the same `sed` command again will produce duplicate YAML keys. Docker Compose will fail to parse the file. Always check whether the keys already exist before adding them.

### `docker compose restart` does NOT pick up .env changes

If you change a value in `.env` and run `docker compose restart backend`, the container keeps its old environment. You must stop, remove, and recreate:

```bash
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml stop backend
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml rm -f backend
docker compose --env-file ~/.agr_ai_curation/.env -f docker-compose.production.yml up -d backend
```

This bit us when changing `PDF_EXTRACTION_SERVICE_URL` to a new PDFX instance IP — the backend kept connecting to the old IP.

### PDFX "extraction_run" table does not exist

If PDF uploads appear to submit but the PDFX service can't track progress, the PDFX Postgres database hasn't been migrated. Run:

```bash
docker exec pdfx-app alembic upgrade head
```

This creates the `extraction_run` table. Without it, the app logs show `relation "extraction_run" does not exist` and extraction status polling fails.

### PDFX worker OOM on t3.large and t3.xlarge

The marker PDF extractor downloads ~1.3 GB of model files and loads them into memory, then peaks at ~10 GB RSS during inference.

- **t3.large (8 GB)**: OOM during model download.
- **t3.xlarge (16 GB)**: OOM during marker inference (models load but processing exceeds available RAM with GROBID + other containers).
- **t3.2xlarge (32 GB)**: Works. This is the minimum for GROBID + Docling + marker on CPU.

### PDFX timeouts on CPU: backend and Celery

Marker on CPU takes 15-30+ minutes per paper. Two timeout layers must both be large enough:

1. **Backend `PDF_EXTRACTION_TIMEOUT`** (default 300s): How long the backend polls the PDFX API. Set to at least `7200` (2 hours) in `.env`.
2. **Celery `task_soft_time_limit`** (default 1800s in `celery_app.py`): How long the Celery worker allows a task to run before killing it. Must be edited in the source and the image rebuilt. Set to at least `7200`.

If the backend times out, you see `PDF extraction timed out before completion`. If Celery times out, you see `SoftTimeLimitExceeded` in the worker logs and the backend eventually sees a failed extraction.

### Consensus merge requires all 3 extractors

If `PDF_EXTRACTION_MERGE=true` and `CONSENSUS_ENABLED=true`, the PDFX consensus pipeline requires all 3 extractors (GROBID, Docling, and Marker). If you only send 2 methods, the merge fails immediately with:

```
Merge requires consensus pipeline with all 3 extractors (CONSENSUS_ENABLED=true, grobid, docling, marker)
```

Set `PDF_EXTRACTION_METHODS=grobid,docling,marker` in the **backend** `.env` (not the PDFX `.env` — the backend sends the methods in each request). Also set `PDFX_SELECTED_METHODS=grobid,docling,marker` in the PDFX `.env` for consistency.

### SSH known_hosts mismatch when reusing an Elastic IP

When you terminate an instance and launch a new one with the same EIP, the new instance has a different host key. SSH will refuse to connect with `REMOTE HOST IDENTIFICATION HAS CHANGED`. Fix:

```bash
ssh-keygen -f ~/.ssh/known_hosts -R <elastic-ip>
```

This is expected and safe when you know you replaced the instance.

---

## 6. Noctua Integration

The noctua agent requires Barista authentication to interact with the Noctua/Minerva API. Add these variables to `~/.agr_ai_curation/.env`:

```
BARISTA_TOKEN_EXCHANGE_URL=http://barista-dev.berkeleybop.org/auth/token/exchange
BARISTA_BASE_URL=http://barista-dev.berkeleybop.org
BARISTA_NAMESPACE=minerva_public_dev
```

These are already wired through `docker-compose.production.yml` to the backend container. If they are not present in the compose file, add them to the `backend` service `environment` section:

```yaml
      BARISTA_TOKEN_EXCHANGE_URL: "${BARISTA_TOKEN_EXCHANGE_URL:-}"
      BARISTA_BASE_URL: "${BARISTA_BASE_URL:-http://barista-dev.berkeleybop.org}"
      BARISTA_NAMESPACE: "${BARISTA_NAMESPACE:-minerva_public_dev}"
```

The noctua agent bundle must also be registered in `packages/alliance/package.yaml` (see step 2.10a) and `noctua-py` must be listed in `packages/alliance/requirements/runtime.txt`.
