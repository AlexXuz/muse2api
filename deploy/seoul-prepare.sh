#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
test "$(id -u)" = 0
if ! id muse2api >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/muse2api --create-home --shell /usr/sbin/nologin muse2api
fi
install -d -m 750 -o muse2api -g muse2api /var/lib/muse2api
install -d -m 755 /opt/muse2api
if ! command -v google-chrome-stable >/dev/null; then
  curl --fail --location --silent --show-error --max-time 180 https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /var/tmp/muse2api-google-chrome.deb
  apt-get update -qq
  apt-get install -y --no-install-recommends python3-venv fonts-noto-cjk /var/tmp/muse2api-google-chrome.deb
  rm -- /var/tmp/muse2api-google-chrome.deb
fi
test -x /opt/muse2api/venv/bin/python || python3 -m venv /opt/muse2api/venv
if ! test -e /swapfile-muse2api; then
  fallocate -l 2G /swapfile-muse2api
  chmod 600 /swapfile-muse2api
  mkswap /swapfile-muse2api
  swapon /swapfile-muse2api
  cp -a /etc/fstab /etc/fstab.before-muse2api
  printf '\n/swapfile-muse2api none swap sw 0 0\n' >> /etc/fstab
fi
google-chrome-stable --version
free -m
