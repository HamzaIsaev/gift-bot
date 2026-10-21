#!/bin/bash
# Gift Bot для macOS: при первом запуске всё устанавливает и настраивает,
# дальше просто запускает бота. Открывать двойным кликом.
set -e
cd "$(dirname "$0")"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

# --- Python 3.11+ ---
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  say "Нужен Python 3.12. Сейчас откроется страница загрузки."
  echo "Установите его, затем в Программы → Python 3.12 запустите «Install Certificates.command»,"
  echo "и снова откройте этот файл."
  open "https://www.python.org/downloads/macos/"
  read -r -p "Нажмите Enter, чтобы закрыть..."
  exit 1
fi

# --- Зависимости ---
if [ ! -x .venv/bin/python ]; then
  say "Устанавливаю зависимости (1–2 минуты)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi

set_env() {  # set_env KEY VALUE — записать значение в .env без вывода на экран
  .venv/bin/python - "$1" "$2" <<'PYEOF'
import sys
from pathlib import Path
p = Path(".env"); key, value = sys.argv[1], sys.argv[2]
lines = [l for l in p.read_text(encoding="utf-8").splitlines() if not l.startswith(key + "=")]
lines.append(f"{key}={value}")
p.write_text("\n".join(lines) + "\n", encoding="utf-8")
PYEOF
}
get_env() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }

# --- Настройка .env ---
[ -f .env ] || cp .env.example .env

if [ -z "$(get_env BOT_TOKEN)" ]; then
  say "Настройка (один раз). Вводимые секреты на экране не отображаются."
  read -r -s -p "Токен бота от @BotFather: " v; echo; set_env BOT_TOKEN "$v"
fi
if [ "$(get_env ADMIN_IDS)" = "[123456789]" ] || [ -z "$(get_env ADMIN_IDS)" ]; then
  read -r -p "Ваш Telegram id (от @userinfobot): " v; set_env ADMIN_IDS "[$v]"
fi
if [ -z "$(get_env TG_API_ID)" ] && [ "$(get_env SKIP_API)" != "1" ]; then
  echo "api_id с my.telegram.org. Если получить не удаётся — просто нажмите Enter:"
  echo "тогда вход на площадки будет через команду /auth в боте (инструкция там)."
  read -r -p "api_id: " v
  if [ -z "$v" ]; then set_env SKIP_API 1; else set_env TG_API_ID "$v"; fi
fi
if [ -n "$(get_env TG_API_ID)" ] && [ -z "$(get_env TG_API_HASH)" ]; then
  read -r -s -p "api_hash с my.telegram.org: " v; echo; set_env TG_API_HASH "$v"
fi
if [ -n "$(get_env TG_API_ID)" ] && [ -z "$(get_env TG_SESSION_STRING)" ]; then
  say "Вход в Telegram-аккаунт для площадок: введите телефон этого аккаунта и код из Telegram."
  TG_API_ID="$(get_env TG_API_ID)" TG_API_HASH="$(get_env TG_API_HASH)" \
    .venv/bin/python scripts/login.py --write-env
fi
if [ -z "$(get_env TG_API_ID)" ]; then
  say "Вход на площадки — ручной: после запуска напишите боту /auth и следуйте инструкции."
fi

# --- Запуск ---
say "Бот запускается. Не закрывайте это окно (остановить — Ctrl+C). Mac не уснёт, пока бот работает."
echo "Админ-панель: http://localhost:8080 (откроется в браузере через несколько секунд)"
(sleep 6 && open "http://localhost:8080") &
caffeinate -i .venv/bin/python -m gift_bot
