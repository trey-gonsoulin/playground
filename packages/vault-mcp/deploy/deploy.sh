#!/bin/bash
# Operate the obsidian-vault box from the Mac. Needs the AWS CLI (profile
# "personal") and, for shell/vnc, the Session Manager plugin
# (brew install --cask session-manager-plugin). See packages/vault-mcp/README.md.
#
#   deploy.sh infra                 create a change set for the stack (review, then execute it)
#   deploy.sh token                 create the bearer token if missing, and print it
#   deploy.sh tunnel-token TOKEN    store the Cloudflare tunnel token (from the Zero Trust dashboard)
#   deploy.sh public-url URL        store https://<hostname> so the health check also tests the tunnel
#   deploy.sh readonly 0|1          VAULT_MCP_READONLY on the next push
#   deploy.sh push                  build the wheels, upload, run install.sh on the box
#   deploy.sh shell                 SSM shell on the box
#   deploy.sh vnc                   VNC to the headless app (Sync login): vnc://localhost:5900
#   deploy.sh logs                  recent logs of every service
set -euo pipefail

STACK=${STACK:-obsidian-vault}
REGION=${REGION:-us-east-1}
PROFILE=${PROFILE:-personal}
ARCH=${ARCH:-arm64} # arm64 for t4g, amd64 for t3
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }
output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
put_secret() { aws ssm put-parameter --name "/obsidian-vault/$1" --type SecureString --overwrite --value "$2" >/dev/null; }

# Run shell commands on the box as root over SSM and print the output.
run_remote() {
  local id cmd_id status
  id=$(output InstanceId)
  cmd_id=$(aws ssm send-command --instance-ids "$id" --document-name AWS-RunShellScript \
    --timeout-seconds 1800 --parameters "$(jq -n --arg c "$1" '{commands: [$c], executionTimeout: ["1800"]}')" \
    --query Command.CommandId --output text)
  echo "SSM command $cmd_id on $id ..." >&2
  while :; do
    sleep 5
    status=$(aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$id" \
      --query Status --output text 2>/dev/null || echo Pending)
    case $status in Pending | InProgress | Delayed) continue ;; esac
    break
  done
  aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$id" \
    --query '[StandardOutputContent, StandardErrorContent]' --output text
  [ "$status" = Success ] || { echo "SSM command finished: $status" >&2; exit 1; }
}

case "${1:-}" in
  infra)
    params=()
    if ! aws cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1; then
      : "${ALERT_EMAIL:?set ALERT_EMAIL for the first deploy}"
      ami=$(aws ssm get-parameter --query Parameter.Value --output text \
        --name "/aws/service/canonical/ubuntu/server/24.04/stable/current/$ARCH/hvm/ebs-gp3/ami-id")
      echo "Pinning Ubuntu 24.04 $ARCH AMI $ami"
      params=(--parameter-overrides "ImageId=$ami" "AlertEmail=$ALERT_EMAIL")
      [ "$ARCH" = amd64 ] && params+=("InstanceType=t3.small")
    fi
    # `deploy` keeps previous parameter values (the pinned AMI) when not overridden.
    aws cloudformation deploy --stack-name "$STACK" --template-file "$HERE/template.yaml" \
      --capabilities CAPABILITY_IAM --no-execute-changeset ${params[@]+"${params[@]}"}
    echo "Review the change set above, then:"
    echo "  aws cloudformation execute-change-set --change-set-name <arn> --profile $PROFILE --region $REGION"
    echo "  aws cloudformation wait stack-create-complete --stack-name $STACK --profile $PROFILE --region $REGION   # or stack-update-complete"
    ;;
  token)
    if ! tok=$(aws ssm get-parameter --name /obsidian-vault/mcp-token --with-decryption \
      --query Parameter.Value --output text 2>/dev/null); then
      tok=$(openssl rand -hex 32)
      put_secret mcp-token "$tok"
      echo "Created a new token (it reaches the box on the next push)." >&2
    fi
    echo "Authorization: Bearer $tok"
    ;;
  tunnel-token) put_secret tunnel-token "${2:?token}" && echo "Stored; run: deploy.sh push" ;;
  public-url) aws ssm put-parameter --name /obsidian-vault/public-url --type String --overwrite --value "${2:?url}" >/dev/null && echo "Stored; run: deploy.sh push" ;;
  readonly) aws ssm put-parameter --name /obsidian-vault/readonly --type String --overwrite --value "${2:?0 or 1}" >/dev/null && echo "Stored; run: deploy.sh push" ;;
  push)
    bucket=$(output BackupBucket)
    rm -rf "$HERE/../dist"
    (cd "$REPO" && uv build -q --wheel --package vault-mcp -o packages/vault-mcp/dist &&
      uv build -q --wheel --package mcp-lambda -o packages/vault-mcp/dist)
    aws s3 sync --delete --only-show-errors "$HERE/../dist" "s3://$bucket/deploy/dist"
    aws s3 sync --delete --only-show-errors "$HERE/server" "s3://$bucket/deploy/server"
    run_remote "set -e; . /etc/vault-mcp/stack.env
rm -rf /opt/vault-mcp/src && mkdir -p /opt/vault-mcp/src
/snap/bin/aws s3 sync --only-show-errors --region \$AWS_REGION s3://\$VAULT_BUCKET/deploy/ /opt/vault-mcp/src/
chmod -R a+rX /opt/vault-mcp/src
OBSIDIAN_VERSION=${OBSIDIAN_VERSION:-1.15.0} bash /opt/vault-mcp/src/server/install.sh 2>&1"
    ;;
  shell) aws ssm start-session --target "$(output InstanceId)" ;;
  vnc)
    run_remote "sudo -iu obsidian vault-vnc start"
    echo "Forwarding localhost:5900; open vnc://localhost:5900 in Finder (Cmd+K). Ctrl+C when done, then:"
    echo "  deploy.sh shell  ->  sudo -iu obsidian vault-vnc stop"
    aws ssm start-session --target "$(output InstanceId)" --document-name AWS-StartPortForwardingSession \
      --parameters portNumber=5900,localPortNumber=5900
    ;;
  logs)
    run_remote "u=\$(id -u obsidian)
sudo -u obsidian XDG_RUNTIME_DIR=/run/user/\$u journalctl --user -n 40 --no-pager -u obsidian -u vault-mcp -u vault-backup -u vault-healthcheck
journalctl -n 20 --no-pager -u cloudflared
tail -n 20 /home/obsidian/.local/state/vault-mcp/audit.log 2>/dev/null"
    ;;
  *) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
