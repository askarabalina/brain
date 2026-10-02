# Развёртывание на сервере

Чтобы бот работал, когда ноутбук выключен, и утренняя сводка приходила сама.

Проверено на ps.kz, тариф Basic-2 (2 ГБ памяти, 2 ядра, 40 ГБ), Астана.

## Какой сервер

Бот ходит наружу и входящих подключений не принимает. Поэтому **не нужны**:
публичный IP, домен, TLS, открытые порты, согласование с ИБ на публикацию.

| | Значение |
|---|---|
| Процессор | 2 ядра |
| Память | 2 ГБ (с запасом — 4) |
| Диск | 40 ГБ |
| ОС | Ubuntu LTS |
| Площадка | Казахстан — данные хранятся в РК |

Планируется n8n — бери 4 ГБ сразу, переносить потом будет лень.

Это **не тот сервер**, который нужен коннектору Claude.ai: там Claude
подключается из облака к тебе, и нужен публичный адрес с доменом и TLS.

## Подключение

Провайдер присылает IP, пользователя и пароль. У ps.kz пользователь `ubuntu`,
не `root`.

```bash
ssh ubuntu@АДРЕС
```

Первый раз спросит про подлинность узла — ответить `yes` целиком. Пароль
при вводе **не отображается никак**: ни точек, ни звёздочек. Это нормально.

Права администратора:

```bash
sudo -i
```

Приглашение сменится с `$` на `#`. Обратно — `exit`.

## Подготовка (от root)

```bash
apt update && apt upgrade -y
apt install -y python3-venv python3-pip git ufw
```

Во время обновления появится фиолетовый экран «Daemons using outdated
libraries». Ничего не менять, нажать Enter.

Фаервол — **одной строкой**: если включить его раньше, чем разрешить SSH,
доступ к серверу оборвётся.

```bash
ufw default deny incoming && ufw default allow outgoing && ufw allow OpenSSH && ufw --force enable
```

## Установка (от ubuntu)

```bash
su - ubuntu
```

**Python.** В Ubuntu 22.04 системный Python 3.10, а нужен 3.11+. Ставим свежий
через `uv`, он скачает его сам:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Дальше полный путь `~/.local/bin/uv`, чтобы не возиться с переменными
окружения.

**Доступ к приватному репозиторию.** Ключ только на чтение одного репозитория:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github -N "" -C "assistant-vps"
cat ~/.ssh/github.pub
```

Вывод добавить на github.com → репозиторий → Settings → Deploy keys →
Add deploy key. **Галочку «Allow write access» не ставить.**

```bash
printf 'Host github.com\n  IdentityFile ~/.ssh/github\n  IdentitiesOnly yes\n' > ~/.ssh/config
chmod 600 ~/.ssh/config
git clone git@github.com:askarabalina/brain.git
```

**Окружения:**

```bash
~/.local/bin/uv venv --python 3.12 ~/brain/.venv
~/.local/bin/uv pip install --python ~/brain/.venv/bin/python -e ~/brain

~/.local/bin/uv venv --python 3.12 ~/nanobot-venv
~/.local/bin/uv pip install --python ~/nanobot-venv/bin/python nanobot-ai
```

nanobot ставится пакетом с PyPI — клонировать его не нужно. `uv` создаёт
окружения без `pip` внутри, поэтому всё через `uv pip install`.

## Конфиг и секреты

```bash
mkdir -p ~/.nanobot/workspace && chmod 700 ~/.nanobot
cp ~/brain/telegram/config.example.json ~/.nanobot/config.json
cp ~/brain/telegram/SOUL.example.md ~/.nanobot/workspace/SOUL.md
```

В `~/.nanobot/config.json` заменить `ПУТЬ/К/brain` на `/home/ubuntu/brain`
в двух местах: `command` и `BRAIN_DIR`.

**Без `SOUL.md` бот будет писать в Hub без черновика и без журнала.** Это самый
легко теряющийся шаг.

```bash
touch ~/.nanobot/env && chmod 600 ~/.nanobot/env
nano ~/.nanobot/env
```

Значения — свои, по одному на строку. Сохранить: Ctrl+O, Enter, Ctrl+X.

```
DEEPSEEK_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_USER_ID=
EXECUTION_HUB_MCP_URL=
EXECUTION_HUB_TOKEN=
```

### Про выбор провайдера

**Gemini из Казахстана не работает:** `User location is not supported for the
API use`. Это география Google, конфигом не лечится.

Anthropic Казахстан поддерживает. Подписка Claude Max при этом доступа к API
не даёт — это отдельный счёт.

Имена моделей устаревают: `gemini-2.5-flash` уже отвечает «no longer available
to new users». Проверяй в консоли провайдера, а не по памяти.

## Проверка до службы

```bash
set -a && . ~/.nanobot/env && set +a
~/nanobot-venv/bin/nanobot status --config ~/.nanobot/config.json
```

Ключевая строка — `Agent: ✓ provider/model configuration is ready`.

```bash
~/nanobot-venv/bin/nanobot gateway --config ~/.nanobot/config.json
```

Команда не вернёт приглашение — бот работает и пишет в терминал. Написать боту
в телеграм «что ты про меня знаешь». Ответил — остановить `Ctrl+C`.

## Служба

```bash
exit   # обратно в root

cp /home/ubuntu/brain/telegram/nanobot.service /etc/systemd/system/
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

Попросить бота прямо в телеграме:

> каждый будний день в 8:30 присылай сводку: сначала блокеры с числом дней
> молчания, потом просрочки, потом сроки на сегодня

Он заведёт расписание сам, через свой инструмент `cron`. Часовой пояс сервера
обычно UTC:

```bash
timedatectl set-timezone Asia/Almaty
```

## Обновление

```bash
su - ubuntu
cd ~/brain && git pull && ~/.local/bin/uv pip install --python ~/brain/.venv/bin/python -e ~/brain
~/.local/bin/uv pip install --python ~/nanobot-venv/bin/python --upgrade nanobot-ai
exit
systemctl restart nanobot
```

База знаний на сервере — отдельный клон. Заметки, продиктованные в телеграм,
коммитятся там и приезжают на ноутбук через `git pull`. Привычка простая:
перед правкой `git pull`, после — `git push`.

## Чего на сервере быть не должно

- **Справочника личных данных.** `~/.brain-private/` остаётся на ноутбуке.
  В конфиге бота `get_reference` и так не зарегистрирован.
- **Расшифровки записей.** Она считается на ноутбуке: на виртуалке без
  ускорителя это часы.
- **Ключей в git.** Секреты только в `~/.nanobot/env` с правами 600.
