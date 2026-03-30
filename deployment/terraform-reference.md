# Terraform Reference: AGR AI Curation Infrastructure

This document records every AWS resource and configuration decision made
during deployment, structured for future conversion to Terraform HCL.

---

## Variables

```hcl
variable "aws_region" {
  default = "us-east-1"
}

variable "vpc_id" {
  description = "Existing GO production VPC"
  default     = "vpc-0926e26ef0721bf97"
}

variable "subnet_id" {
  description = "Existing subnet in go-production VPC, us-east-1a"
  default     = "subnet-0a09e8ea837f8606b"
}

variable "ssh_key_name" {
  default = "go-ssh"
}

variable "domain" {
  default = "curation-test.geneontology.io"
}

variable "main_instance_type" {
  default = "t3.xlarge"  # 4 vCPU, 16 GB RAM
}

variable "pdfx_instance_type" {
  # Production: g5.2xlarge (8 vCPU, 32 GB RAM, 1x NVIDIA A10G 24GB VRAM, ~$905/mo)
  # Dev/experiment: t3.2xlarge (8 vCPU, 32 GB, CPU-only, ~$240/mo)
  #   - GROBID + marker on CPU, single PDF at a time, speed not critical
  #   - t3.large (8 GB) OOMs during marker model download
  #   - t3.xlarge (16 GB) OOMs during marker inference (~10 GB RSS)
  #   - t3.2xlarge (32 GB) is the minimum for GROBID + marker on CPU
  #   - Upgrade to g4dn.xlarge (~$380/mo) for GPU acceleration if needed
  default = "t3.2xlarge"
}

variable "main_ami" {
  description = "Ubuntu 24.04 LTS"
  default     = "ami-0462ececcfe0a450f"
}

variable "pdfx_ami" {
  description = "Ubuntu 24.04 LTS (same as main app — no GPU drivers needed for CPU-only)"
  default     = "ami-0462ececcfe0a450f"
}
```

## Resource: Key Pair

Already exists, imported from local SSH key.

```hcl
resource "aws_key_pair" "go_ssh" {
  key_name   = "go-ssh"
  public_key = file("/home/sjcarbon/local/share/secrets/go/ssh-keys/go-ssh.pub")
}
```

- **KeyPairId**: `key-0f7e28ce80286d039`

## Resource: Security Group

Single security group shared by both instances. Inbound rules:

```hcl
resource "aws_security_group" "curation" {
  name        = "agr-ai-curation-test"
  description = "AGR AI Curation test deployment"
  vpc_id      = var.vpc_id

  # SSH from LBL network
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["128.3.112.0/21"]
    description = "LBL-SSH"
  }

  # HTTP (Cloudflare redirect)
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTP"
  }

  # HTTPS (Cloudflare proxy)
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
```

- **GroupId**: `sg-063c1ef568e5f5d79`
- **Note**: PDFX instance only needs port 5000 from the main app (private IP),
  which is allowed implicitly since both are in the same VPC/SG. No public
  exposure of port 5000.

## Resource: EC2 Instance — Main App

```hcl
resource "aws_instance" "main" {
  ami                         = var.main_ami
  instance_type               = var.main_instance_type
  key_name                    = aws_key_pair.go_ssh.key_name
  vpc_security_group_ids      = [aws_security_group.curation.id]
  subnet_id                   = var.subnet_id
  associate_public_ip_address = true

  root_block_device {
    volume_size           = 100  # GB
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name = "agr-ai-curation-test"
  }
}
```

**Bootstrap (user_data or manual SSH)**:
```bash
sudo apt-get update && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
sudo apt-get install -y docker-compose-plugin nginx git
```

## Resource: EC2 Instance — PDFX (GPU)

```hcl
resource "aws_instance" "pdfx" {
  ami                         = var.pdfx_ami
  instance_type               = var.pdfx_instance_type
  key_name                    = aws_key_pair.go_ssh.key_name
  vpc_security_group_ids      = [aws_security_group.curation.id]
  subnet_id                   = var.subnet_id
  associate_public_ip_address = true  # for SSH access; PDFX not publicly exposed

  root_block_device {
    volume_size           = 50   # GB — CPU-only, no large GPU models
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name = "agr-ai-curation-pdfx"
  }
}
```

