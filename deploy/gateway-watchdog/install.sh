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
compose_directory=${IBKR_COMPOSE_DIRECTORY:-/home/${watchdog_user}/ibkr-stack}
compose_service=${IBKR_COMPOSE_SERVICE:-ib-gateway}
docker=${IBKR_DOCKER:-/usr/bin/docker}

if [[ -n ${IBKR_COMPOSE_FILE:-} ]]; then
    compose_file=${IBKR_COMPOSE_FILE}
elif [[ -f ${compose_directory}/compose.yml ]]; then
    compose_file=${compose_directory}/compose.yml
elif [[ -f ${compose_directory}/compose.yaml ]]; then
    compose_file=${compose_directory}/compose.yaml
elif [[ -f ${compose_directory}/docker-compose.yml ]]; then
    compose_file=${compose_directory}/docker-compose.yml
else
    echo "Could not locate a Compose file in ${compose_directory}." >&2
    exit 1
fi

for required in "${python}" "${docker}" "${repository_root}/scripts/gateway_watchdog.py"; do
    if [[ ! -e ${required} ]]; then
        echo "Required path does not exist: ${required}" >&2
        exit 1
    fi
done

temporary_directory=$(mktemp -d)
trap 'rm -rf "${temporary_directory}"' EXIT

render() {
    sed \
        -e "s|@WATCHDOG_USER@|${watchdog_user}|g" \
        -e "s|@WATCHDOG_GROUP@|${watchdog_group}|g" \
        -e "s|@REPOSITORY_ROOT@|${repository_root}|g" \
        -e "s|@PYTHON@|${python}|g" \
        -e "s|@DOCKER@|${docker}|g" \
        -e "s|@COMPOSE_DIRECTORY@|${compose_directory}|g" \
        -e "s|@COMPOSE_FILE@|${compose_file}|g" \
        -e "s|@COMPOSE_SERVICE@|${compose_service}|g" \
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
