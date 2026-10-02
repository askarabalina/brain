"""MCP-сервер личной базы знаний.

Отдаёт ИИ-клиентам (Claude Code, Gemini CLI) доступ к папке `brain/`:
профиль, карточки проектов, хронологическую ленту и итоги сессий.

Задачи здесь намеренно не хранятся — источник правды по ним Execution Hub,
который подключается отдельным MCP-сервером.

Справочник личных данных (`get_reference`) лежит вне репозитория и вне
`brain/`: он не версионируется и не попадает в контекст сессии сам по себе.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("brain")

# Разрешённые источники записи. Каждая запись обязана назвать, откуда взялась,
# иначе через полгода невозможно понять, чему верить.
SOURCES = ("встреча", "чат", "сессия", "ручной ввод")

MAX_RESULTS = 40
SNIPPET_LINES = 3

# Справочник личных данных: отдельный файл за пределами репозитория.
# Умышленно не `brain/`: туда он попал бы и в git-историю, и в контекст каждой
# сессии через whoami. Из истории git значение уже не вычистить.
DEFAULT_REFERENCE = Path.home() / ".brain-private" / "reference.md"
MAX_REFERENCE_ENTRIES = 5

# Журнал записей во внешние системы. Лежит рядом с базой, но не в ней:
# это не знание, а след действий, и читается он глазами при разборе «кто это
# написал». Коммитится вместе со всем остальным.
AUDIT_DIR_NAME = "audit"

# Личные задачи лежат рядом с базой знаний, но не внутри неё: в brain/ их нашли
# бы search_memory и whoami, а личному в рабочих ответах делать нечего.
# Доступ только через свои инструменты.
PERSONAL_TASKS_NAME = "personal/tasks.md"
WRITE_KINDS = ("комментарий", "задача")


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


def resolve_card(name: str, folder: str) -> Path:
    """Путь к карточке с проверкой, что он не вышел за пределы базы.

    Имя приходит из ответа модели, то есть косвенно — из текста, который мы
    не контролируем. Без этой проверки `../../.ssh/id_rsa` стал бы читаемым.
    """
    root = (brain_dir() / folder).resolve()
    candidate = (root / f"{slugify(name)}.md").resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Недопустимое имя карточки: {name!r}")
    return candidate


def resolve_project_file(name: str) -> Path:
    return resolve_card(name, "projects")


def known_cards(folder: str) -> list[str]:
    root = brain_dir() / folder
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.md") if not p.stem.startswith("_"))


def known_projects() -> list[str]:
    return known_cards("projects")


def read_if_exists(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None


def reference_file() -> Path:
    """Файл справочника: переменная BRAIN_REFERENCE либо ~/.brain-private/reference.md."""
    env = os.environ.get("BRAIN_REFERENCE")
    path = Path(env).expanduser() if env else DEFAULT_REFERENCE
    return path.resolve()


def reference_entries() -> list[tuple[str, str]]:
    """Справочник как пары «заголовок, тело». Разделы задаются строками `## `."""
    path = reference_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return []

    entries: list[tuple[str, str]] = []
    title, body = None, []
    for line in raw.splitlines():
        if line.startswith("## "):
            if title is not None:
                entries.append((title, "\n".join(body).strip()))
            title, body = line[3:].strip(), []
        elif title is not None:
            body.append(line)
    if title is not None:
        entries.append((title, "\n".join(body).strip()))
    return entries


def personal_tasks_file() -> Path:
    return brain_dir().parent / PERSONAL_TASKS_NAME


def parse_tasks(raw: str) -> tuple[list[dict[str, str]], list[str]]:
    """Строки-задачи и всё остальное содержимое файла.

    Задача — строка вида `- [ ] 2026-10-05 · текст`. Дата необязательна.
    """
    open_tasks: list[dict[str, str]] = []
    done: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ] "):
            body = stripped[6:].strip()
            due, _, rest = body.partition(" · ")
            if len(due) == 10 and due.count("-") == 2:
                open_tasks.append({"due": due, "text": rest.strip()})
            else:
                open_tasks.append({"due": "", "text": body})
        elif stripped.startswith("- [x] "):
            done.append(stripped[6:].strip())
    return open_tasks, done


def render_tasks(open_tasks: list[dict[str, str]], done: list[str]) -> str:
    lines = ["# Личные задачи", "", "Не по работе. Рабочие — в Execution Hub.", "", "## Открытые", ""]
    # Сначала со сроком по возрастанию, бессрочные в конец: дата сортируется
    # как строка, потому что формат фиксированный.
    for task in sorted(open_tasks, key=lambda x: x["due"] or "9999-99-99"):
        prefix = f"{task['due']} · " if task["due"] else ""
        lines.append(f"- [ ] {prefix}{task['text']}")
    lines += ["", "## Сделано", ""]
    lines += [f"- [x] {d}" for d in done]
    return "\n".join(lines) + "\n"


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

    products = known_cards("products")
    if products:
        parts.append("## Карточки функционала продуктов\n\n"
                     + "\n".join(f"- {p}" for p in products)
                     + "\n\nЧитать через get_product, когда спрашивают, умеет ли продукт что-то.")

    return "\n\n---\n\n".join(parts)


@mcp.tool()
def list_projects() -> str:
    """Имена карточек проектов и карточек функционала продуктов."""
    projects = known_projects()
    products = known_cards("products")
    blocks = []
    if projects:
        blocks.append("Проекты (get_project) — решения, люди, зависимости:\n"
                      + "\n".join(f"- {p}" for p in projects))
    if products:
        blocks.append("Продукты (get_product) — что умеет, в каком состоянии:\n"
                      + "\n".join(f"- {p}" for p in products))
    return "\n\n".join(blocks) if blocks else "Карточек пока нет."


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
def get_product(name: str) -> str:
    """Функционал продукта: что он умеет, в каком состоянии, с какой версии.

    Читать, когда спрашивают «есть ли у нас X», готовят ответ заказчику или
    разбирают требования закупки. Статусы: есть, частично, в работе, план, нет,
    уточнить.

    `уточнить` означает «не подтверждено», а не «нет». Не выдавай такую строку
    за отсутствие возможности — скажи, что статус не подтверждён.
    """
    path = resolve_card(name, "products")
    content = read_if_exists(path)
    if content is None:
        available = known_cards("products")
        hint = "\n".join(f"- {p}" for p in available) if available else "(пусто)"
        return f"Карточки функционала {name!r} нет. Что есть:\n{hint}"
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
def get_reference(query: str) -> str:
    """Личные справочные данные: серийные номера, лицензии, тарифы, документы.

    Вызывать ТОЛЬКО когда пользователь прямо спрашивает конкретное значение.
    Не вызывать «на всякий случай» и не для общего знакомства с пользователем —
    для этого есть whoami.

    Возвращает только совпавшие разделы, не файл целиком. Найденные значения
    не переноси в базу знаний, не повторяй в итогах сессии и не упоминай
    в последующих ответах без нового вопроса.
    """
    path = reference_file()

    # Справочник внутри репозитория — это ошибка настройки: он уедет в git.
    repo = brain_dir().parent
    if path.is_relative_to(repo):
        return (
            f"Справочник лежит внутри репозитория ({path}) — так он попадёт в git "
            "и останется в истории навсегда. Перенеси его наружу, например "
            "в ~/.brain-private/reference.md, или задай путь в BRAIN_REFERENCE."
        )

    entries = reference_entries()
    if not entries:
        return (
            f"Справочник пуст или отсутствует: {path}. "
            "Создай файл, разделы задаются строками вида `## Ноутбук`."
        )

    needle = query.strip().lower()
    if not needle:
        titles = "\n".join(f"- {title}" for title, _ in entries)
        return f"Что именно нужно? Разделы справочника:\n{titles}"

    hits = [(ti, bo) for ti, bo in entries if needle in ti.lower() or needle in bo.lower()]
    if not hits:
        titles = "\n".join(f"- {title}" for title, _ in entries)
        return f"По запросу {query!r} ничего нет. Разделы справочника:\n{titles}"

    if len(hits) > MAX_REFERENCE_ENTRIES:
        titles = "\n".join(f"- {title}" for title, _ in hits)
        return f"Слишком общий запрос, подошло {len(hits)} разделов:\n{titles}"

    return "\n\n".join(f"## {title}\n{body}" for title, body in hits)


@mcp.tool()
def personal_tasks(include_done: bool = False) -> str:
    """Личные задачи пользователя — не по работе.

    Рабочие задачи здесь не хранятся, они в Execution Hub. Не смешивай
    эти списки в одном ответе без просьбы.
    """
    raw = read_if_exists(personal_tasks_file())
    if raw is None:
        return "Личных задач пока нет."

    open_tasks, done = parse_tasks(raw)
    if not open_tasks and not done:
        return "Личных задач пока нет."

    today = f"{date.today():%Y-%m-%d}"
    lines = []
    for task in sorted(open_tasks, key=lambda x: x["due"] or "9999-99-99"):
        if not task["due"]:
            lines.append(f"- {task['text']}")
        elif task["due"] < today:
            lines.append(f"- {task['text']} — просрочено, срок был {task['due']}")
        elif task["due"] == today:
            lines.append(f"- {task['text']} — сегодня")
        else:
            lines.append(f"- {task['text']} — {task['due']}")

    out = "Открытые:\n" + ("\n".join(lines) if lines else "(пусто)")
    if include_done and done:
        out += "\n\nСделано:\n" + "\n".join(f"- {d}" for d in done)
    return out


@mcp.tool()
def add_personal_task(text: str, due: str = "") -> str:
    """Добавить личную задачу.

    text: что сделать, своими словами.
    due: срок в виде ГГГГ-ММ-ДД, если назван. Не выдумывай его — нет срока,
    оставь пустым.

    Это не напоминание: задача лежит в списке и ждёт, пока её не спросят.
    Чтобы пришло уведомление в нужное время, нужен отдельный будильник.
    """
    if not text.strip():
        return "Пустую задачу не добавлю."
    if due:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            return f"Срок должен быть в виде ГГГГ-ММ-ДД. Получено: {due!r}"

    path = personal_tasks_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = read_if_exists(path) or ""
    open_tasks, done = parse_tasks(raw)
    open_tasks.append({"due": due, "text": text.strip()})
    path.write_text(render_tasks(open_tasks, done), encoding="utf-8")

    return f"Добавлено: {text.strip()}" + (f" (срок {due})" if due else "")


@mcp.tool()
def close_personal_task(text: str) -> str:
    """Закрыть личную задачу. Достаточно части текста, если она узнаётся однозначно."""
    path = personal_tasks_file()
    raw = read_if_exists(path)
    if raw is None:
        return "Личных задач пока нет."

    open_tasks, done = parse_tasks(raw)
    needle = text.strip().lower()
    hits = [t for t in open_tasks if needle in t["text"].lower()]

    if not hits:
        return f"Не нашёл задачу по {text!r}."
    if len(hits) > 1:
        listed = "\n".join(f"- {t['text']}" for t in hits)
        return f"Подходит несколько, уточни какая:\n{listed}"

    task = hits[0]
    open_tasks.remove(task)
    closed = f"{date.today():%Y-%m-%d} · {task['text']}"
    path.write_text(render_tasks(open_tasks, [closed] + done), encoding="utf-8")
    return f"Закрыто: {task['text']}"


@mcp.tool()
def log_action(kind: str, target: str, text: str, channel: str = "телеграм") -> str:
    """Записать в журнал то, что было отправлено во внешнюю систему.

    Вызывать СРАЗУ ПОСЛЕ успешной записи в Execution Hub — комментария к задаче
    или созданной задачи. Не вместо записи и не до неё.

    kind: "комментарий" или "задача".
    target: задача или проект, к которым это относится — с названием, не только id.
    text: что было отправлено, дословно.
    channel: откуда пришло действие.

    Журнал нужен, чтобы человек мог потом увидеть всё, что ушло наружу с его
    имени, и проверить. Не приукрашивай текст и не сокращай его.
    """
    if kind not in WRITE_KINDS:
        return f"kind должен быть одним из: {', '.join(WRITE_KINDS)}. Получено: {kind!r}"
    if not target.strip() or not text.strip():
        return "Нужны и цель, и отправленный текст."

    now = datetime.now()
    path = brain_dir().parent / AUDIT_DIR_NAME / f"{now:%Y-%m}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            f"# Записи во внешние системы · {now:%Y-%m}\n\n"
            "Что ушло наружу и откуда. Пополняется автоматически.\n",
            encoding="utf-8",
        )

    entry = (
        f"\n## {now:%Y-%m-%d %H:%M} · {kind}\n\n"
        f"**Куда:** {target.strip()}  \n"
        f"**Откуда:** {channel.strip()}\n\n"
        f"> {text.strip().replace(chr(10), chr(10) + '> ')}\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(entry)

    return f"Записано в {path.relative_to(brain_dir().parent)}."


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
