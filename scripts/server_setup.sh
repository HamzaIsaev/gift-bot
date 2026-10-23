#!/bin/bash
# Установка Gift Bot на сервер (Ubuntu 22.04/24.04) одной командой:
#   bash ~/gift-bot/scripts/server_setup.sh
# Ставит Docker, задаёт пароль админ-панели и запускает бота 24/7.
set -e
cd "$(dirname "$0")/.."

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

if [ ! -f .env ]; then
  echo "Нет файла .env — скопируйте папку бота с Mac вместе с .env (см. README)."
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  say "Устанавливаю Docker (1–2 минуты)..."
  curl -fsSL https://get.docker.com | sh
fi

get_env() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }
set_env() {
  grep -v -E "^$1=" .env > .env.tmp || true
  echo "$1=$2" >> .env.tmp && mv .env.tmp .env
}

if [ -z "$(get_env PANEL_PASSWORD)" ]; then
  say "Придумайте пароль для админ-панели (на экране не отображается):"
  read -r -s -p "Пароль: " v; echo
  set_env PANEL_PASSWORD "$v"
fi
IP=$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
if [ -z "$(get_env PANEL_DOMAIN)" ]; then
  # Бесплатный адрес с https для мини-приложения: 1.2.3.4 -> 1-2-3-4.sslip.io
  set_env PANEL_DOMAIN "$(echo "$IP" | tr . -).sslip.io"
fi
chmod 600 .env
mkdir -p data

say "Собираю и запускаю бота (первый раз 3–5 минут)..."
docker compose up -d --build

say "Готово! Бот работает 24/7 и сам перезапускается после сбоев и перезагрузки сервера."
echo "Мини-приложение: кнопка «APP» в чате с ботом (или /app)"
echo "Админ-панель в браузере: https://$(get_env PANEL_DOMAIN)  (логин — любой, пароль — тот, что вы ввели)"
echo "Вход на площадки: напишите боту /qr и отсканируйте присланный QR вторым аккаунтом."
echo
echo "Полезные команды:"
echo "  docker compose logs -f --tail 50   — логи"
echo "  docker compose restart             — перезапуск"
echo "  docker compose down                — остановить"
