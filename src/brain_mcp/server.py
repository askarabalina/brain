"""MCP-сервер личной базы знаний.

Отдаёт ИИ-клиентам (Claude Code, Gemini CLI) доступ к папке `brain/`:
профиль, карточки проектов, хронологическую ленту и итоги сессий.

Задачи здесь намеренно не хранятся — источник правды по ним Execution Hub,
который подключается отдельным MCP-сервером.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("brain")

# Разрешённые источники записи. Каждая запись обязана назвать, откуда взялась,
# иначе через полгода невозможно понять, чему верить.
SOURCES = ("встреча", "чат", "сессия", "ручной ввод")

MAX_RESULTS = 40
SNIPPET_LINES = 3


def brain_dir() -> Path:
    """Корень базы знаний: переменная BRAIN_DIR либо `brain/` рядом с пакетом."""
    env = os.environ.get("BRAIN_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parents[2] / "brain").resolve()


def slugify(name: str) -> str:
    """Имя проекта → имя файла. Кириллица остаётся как есть, её понимает git."""
    slug = name.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug, flags=re.UNICODE)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")


def resolve_project_file(name: str) -> Path:
    """Путь к карточке проекта с проверкой, что он не вышел за пределы базы.

    Имя проекта приходит из ответа модели, то есть косвенно — из текста, который
    мы не контролируем. Без этой проверки `../../.ssh/id_rsa` стал бы читаемым.
    """
    projects = brain_dir() / "projects"
    candidate = (projects / f"{slugify(name)}.md").resolve()
    if not candidate.is_relative_to(projects.resolve()):
        raise ValueError(f"Недопустимое имя проекта: {name!r}")
    return candidate


def known_projects() -> list[str]:
    projects = brain_dir() / "projects"
    if not projects.is_dir():
        return []
    return sorted(p.stem for p in projects.glob("*.md"))


def read_if_exists(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None


@mcp.tool()
def whoami() -> str:
    """Профиль пользователя и список проектов в базе знаний.

    Вызывать в начале сессии, до остальных вопросов о работе пользователя.
    """
    profile = read_if_exists(brain_dir() / "profile.md")
    if profile is None:
        return (
            "Профиль не заполнен: нет файла brain/profile.md. "
            "Скажи пользователю, что базу знаний нужно наполнить."
        )

    parts = [profile]

    people = read_if_exists(brain_dir() / "people.md")
    if people:
        parts.append(people)

    projects = known_projects()
    if projects:
        parts.append("## Карточки проектов в базе\n\n" + "\n".join(f"- {p}" for p in projects))

    return "\n\n---\n\n".join(parts)


@mcp.tool()
def list_projects() -> str:
    """Имена всех карточек проектов в базе знаний."""
    projects = known_projects()
    if not projects:
        return "Карточек проектов пока нет."
    return "\n".join(f"- {p}" for p in projects)


@mcp.tool()
def get_project(name: str) -> str:
    """Карточка проекта целиком: цели, вехи, решения, блокеры, люди, зависимости.

    Читать перед любым разговором о проекте — в ней то, чего нет в Execution Hub:
    почему решения приняли именно такими и на ком застряли блокеры.
    """
    path = resolve_project_file(name)
    content = read_if_exists(path)
    if content is None:
        available = known_projects()
        hint = "\n".join(f"- {p}" for p in available) if available else "(пусто)"
        return f"Карточки {name!r} нет. Что есть:\n{hint}"
    return content


@mcp.tool()
def search_memory(query: str, project: str | None = None) -> str:
    """Поиск по базе знаний. Возвращает совпадения с путём к файлу.

    query: слово или фраза, регистр не важен.
    project: ограничить одной карточкой проекта.
    """
    root = brain_dir()
    target = resolve_project_file(project) if project else root
    if not target.exists():
        return f"Нечего искать: {target} не существует."

    # ripgrep есть не везде, grep -r есть везде.
    result = subprocess.run(
        ["grep", "-rniI", f"-A{SNIPPET_LINES}", "--", query, str(target)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    # grep возвращает 1, когда совпадений нет: это не ошибка.
    if result.returncode > 1:
        return f"Поиск не удался: {result.stderr.strip()}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        return f"По запросу {query!r} ничего не найдено."

    trimmed = lines[:MAX_RESULTS]
    out = "\n".join(ln.replace(str(root) + "/", "") for ln in trimmed)
    if len(lines) > MAX_RESULTS:
        out += f"\n\n… ещё {len(lines) - MAX_RESULTS} строк, уточни запрос."
    return out


@mcp.tool()
def save_note(text: str, project: str, source: str) -> str:
    """Записать факт или решение в хронологическую ленту базы знаний.

    text: что произошло и что это меняет, 2-4 предложения.
    project: к какому проекту относится, либо "общее".
    source: откуда известно — встреча, чат, сессия или ручной ввод.

    Записывать только то, что пользователь сказал сам или подтвердил.
    Не записывать содержимое документов и переписок без его просьбы.
    """
    if source not in SOURCES:
        return f"Источник должен быть одним из: {', '.join(SOURCES)}. Получено: {source!r}"
    if not text.strip():
        return "Пустую запись сохранять не буду."

    today = date.today()
    log = brain_dir() / "log" / f"{today:%Y-%m}.md"
    log.parent.mkdir(parents=True, exist_ok=True)

    if not log.exists():
        log.write_text(f"# {today:%Y-%m}\n\nХронологическая лента.\n", encoding="utf-8")

    entry = (
        f"\n---\n\n## {today:%Y-%m-%d}\n\n"
        f"**Проект:** {project} · **Источник:** {source}\n\n"
        f"{text.strip()}\n"
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write(entry)

    return f"Записано в {log.relative_to(brain_dir().parent)}."


@mcp.tool()
def save_session_summary(
    title: str,
    summary: str,
    project: str,
    decisions: list[str] | None = None,
    next_steps: list[str] | None = None,
) -> str:
    """Сохранить итоги текущей сессии в базу знаний.

    Вызывать в конце сессии, когда было принято решение или сделана работа,
    о которой стоит помнить в следующий раз. Проходные вопросы не сохранять.
    """
    if not title.strip() or not summary.strip():
        return "Нужны и заголовок, и краткое содержание."

    today = date.today()
    path = brain_dir() / "sessions" / f"{today:%Y-%m-%d}-{slugify(title)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    body = [
        "---",
        f"date: {today:%Y-%m-%d}",
        f"project: {project}",
        "source: сессия",
        "---",
        "",
        f"# {title.strip()}",
        "",
        summary.strip(),
    ]
    if decisions:
        body += ["", "## Решения", ""] + [f"- {d}" for d in decisions]
    if next_steps:
        body += ["", "## Дальше", ""] + [f"- [ ] {s}" for s in next_steps]

    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return f"Итоги сохранены в {path.relative_to(brain_dir().parent)}."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
