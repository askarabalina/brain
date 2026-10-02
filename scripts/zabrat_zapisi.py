"""Забрать записи, присланные боту в телеграм, расшифровать и вернуть ответ.

Сервер принимает файл и держит его у себя — телеграм-канал nanobot кладёт
всё присланное в ~/.nanobot/media/telegram/ сам, без участия модели. Считать
там нечем: два ядра без ускорителя.

Поэтому работник — ноутбук. Раз в несколько минут он забирает новые записи,
расшифровывает их локально (whispermlx, с разделением по говорящим) и
присылает готовый документ обратно в телеграм.

Запись при этом не покидает периметр: сервер в Казахстане, расшифровка
на ноутбуке, облачные распознавалки не участвуют.

Запускается по расписанию через launchd — см. scripts/kz.brain.zapisi.plist.
Вручную: python3 scripts/zabrat_zapisi.py [--sukhoy-progon]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from podgotovit_audio import human_time, probe_duration  # noqa: E402

CONFIG_PATH = Path.home() / ".brain-private" / "zapisi.env"

# Что считаем записью. Остальное (фото, документы, pdf) не трогаем.
AUDIO_EXT = {".m4a", ".mp3", ".ogg", ".oga", ".opus", ".wav", ".aac", ".flac",
             ".mp4", ".mov", ".webm", ".mkv", ".avi"}

# Короткие файлы — это голосовые заметки «запиши, что…», а не встречи.
# Их расшифровывает сам бот, и присылать на них документ незачем.
DEFAULT_MIN_MINUTES = 3.0

REMOTE_MEDIA = ".nanobot/media/telegram"


def load_config() -> dict[str, str]:
    """Прочитать настройки. Лежат вне репозитория: там токен бота."""
    if not CONFIG_PATH.is_file():
        sys.exit(
            f"нет файла настроек: {CONFIG_PATH}\n"
            "Скопируй scripts/zapisi.example.env туда и заполни."
        )
    if CONFIG_PATH.stat().st_mode & 0o077:
        sys.exit(f"{CONFIG_PATH}: файл с токеном открыт другим. Выполни: chmod 600 {CONFIG_PATH}")

    config: dict[str, str] = {}
    for line in CONFIG_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        config[key.strip()] = value.strip().strip('"').strip("'")

    for required in ("SERVER", "BOT_TOKEN", "CHAT_ID"):
        if not config.get(required):
            sys.exit(f"в {CONFIG_PATH} не заполнено: {required}")
    return config


def extend_path(config: dict[str, str]) -> None:
    """Добавить в PATH то, что ставится в домашнюю папку.

    launchd запускает скрипт с голым системным PATH: ни ffmpeg, ни whispermlx,
    поставленные через uv или pip --user, туда не входят. Руками из терминала
    всё работает, по расписанию — нет, и это самая неочевидная поломка
    во всей цепочке.
    """
    extra = [str(Path.home() / ".local" / "bin")]
    if whisper := config.get("WHISPERMLX"):
        extra.append(str(Path(whisper).expanduser().parent))
    current = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(extra + current.split(os.pathsep)))


def telegram(config: dict[str, str], method: str, fields: list[tuple[str, str]]) -> bool:
    """Вызов Bot API через curl.

    Аргументы передаются curl'у через stdin (-K -), а не командной строкой:
    иначе токен виден в выводе ps любому процессу на машине.
    """
    url = f"https://api.telegram.org/bot{config['BOT_TOKEN']}/{method}"
    args = [f'url = "{url}"'] + [f'form = "{name}={value}"' for name, value in fields]
    try:
        result = subprocess.run(
            ["curl", "-sS", "-K", "-"],
            input="\n".join(args), capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"телеграм недоступен: {exc}", file=sys.stderr)
        return False
    ok = result.returncode == 0 and '"ok":true' in result.stdout
    if not ok:
        print(f"телеграм отказал ({method}): {result.stdout[:200]}{result.stderr[:200]}",
              file=sys.stderr)
    return ok


def ssh(config: dict[str, str], command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", config["SERVER"], command],
        capture_output=True, text=True, timeout=120,
    )


def remote_recordings(config: dict[str, str]) -> list[tuple[str, int]]:
    """Имена и время изменения файлов в папке, куда бот складывает присланное."""
    result = ssh(config, f'stat -c "%Y %n" ~/{REMOTE_MEDIA}/* 2>/dev/null || true')
    if result.returncode != 0:
        sys.exit(f"сервер не ответил: {result.stderr.strip() or 'соединение не установилось'}")

    found: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        mtime, _, path = line.strip().partition(" ")
        name = path.rsplit("/", 1)[-1]
        if name and Path(name).suffix.lower() in AUDIO_EXT and mtime.isdigit():
            found.append((name, int(mtime)))
    return sorted(found, key=lambda item: item[1])


def speaker_label(raw: str | None, order: dict[str, int]) -> str:
    """SPEAKER_00 → «Говорящий 1». Имена не угадываем, их подставляет человек."""
    if not raw:
        return "Говорящий ?"
    if raw not in order:
        order[raw] = len(order) + 1
    return f"Говорящий {order[raw]}"


def segments_from_json(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text())
    segments = data.get("segments") if isinstance(data, dict) else None
    return segments if isinstance(segments, list) else []


def build_document(segments: list[dict[str, object]], source: str, duration: float) -> str:
    """Склеить соседние реплики одного говорящего, метки — на смене говорящего."""
    order: dict[str, int] = {}
    blocks: list[tuple[float, str, list[str]]] = []

    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        who = speaker_label(segment.get("speaker"), order)  # type: ignore[arg-type]
        start = float(segment.get("start", 0.0))  # type: ignore[arg-type]
        if blocks and blocks[-1][1] == who:
            blocks[-1][2].append(text)
        else:
            blocks.append((start, who, [text]))

    head = [
        f"# Расшифровка · {datetime.now():%d.%m.%Y}",
        "",
        f"**Запись:** {source}, {human_time(duration)}",
        f"**Говорящих:** {len(order) or '—'}",
        "",
        "Имена не подставлены: кто есть кто, знает только человек. "
        "Названия систем распознаны начерно — выправляются при разборе.",
        "",
        "---",
        "",
    ]
    body = [f"**[{human_time(start)}] {who}:** {' '.join(parts)}\n"
            for start, who, parts in blocks]
    return "\n".join(head + body)


def transcribe(audio: Path, work_dir: Path, config: dict[str, str]) -> Path | None:
    """Расшифровать локально. Возвращает путь к .json или None."""
    whisper = config.get("WHISPERMLX") or shutil.which("whispermlx")
    if not whisper:
        print("whispermlx не найден. Укажи полный путь в WHISPERMLX=", file=sys.stderr)
        return None

    out_dir = work_dir / "расшифровки"
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [
        whisper, str(audio),
        "--language", "ru",
        "--model", config.get("MODEL", "large-v3-turbo"),
        "--diarize",
        "--output_format", "json",
        "--output_dir", str(out_dir),
    ]
    if token := config.get("HF_TOKEN"):
        command += ["--hf_token", token]

    result = subprocess.run(command, capture_output=True, text=True, timeout=14400)
    produced = out_dir / f"{audio.stem}.json"
    if result.returncode != 0 or not produced.is_file():
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        print(f"расшифровка не удалась: {' / '.join(tail)}", file=sys.stderr)
        return None
    return produced


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Забрать записи из телеграма, расшифровать и вернуть документ.")
    parser.add_argument("--sukhoy-progon", action="store_true",
                        help="показать, что нашлось, и ничего не делать")
    args = parser.parse_args()

    config = load_config()
    extend_path(config)
    work_dir = Path(config.get("ZAPISI_DIR", "~/Записи")).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    ledger = work_dir / ".сделано"
    done = set(ledger.read_text().split()) if ledger.is_file() else set()
    min_minutes = float(config.get("MIN_MINUTES", DEFAULT_MIN_MINUTES))

    fresh = [(name, mtime) for name, mtime in remote_recordings(config) if name not in done]
    if not fresh:
        return

    if args.sukhoy_progon:
        for name, mtime in fresh:
            print(f"{datetime.fromtimestamp(mtime):%d.%m %H:%M}  {name}")
        return

    incoming = work_dir / "входящие"
    incoming.mkdir(exist_ok=True)

    for name, mtime in fresh:
        local = incoming / name
        copied = subprocess.run(
            ["scp", "-q", "-o", "BatchMode=yes",
             f"{config['SERVER']}:~/{REMOTE_MEDIA}/{name}", str(local)],
            capture_output=True, text=True, timeout=1800,
        )
        if copied.returncode != 0:
            print(f"{name}: не скачался — {copied.stderr.strip()}", file=sys.stderr)
            continue

        duration = probe_duration(local)
        # Короткое — голосовая заметка, бот ответил на неё сам. Отмечаем и уходим,
        # чтобы не присылать документ на каждое «запиши, что…».
        if duration is not None and duration < min_minutes * 60:
            done.add(name)
            ledger.write_text("\n".join(sorted(done)) + "\n")
            local.unlink(missing_ok=True)
            continue
        if duration is None:
            # Нет ffmpeg или битый файл. Пропустить молча нельзя: так встреча
            # уедет в «сделано» и больше не вернётся. Лучше расшифровать зря.
            print(f"{name}: длительность не определилась, расшифровываю без проверки",
                  file=sys.stderr)
            duration = 0.0

        stamp = datetime.fromtimestamp(mtime)
        telegram(config, "sendMessage", [
            ("chat_id", config["CHAT_ID"]),
            ("text", f"Взял запись от {stamp:%d.%m %H:%M} — {human_time(duration)}. "
                     f"Расшифровываю, пришлю документом."),
        ])

        produced = transcribe(local, work_dir, config)
        if produced is None:
            telegram(config, "sendMessage", [
                ("chat_id", config["CHAT_ID"]),
                ("text", f"Запись от {stamp:%d.%m %H:%M} расшифровать не вышло. "
                         f"Подробности в журнале на ноутбуке."),
            ])
            continue

        document = work_dir / f"расшифровка-{stamp:%Y-%m-%d-%H%M}.md"
        document.write_text(build_document(segments_from_json(produced), name, duration))

        sent = telegram(config, "sendDocument", [
            ("chat_id", config["CHAT_ID"]),
            ("document", f"@{document}"),
            ("caption", f"Расшифровка записи от {stamp:%d.%m %H:%M}. "
                        f"Черновая: имена не подставлены, названия систем могут быть перевраны."),
        ])
        if sent:
            done.add(name)
            ledger.write_text("\n".join(sorted(done)) + "\n")
        print(f"готово: {document}")


if __name__ == "__main__":
    main()
