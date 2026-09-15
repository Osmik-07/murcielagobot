#!/usr/bin/env bash
# Выполняется на VPS через forced command из authorized_keys пользователя deploy —
# GitHub Actions не может передать сюда ничего, кроме самого подключения.
#
# Всё тело — в функции не для красоты: ниже мы делаем git reset, который
# переписывает ЭТОТ ЖЕ файл прямо во время выполнения. Bash дочитывает скрипт
# с диска по мере исполнения, поэтому в «плоском» виде он после обновления
# продолжал бы читать новый файл со старого смещения — то есть пропускал или
# коверкал команды. Функция разбирается целиком до первого вызова, и подмена
# файла на текущий запуск уже не влияет.
set -euo pipefail

main() {
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
  (cd miniapp && npm ci --silent --no-audit --no-fund && npm run build)

  echo "==> рестарт сервиса"
  sudo systemctl restart murcielagobot
  sleep 2
  sudo systemctl is-active murcielagobot

  echo "==> готово: $(git rev-parse --short HEAD)"
}

main "$@"
