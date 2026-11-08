#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

watchdog_user=${WATCHDOG_USER:-${SUDO_USER:-}}
if [[ -z ${watchdog_user} || ${watchdog_user} == root ]]; then
    echo "Set WATCHDOG_USER to the unprivileged account that owns this repository." >&2
    exit 1
fi
watchdog_group=$(id -gn "${watchdog_user}")

script_directory=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd "${script_directory}/../.." && pwd)
python=${IBKR_WATCHDOG_PYTHON:-${repository_root}/.venv/bin/python}
docker=${IBKR_DOCKER:-/usr/bin/docker}

for required in "${python}" "${docker}" "${repository_root}/scripts/gateway_watchdog.py"; do
    if [[ ! -e ${required} ]]; then
        echo "Required path does not exist: ${required}" >&2
        exit 1
    fi
done

container_name=${IBKR_GATEWAY_CONTAINER_NAME:-}
if [[ -z ${container_name} ]]; then
    mapfile -t gateway_containers < <(
        "${docker}" ps --all \
            --filter label=com.docker.compose.project=ibkr-paper \
            --filter label=com.docker.compose.service=ib-gateway \
            --format '{{.Names}}'
    )
    if [[ ${#gateway_containers[@]} -ne 1 ]]; then
        echo "Expected exactly one ibkr-paper Gateway container; found ${#gateway_containers[@]}." >&2
        echo "Start the Compose stack first, or set IBKR_GATEWAY_CONTAINER_NAME." >&2
        exit 1
    fi
    container_name=${gateway_containers[0]}
fi
if [[ ! ${container_name} =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    echo "Invalid Gateway container name: ${container_name}" >&2
    exit 1
fi

temporary_directory=$(mktemp -d)
trap 'rm -rf "${temporary_directory}"' EXIT

render() {
    sed \
        -e "s|@WATCHDOG_USER@|${watchdog_user}|g" \
        -e "s|@WATCHDOG_GROUP@|${watchdog_group}|g" \
        -e "s|@REPOSITORY_ROOT@|${repository_root}|g" \
        -e "s|@PYTHON@|${python}|g" \
        -e "s|@DOCKER@|${docker}|g" \
        -e "s|@CONTAINER_NAME@|${container_name}|g" \
        "$1" > "$2"
}

render \
    "${script_directory}/restart-ibkr-gateway.in" \
    "${temporary_directory}/restart-ibkr-gateway"
render \
    "${script_directory}/ibkr-gateway-watchdog.sudoers.in" \
    "${temporary_directory}/ibkr-gateway-watchdog.sudoers"
render \
    "${script_directory}/ibkr-gateway-watchdog.service.in" \
    "${temporary_directory}/ibkr-gateway-watchdog.service"

chmod 0440 "${temporary_directory}/ibkr-gateway-watchdog.sudoers"
visudo -cf "${temporary_directory}/ibkr-gateway-watchdog.sudoers"

install -o root -g root -m 0755 \
    "${temporary_directory}/restart-ibkr-gateway" \
    /usr/local/sbin/restart-ibkr-gateway
install -o root -g root -m 0440 \
    "${temporary_directory}/ibkr-gateway-watchdog.sudoers" \
    /etc/sudoers.d/ibkr-gateway-watchdog
install -o root -g root -m 0644 \
    "${temporary_directory}/ibkr-gateway-watchdog.service" \
    /etc/systemd/system/ibkr-gateway-watchdog.service

if [[ ! -e /etc/default/ibkr-gateway-watchdog ]]; then
    install -o root -g root -m 0644 \
        "${script_directory}/ibkr-gateway-watchdog.env" \
        /etc/default/ibkr-gateway-watchdog
fi

systemctl daemon-reload
systemctl enable ibkr-gateway-watchdog.service
systemctl restart ibkr-gateway-watchdog.service
systemctl --no-pager --full status ibkr-gateway-watchdog.service
