# Развёртывание на сервере

Чтобы бот работал, когда ноутбук выключен, и утренняя сводка приходила
в 8:30 сама.

## Какой сервер

Бот ходит наружу и входящих подключений не принимает. Поэтому **не нужны**:
публичный IP, домен, TLS, открытые порты, согласование с ИБ на публикацию.
Подойдёт самая дешёвая виртуалка.

| | Значение |
|---|---|
| Процессор | 2 ядра |
| Память | 4 ГБ |
| Диск | 40 ГБ |
| ОС | Ubuntu 24.04 LTS |
| Площадка | Казахстан — данные хранятся в РК |

Если позже на том же сервере появится n8n — бери 4 ядра, 8 ГБ, 80 ГБ сразу,
переносить будет лень.

Это **не тот сервер**, который нужен коннектору Claude.ai: там Claude
подключается из облака к тебе, и нужен публичный адрес с доменом, TLS
и согласованием. Здесь ничего этого нет.

## Подготовка

Всё под `root`, по SSH.

```bash
apt update && apt upgrade -y
apt install -y python3-venv python3-pip git ufw fail2ban

# Пускаем только SSH: бот входящих не принимает, открывать нечего.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# Отдельный пользователь: бот не должен ходить от root.
adduser --disabled-password --gecos "" bot
```

## Установка

```bash
su - bot

git clone https://github.com/askarabalina/project.git nanobot
cd nanobot && python3 -m venv .venv && .venv/bin/pip install -e . && cd ..

git clone https://github.com/askarabalina/brain.git
cd brain && python3 -m venv .venv && .venv/bin/pip install -e . && cd ..
```

## Конфиг и секреты

```bash
mkdir -p ~/.nanobot && chmod 700 ~/.nanobot
cp ~/brain/telegram/config.example.json ~/.nanobot/config.json
```

В `~/.nanobot/config.json` замени `ПУТЬ/К/brain` на `/home/bot/brain`
в двух местах: `command` и `BRAIN_DIR`.

```bash
cat > ~/.nanobot/env <<'EOF'
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_USER_ID=
EXECUTION_HUB_MCP_URL=
EXECUTION_HUB_TOKEN=
TRANSCRIPTION_API_KEY=
EOF

chmod 600 ~/.nanobot/env
```

Заполни значения. Файл читает только systemd, в git он не попадает.

Проверка до того, как делать службу:

```bash
set -a && . ~/.nanobot/env && set +a
~/nanobot/.venv/bin/nanobot gateway --config ~/.nanobot/config.json
```

Напиши боту в телеграм «что ты про меня знаешь». Ответил — `Ctrl+C`
и дальше.

## Служба

```bash
exit   # обратно в root

cp /home/bot/brain/telegram/nanobot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nanobot
systemctl status nanobot
```

Теперь бот переживает перезагрузку и падение.

```bash
journalctl -u nanobot -f        # смотреть логи
systemctl restart nanobot       # после правки конфига
```

## Утренняя сводка

Попроси бота прямо в телеграме:

> каждый будний день в 8:30 присылай сводку: сначала блокеры с числом дней
> молчания, потом просрочки, потом сроки на сегодня

Он заведёт расписание сам, через свой инструмент `cron`. Проверь часовой
пояс — в конфиге стоит `Asia/Almaty`, а на свежей виртуалке время обычно UTC:

```bash
timedatectl set-timezone Asia/Almaty
```

## Обновление

```bash
su - bot
cd ~/brain && git pull && .venv/bin/pip install -e .
cd ~/nanobot && git pull && .venv/bin/pip install -e .
exit
systemctl restart nanobot
```

База знаний на сервере — отдельный клон. Заметки, продиктованные в телеграм,
коммитятся там, и на ноутбук приедут через `git pull`. Если правишь базу
и там, и там — пушь с одной стороны, прежде чем править с другой, иначе
придётся разбирать расхождение.

## Чего на сервере быть не должно

- **Справочника личных данных.** `~/.brain-private/` остаётся только
  на ноутбуке. В конфиге бота `get_reference` и так не зарегистрирован.
- **Инструментов записи в Execution Hub.** Список `enabledTools` в конфиге —
  единственное, что отделяет «спросил с телефона» от «случайно поменял
  статус задачи без просмотра». Не расширяй его ради удобства.
- **Ключей в git.** Секреты только в `~/.nanobot/env` с правами 600.
