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

The published frontend image on ECR is broken (empty Alpine scratch image). The backend needs the GitHub auth code that may not be in the published image. Build both locally:

```bash
cd ~/agr_ai_curation

docker build -t public.ecr.aws/v4p5b7m9/agr-ai-curation-backend:smoke-20260310-final ./backend

docker build --build-arg VITE_DEV_MODE=false \
  -t public.ecr.aws/v4p5b7m9/agr-ai-curation-frontend:smoke-20260310-final ./frontend
```

The tags must match what `docker-compose.production.yml` expects (check `FRONTEND_IMAGE_TAG` and `BACKEND_IMAGE_TAG` defaults or set them in `.env`).

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

### 2.14 Verify

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

### GROBID cgroup v2 crash

On hosts running cgroup v2 (most modern Ubuntu/Debian), GROBID's JVM crashes because the JVM incorrectly reads container memory limits. Fix by setting this environment variable on the GROBID container:

```
JAVA_TOOL_OPTIONS=-XX:-UseContainerSupport
```

In a PDFX compose file, add it to the GROBID service's `environment` section. Without this, GROBID will segfault or OOM-kill shortly after starting.

### Duplicate keys in docker-compose.production.yml after git pull

If env vars (like `GITHUB_CLIENT_ID`) were previously added manually via `sed` and have since been committed upstream, running `git pull` followed by the same `sed` command again will produce duplicate YAML keys. Docker Compose will fail to parse the file. Always check whether the keys already exist before adding them.

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
