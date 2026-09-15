#!/usr/bin/env bash
# Create a demo droplet for this repository with doctl and print its IP.
#
#   deploy/digitalocean/create-droplet.sh [options]
#
# Options:
#   --name NAME        droplet name (default: sentry-demo)
#   --region SLUG      region (default: fra1)
#   --size SLUG        size slug; default picks the cheapest size that meets the
#                      errors-only minimum (2 vCPU, 8 GB RAM, 50 GB disk)
#   --full             pick for the feature-complete minimum instead (4 vCPU, 16 GB)
#   --ssh-key PATH     public key to authorize (default: ~/.ssh/id_ed25519.pub)
#   --yes              do not ask for confirmation before creating
#
# Requires an authenticated doctl (`doctl auth init`). Idempotent: an existing
# droplet with the same name is reused.
set -euo pipefail

DOCTL="${DOCTL:-$(command -v doctl || echo "$HOME/.local/bin/doctl")}"
NAME=sentry-demo REGION=fra1 SIZE="" MIN_VCPU=2 MIN_MEM=8192 MIN_DISK=50 PUBKEY="$HOME/.ssh/id_ed25519.pub" YES=""
while [[ $# -gt 0 ]]; do
  case "$1" in
  --name) NAME="$2"; shift 2 ;;
  --region) REGION="$2"; shift 2 ;;
  --size) SIZE="$2"; shift 2 ;;
  --full) MIN_VCPU=4 MIN_MEM=16384; shift ;;
  --ssh-key) PUBKEY="$2"; shift 2 ;;
  --yes) YES=1; shift ;;
  -h | --help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

"$DOCTL" account get --format Email,Status --no-header >/dev/null || {
  echo "doctl is not authenticated: run '$DOCTL auth init' first" >&2
  exit 1
}

existing=$("$DOCTL" compute droplet list --format Name,PublicIPv4 --no-header | awk -v n="$NAME" '$1==n{print $2}')
if [[ -n "$existing" ]]; then
  echo "droplet $NAME already exists: $existing"
  echo "$existing"
  exit 0
fi

if [[ -z "$SIZE" ]]; then
  SIZE=$("$DOCTL" compute size list --output json | python3 -c "
import json, sys
region, vcpu, mem, disk = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
ok = [s for s in json.load(sys.stdin)
      if s.get('available') and region in s.get('regions', [])
      and s['vcpus'] >= vcpu and s['memory'] >= mem and s['disk'] >= disk]
ok.sort(key=lambda s: (s['price_monthly'], -s['vcpus']))
print(ok[0]['slug'] if ok else '')
" "$REGION" "$MIN_VCPU" "$MIN_MEM" "$MIN_DISK")
  [[ -n "$SIZE" ]] || { echo "no size in $REGION meets ${MIN_VCPU} vCPU / ${MIN_MEM} MB / ${MIN_DISK} GB" >&2; exit 1; }
fi
"$DOCTL" compute size list --format Slug,VCPUs,Memory,Disk,PriceMonthly | awk -v s="$SIZE" 'NR==1||$1==s'

[[ -f "$PUBKEY" ]] || { echo "public key not found: $PUBKEY" >&2; exit 1; }
FINGERPRINT=$(ssh-keygen -E md5 -lf "$PUBKEY" | awk '{print $2}' | sed 's/^MD5://')
if ! "$DOCTL" compute ssh-key list --format FingerPrint --no-header | grep -qx "$FINGERPRINT"; then
  echo "importing SSH key $PUBKEY ($FINGERPRINT)"
  "$DOCTL" compute ssh-key import "sentry-demo-$(hostname)-$(date +%Y%m%d)" --public-key-file "$PUBKEY" >/dev/null
fi

if [[ -z "$YES" ]]; then
  read -r -p "Create droplet $NAME ($SIZE, $REGION, ubuntu-24-04-x64)? [y/N] " answer
  [[ "$answer" == [yY]* ]] || { echo "aborted"; exit 1; }
fi

echo "creating $NAME ..."
"$DOCTL" compute droplet create "$NAME" \
  --region "$REGION" --size "$SIZE" --image ubuntu-24-04-x64 \
  --ssh-keys "$FINGERPRINT" --tag-names sentry-demo --enable-monitoring \
  --wait --format ID,Name,PublicIPv4,Status >/dev/null
IP=$("$DOCTL" compute droplet list --format Name,PublicIPv4 --no-header | awk -v n="$NAME" '$1==n{print $2}')
echo "waiting for SSH on $IP ..."
for _ in $(seq 1 60); do
  ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "root@$IP" true 2>/dev/null && break
  sleep 5
done
echo "$IP"
