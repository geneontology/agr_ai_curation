# AWS Deployment Audit Log

**Account**: 162763300381 (IAM user: kltm)
**Region**: us-east-1
**Purpose**: Deploy AGR AI Curation to curation-test.geneontology.io

## Deployment History

| Date | Action |
|------|--------|
| 2026-03-20 | Initial deployment (single instance) |
| 2026-03-27 | Redeployed: terminated old instance, launched new main app instance. PDFX to be deployed separately on GPU instance. |

## Resources Created

All resources below are currently active. To tear down,
delete in reverse order.

---

### 1. Key Pair: `go-ssh`

- **KeyPairId**: `key-0f7e28ce80286d039`
- **Created**: 2026-03-20 (reused across deployments)

```bash
aws ec2 import-key-pair --key-name go-ssh \
  --public-key-material fileb:///home/sjcarbon/local/share/secrets/go/ssh-keys/go-ssh.pub \
  --region us-east-1
```

**Undo**:
```bash
aws ec2 delete-key-pair --key-name go-ssh --region us-east-1
```

---

### 2. Security Group: `agr-ai-curation-test`

- **GroupId**: `sg-063c1ef568e5f5d79`
- **VPC**: `vpc-0926e26ef0721bf97` (go-production)
- **Created**: 2026-03-20 (reused across deployments)
- **Inbound rules**:
  - 22/TCP from 128.3.112.0/21 (LBL SSH)
  - 22/TCP from 23.93.200.213/32 (SJC-remote, sgr-034af72c24387535e, added 2026-03-21)
  - 80/TCP from 0.0.0.0/0 (HTTP)
  - 443/TCP from 0.0.0.0/0 (HTTPS)

**Undo**:
```bash
aws ec2 delete-security-group --group-id sg-063c1ef568e5f5d79 --region us-east-1
```

---

### 3. EC2 Instance: `agr-ai-curation-test` (main app)

- **InstanceId**: `i-0ea2dc21ef8da2fa4`
- **Type**: t3.xlarge (4 vCPU, 16 GB RAM)
- **AMI**: ami-0462ececcfe0a450f (Ubuntu 24.04 LTS)
- **Storage**: 100 GB gp3
- **Subnet**: subnet-0a09e8ea837f8606b (go-production, us-east-1a)
- **Private IP**: 10.0.1.207
- **Key pair**: go-ssh
- **Created**: 2026-03-27 (replaces i-07d4432be2e8afea8, terminated same day)

```bash
aws ec2 run-instances --region us-east-1 \
  --image-id ami-0462ececcfe0a450f --instance-type t3.xlarge \
  --key-name go-ssh --security-group-ids sg-063c1ef568e5f5d79 \
  --subnet-id subnet-0a09e8ea837f8606b --associate-public-ip-address \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":100,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=agr-ai-curation-test}]'
```

**Undo**:
```bash
aws ec2 terminate-instances --instance-ids i-0ea2dc21ef8da2fa4 --region us-east-1
```

---

### 4. EC2 Instance: `agr-ai-curation-pdfx` (CPU, PDF extraction)

- **InstanceId**: `i-0e82df32732b76879`
- **Type**: t3.2xlarge (8 vCPU, 32 GB RAM) — CPU-only, marker runs on CPU
- **AMI**: ami-0462ececcfe0a450f (Ubuntu 24.04 LTS)
- **Storage**: 50 GB gp3
- **Subnet**: subnet-0a09e8ea837f8606b (go-production, us-east-1a)
- **Private IP**: 10.0.1.11
- **Public IP**: 3.82.1.92 (auto-assigned, SSH only)
- **Key pair**: go-ssh
- **Created**: 2026-03-29 (replaces i-0f3ff8b455f6c7876, OOM on t3.xlarge)
- **SG rule**: sgr-0b807fce856e17523 (port 5000, self-referencing SG for internal access)

**Undo**:
```bash
aws ec2 terminate-instances --instance-ids i-0e82df32732b76879 --region us-east-1
```

---

### 5. Elastic IP

- **AllocationId**: `eipalloc-0463d1408c59aadbd`
- **PublicIp**: `32.193.66.135`
- **AssociationId**: `eipassoc-05d74fc2a7e08fe7b` (to i-0ea2dc21ef8da2fa4)
- **Created**: 2026-03-20 (reused across deployments)

**Undo**:
```bash
aws ec2 disassociate-address --association-id eipassoc-05d74fc2a7e08fe7b --region us-east-1
aws ec2 release-address --allocation-id eipalloc-0463d1408c59aadbd --region us-east-1
```

---

## Terminated Resources

| Resource | ID | Terminated |
|----------|-----|-----------|
| EC2 Instance | i-07d4432be2e8afea8 | 2026-03-27 |
| EC2 Instance (PDFX t3.large) | i-02e073013836337d2 | 2026-03-29 (OOM, upgraded to t3.xlarge) |
| EC2 Instance (PDFX t3.xlarge) | i-0f3ff8b455f6c7876 | 2026-03-29 (OOM, upgraded to t3.2xlarge) |

---

## Full Teardown (reverse order)

```bash
export AWS_SHARED_CREDENTIALS_FILE=/home/sjcarbon/local/share/secrets/go/credentials/go-aws-credentials

# 5. Release Elastic IP
aws ec2 disassociate-address --association-id eipassoc-05d74fc2a7e08fe7b --region us-east-1
aws ec2 release-address --allocation-id eipalloc-0463d1408c59aadbd --region us-east-1

# 4. Terminate PDFX instance
aws ec2 terminate-instances --instance-ids i-0e82df32732b76879 --region us-east-1
aws ec2 wait instance-terminated --instance-ids i-0e82df32732b76879 --region us-east-1

# 3. Terminate main app instance
aws ec2 terminate-instances --instance-ids i-0ea2dc21ef8da2fa4 --region us-east-1
aws ec2 wait instance-terminated --instance-ids i-0ea2dc21ef8da2fa4 --region us-east-1

# 2. Delete security group (after all instances terminated)
aws ec2 delete-security-group --group-id sg-063c1ef568e5f5d79 --region us-east-1

# 1. Delete key pair
aws ec2 delete-key-pair --key-name go-ssh --region us-east-1
```