**Bootstrap (user_data or manual SSH)**:
```bash
sudo apt-get update && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
sudo apt-get install -y docker-compose-plugin git
```

## Resource: Elastic IP

Single EIP for the main app instance (DNS target).

```hcl
resource "aws_eip" "main" {
  domain = "vpc"

  tags = {
    Name = "agr-ai-curation-test"
  }
}

resource "aws_eip_association" "main" {
  instance_id   = aws_instance.main.id
  allocation_id = aws_eip.main.id
}
```

- **AllocationId**: `eipalloc-0463d1408c59aadbd`
- **PublicIp**: `32.193.66.135`
- PDFX instance does not need an EIP (accessed via private IP from main app)

## DNS (not in AWS Terraform — Cloudflare + Route 53)

- **Route 53**: CNAME `curation-test.geneontology.io` → Cloudflare
- **Cloudflare**: Partial (CNAME) DNS setup, proxied A record → EIP
- **Cloudflare SSL**: Full (not Strict) — origin uses self-signed cert
- **DCV Delegation**: `_acme-challenge.curation-test.geneontology.io` CNAME in Route 53

## Application Configuration

### Main app `.env` (key variables)

```
AUTH_PROVIDER=github
GITHUB_CLIENT_ID=<from-github-oauth-app>
GITHUB_CLIENT_SECRET=<from-github-oauth-app>
GITHUB_JWT_SECRET=<openssl-rand-hex-32>
GITHUB_REDIRECT_URI=https://curation-test.geneontology.io/api/auth/callback
GITHUB_USERS_YAML_URL=https://raw.githubusercontent.com/geneontology/go-site/master/metadata/users.yaml

ANTHROPIC_API_KEY=<new-key>
OPENAI_API_KEY=<if-needed>

PDF_EXTRACTION_SERVICE_URL=http://<pdfx-private-ip>:5000
PDF_EXTRACTION_METHODS=grobid,marker
PDF_EXTRACTION_MERGE=true

BARISTA_TOKEN_EXCHANGE_URL=http://barista-dev.berkeleybop.org/auth/token/exchange
BARISTA_BASE_URL=http://barista-dev.berkeleybop.org
BARISTA_NAMESPACE=minerva_public_dev
```

### PDFX `.env`

```
OPENAI_API_KEY=<same-key-for-llm-merge>
DOCLING_DEVICE=cuda
MARKER_DEVICE=auto
CONSENSUS_ENABLED=true
PDFX_SELECTED_METHODS=grobid,marker
PDFX_DEFAULT_MERGE=true
PDFX_GPU_ENABLED=true
```

### PDFX GROBID cgroup v2 fix

Add to grobid service environment in compose:
```yaml
environment:
  JAVA_TOOL_OPTIONS: "-XX:-UseContainerSupport"
```

## Network topology

```
Internet → Cloudflare (HTTPS) → Main App EIP:443 → nginx (self-signed TLS)
                                                      → localhost:3002 (frontend+backend)

Main App (private IP) → PDFX Instance (private IP):5000
                          → pdfx-app (Flask/Gunicorn)
                          → pdfx-worker (Celery + GPU)
                          → pdfx-grobid (GROBID CRF)
                          → pdfx-redis
                          → pdfx-postgres
```

## Cost estimate

| Resource | Type | Approx. monthly cost |
|----------|------|---------------------|
| Main app | t3.xlarge on-demand | ~$120 |
| PDFX | t3.2xlarge on-demand (CPU) | ~$240 |
| EIP | (while associated) | $0 |
| Storage | 150 GB gp3 total | ~$12 |
| **Total** | | **~$372/mo** |

Note: Marker on CPU needs ~10 GB RSS during inference. t3.large (8 GB)
and t3.xlarge (16 GB) both OOM. t3.2xlarge (32 GB) is the minimum for
GROBID + marker on CPU. For GPU acceleration, use g4dn.xlarge (~$380/mo)
which is nearly the same cost but much faster.
