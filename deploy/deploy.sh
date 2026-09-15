#!/usr/bin/env bash
# Выполняется на VPS через forced command из authorized_keys пользователя deploy —
# GitHub Actions не может передать сюда ничего, кроме самого подключения.
set -euo pipefail

cd /opt/murcielagobot

echo "==> git pull"
git fetch origin main
git reset --hard origin/main

echo "==> зависимости"
./.venv/bin/pip install --quiet -r requirements.txt

echo "==> миграции БД"
./.venv/bin/alembic upgrade head

echo "==> сборка Mini App"
# Собираем на сервере, а не в CI: GitHub Actions по замыслу умеет ровно одно —
# дёрнуть этот скрипт, вся логика деплоя живёт здесь.
(cd miniapp && npm ci --silent && npm run build)

echo "==> рестарт сервиса"
sudo systemctl restart murcielagobot
sleep 2
sudo systemctl is-active murcielagobot

echo "==> готово: $(git rev-parse --short HEAD)"
