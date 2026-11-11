#!/bin/bash
# Operate the obsidian-vault stack from the Mac. Needs the AWS CLI (profile
# "personal"), the SAM CLI + Docker Desktop (infra), and the Session Manager
# plugin (shell/vnc: brew install --cask session-manager-plugin).
# See packages/vault-mcp/README.md.
#
#   deploy.sh infra            sam build + a change set to review (nothing is executed)
#   deploy.sh token            create the bearer token if missing, and print it
#   deploy.sh alerts EMAIL     subscribe EMAIL to the alert topic (confirm the email)
#   deploy.sh readonly 0|1     VAULT_MCP_READONLY (box: next push; Lambda: next cold start)
#   deploy.sh idle-minutes N   idle time before the box stops itself (default 30; next push)
#   deploy.sh sync-busy LIST   Sync statuses that mean "still syncing" (README step 5; next push)
#   deploy.sh start            start the box and wait until SSM can reach it
#   deploy.sh push             build the wheels, upload, run install.sh on the box
#   deploy.sh url              the connector URL
#   deploy.sh shell            SSM shell on the box
#   deploy.sh vnc              VNC to the headless app (Sync login): vnc://localhost:5900
#   deploy.sh logs             recent logs of every service
set -euo pipefail

STACK=${STACK:-obsidian-vault}
REGION=${REGION:-us-east-1}
PROFILE=${PROFILE:-personal}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
ECR_REPO=obsidian-vault/vaultproxyfunction
aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }
output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
put_param() { aws ssm put-parameter --name "/obsidian-vault/$1" --type "$2" --overwrite --value "$3" >/dev/null; }

start_box() {
  local id state
  id=$(output InstanceId)
  state=$(aws ec2 describe-instances --instance-ids "$id" --query 'Reservations[0].Instances[0].State.Name' --output text)
  if [ "$state" = stopping ]; then aws ec2 wait instance-stopped --instance-ids "$id"; state=stopped; fi
  if [ "$state" = stopped ]; then aws ec2 start-instances --instance-ids "$id" >/dev/null; fi
  aws ec2 wait instance-running --instance-ids "$id"
  echo "Waiting for SSM to reach $id ..." >&2
  for _ in $(seq 60); do
    [ "$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$id" \
      --query 'InstanceInformationList[0].PingStatus' --output text)" = Online ] && return 0
    sleep 5
  done
  echo "SSM never came online" >&2
  return 1
}

# Run shell commands on the box as root over SSM and print the output.
run_remote() {
  local id cmd_id status
  start_box
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
    # Exported, so sam build AND sam deploy use Docker Desktop's engine (see CLAUDE.md).
    export DOCKER_HOST=${DOCKER_HOST:-unix://$HOME/.docker/run/docker.sock}
    if ! aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1; then
      aws ecr create-repository --repository-name "$ECR_REPO" \
        --image-scanning-configuration scanOnPush=true >/dev/null
      aws ecr put-lifecycle-policy --repository-name "$ECR_REPO" --lifecycle-policy-text \
        '{"rules":[{"rulePriority":1,"description":"keep 5","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}' >/dev/null
      echo "Created ECR repository $ECR_REPO"
    fi
    aws ssm get-parameter --name /obsidian-vault/mcp-token >/dev/null 2>&1 ||
      echo "NOTE: run './deploy.sh token' before the Lambda's first request." >&2
    cd "$HERE"
    sam build --config-file samconfig.toml
    sam deploy --config-file samconfig.toml --no-execute-changeset
    echo "Review the change set above (expect Modify rows with Replacement False on updates), then:"
    echo "  aws cloudformation execute-change-set --change-set-name <arn> --profile $PROFILE --region $REGION"
    echo "  aws cloudformation wait stack-create-complete --stack-name $STACK --profile $PROFILE --region $REGION   # or stack-update-complete"
    ;;
  token)
    if ! tok=$(aws ssm get-parameter --name /obsidian-vault/mcp-token --with-decryption \
      --query Parameter.Value --output text 2>/dev/null); then
      tok=$(openssl rand -hex 32)
      put_param mcp-token SecureString "$tok"
      echo "Created a new token (the box gets it on the next push)." >&2
    fi
    echo "Authorization: Bearer $tok"
    ;;
  alerts) aws sns subscribe --topic-arn "$(output AlertTopicArn)" --protocol email \
    --notification-endpoint "${2:?email}" >/dev/null && echo "Check ${2} for the confirmation email." ;;
  readonly) put_param readonly String "${2:?0 or 1}" && echo "Stored; run: deploy.sh push" ;;
  idle-minutes) put_param idle-minutes String "${2:?minutes}" && echo "Stored; run: deploy.sh push" ;;
  sync-busy) put_param sync-busy String "${2:?comma-separated statuses}" && echo "Stored; run: deploy.sh push" ;;
  start) start_box && echo "Running." ;;
  push)
    bucket=$(output DeployBucket)
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
  url) echo "$(output McpUrl)" ;;
  shell) start_box && aws ssm start-session --target "$(output InstanceId)" ;;
  vnc)
    run_remote "sudo -iu obsidian vault-vnc start"
    echo "Forwarding localhost:5900; open vnc://localhost:5900 in Finder (Cmd+K). Ctrl+C when done, then:"
    echo "  deploy.sh shell  ->  sudo -iu obsidian vault-vnc stop     (until then the box won't idle-stop)"
    aws ssm start-session --target "$(output InstanceId)" --document-name AWS-StartPortForwardingSession \
      --parameters portNumber=5900,localPortNumber=5900
    ;;
  logs)
    run_remote "u=\$(id -u obsidian)
sudo -u obsidian XDG_RUNTIME_DIR=/run/user/\$u journalctl --user -n 40 --no-pager -u obsidian -u vault-mcp -u vault-healthcheck
journalctl -n 20 --no-pager -u vault-idle-stop
tail -n 20 /home/obsidian/.local/state/vault-mcp/audit.log 2>/dev/null"
    ;;
  *) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
