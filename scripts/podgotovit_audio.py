"""Подготовка аудио к распознаванию и к отправке в телеграм.

Приводит любую запись к тому, что нужно распознавалке: моно, 16 кГц. Этого
достаточно — Whisper и родственные модели всё равно работают на 16 кГц моно,
так что более высокое качество исходника не улучшает распознавание, а только
раздувает файл.

Отдельно считает, влезет ли результат в лимит телеграма: бот может скачать
не больше 20 МБ, и часовая запись в обычном качестве туда не помещается.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Бот Telegram скачивает файлы не больше 20 МБ (getFile). Это ограничение
# платформы, а не настройки: обходится только своим сервером Bot API.
TELEGRAM_LIMIT_BYTES = 20 * 1024 * 1024

# Распознавалки работают на 16 кГц моно. Выше — не точнее, только тяжелее.
TARGET_RATE = 16000


def probe_duration(path: Path) -> float | None:
    """Длительность в секундах. None, если формат не разобрался.

    Пробуем ffprobe, а если его нет — достаём из вывода самого ffmpeg.
    Отдельный ffprobe есть не везде: сборки, которые ставятся как пакет
    Python, обычно содержат только ffmpeg.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return float(json.loads(result.stdout)["format"]["duration"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        pass

    # ffmpeg без выходного файла завершается с ошибкой, но до этого печатает
    # сведения о входе — оттуда и берём «Duration: 00:12:34.56».
    result = subprocess.run(
        ["ffmpeg", "-i", str(path)], capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def human_time(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def human_size(num_bytes: int) -> str:
    mb = num_bytes / 1024 / 1024
    return f"{mb:.1f} МБ"


def convert(src: Path, dst: Path, bitrate_kbps: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-vn",                              # видеодорожку выбрасываем
         "-ac", "1",                          # моно
         "-ar", str(TARGET_RATE),
         "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k",
         # Opus с профилем для голоса: на речи заметно экономнее музыкального.
         "-application", "voip",
         str(dst)],
        check=True, timeout=3600,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Подготовить запись к распознаванию и к отправке в телеграм.")
    parser.add_argument("source", type=Path, help="аудио или видео любого формата")
    parser.add_argument("-o", "--out", type=Path, help="куда писать (по умолчанию рядом, .ogg)")
    parser.add_argument("-b", "--bitrate", type=int, default=24,
                        help="килобит в секунду, по умолчанию 24 — хватает для речи")
    args = parser.parse_args()

    if not args.source.is_file():
        sys.exit(f"нет файла: {args.source}")

    duration = probe_duration(args.source)
    if duration is None:
        sys.exit(f"{args.source.name}: формат не распознан — это точно аудио или видео?")

    target = args.out or args.source.with_suffix(".ogg")
    print(f"Исходник:  {args.source.name}, {human_time(duration)}, "
          f"{human_size(args.source.stat().st_size)}")

    try:
        convert(args.source, target, args.bitrate)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"ffmpeg не справился (код {exc.returncode}). Файл повреждён или это не аудио.")

    size = target.stat().st_size
    print(f"Готово:    {target.name}, {human_size(size)}, моно {TARGET_RATE} Гц, "
          f"{args.bitrate} кбит/с")

    if size <= TELEGRAM_LIMIT_BYTES:
        print("Телеграм:  влезает, можно отправлять боту")
        return

    print(f"Телеграм:  НЕ влезает, лимит бота {human_size(TELEGRAM_LIMIT_BYTES)}")

    # Считаем от фактического размера, а не от заказанного битрейта: opus
    # переменный и на сложном материале заметно превышает заданную цифру.
    # Запас в пять процентов — на разброс между прогонами.
    needed = int(args.bitrate * TELEGRAM_LIMIT_BYTES / size * 0.95)
    if needed >= 8:
        print(f"           пересобрать с -b {needed} или меньше")
    else:
        print("           даже 8 кбит/с не спасут — запись слишком длинная.")
        print("           Резать на части или распознавать на компьютере, "
              "минуя телеграм.")


if __name__ == "__main__":
    main()
