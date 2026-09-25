import re
from pathlib import Path

from budget import TaskBudget


CONTENT_CHECK_SYSTEM_PROMPT = (
    "Ты QA-ревьюер. Тебе дают задачу и содержимое всех файлов "
    "сгенерированного сайта. Проверь, что результат "
    "ПОЛНОСТЬЮ соответствует задаче.\n"
    "\n"
    "Проверяй конкретно:\n"
    "1. Все ли запрошенные файлы созданы?\n"
    "2. На каждой HTML-странице есть запрошенные элементы "
    "(h1, абзац, ссылки, навигация)?\n"
    "3. Все ли ссылки на страницы ведут на существующие файлы?\n"
    "4. Учтены ли требования к стилям (тёмный фон, светлый "
    "текст, центрирование и т.п.)?\n"
    "\n"
    "Отвечай СТРОГО в формате:\n"
    "STATUS: OK — если всё выполнено полностью.\n"
    "STATUS: INCOMPLETE — если чего-то не хватает.\n"
    "\n"
    "Если INCOMPLETE, добавь секцию MISSING с "
    "нумерованным списком недостающего. Каждый пункт — "
    "конкретное требование из задачи, которое не выполнено.\n"
    "Формат пункта: '1. В файле X отсутствует Y'.\n"
    "\n"
    "Не пиши советы и улучшения. Только то, что было "
    "явно запрошено и отсутствует. Если всё на месте — "
    "просто 'STATUS: OK' без MISSING."
    "Важно про CSS и HTML:\n"
    "- CSS-правила применяются через селекторы (класс, "
    "тег), а НЕ через имя HTML-файла. Одно правило "
    "'nav.site-nav { ... }' действует на всех страницах, "
    "где есть <nav class=\"site-nav\">.\n"
    "- Не требуй отдельной правки для about.html, если "
    "CSS-правило уже покрывает нужный селектор. "
    "Проверь, что селектор присутствует в CSS — этого "
    "достаточно для всех страниц.\n"
    "- Если в payload есть '... [середина файла пропущена]' "
    "или '[omitted, total limit reached]' — сообщи об "
    "этом в ответе как о проблеме HARNESS, а не как о "
    "проблеме задачи. Не делай вывод 'правил нет', "
    "если ты их просто не видишь.\n"
)


MAX_FILE_CHARS = 6000
MAX_TOTAL_CHARS = 24000


def _collect_files(output_dir: Path) -> str:
    """Собирает содержимое всех файлов проекта.

    Обрезает большие файлы и общий объём — иначе prompt
    может не влезть в контекст модели.
    """
    parts: list[str] = []
    total = 0

    for item in sorted(output_dir.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(output_dir).as_posix()

        try:
            text = item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            parts.append(f"=== {rel} === [binary, skipped]")
            continue

        if len(text) > MAX_FILE_CHARS:
            # Показываем начало И конец — иначе checker
            # не видит поздние правила и даёт ложные выводы.
            head = MAX_FILE_CHARS // 2
            tail = MAX_FILE_CHARS - head - 60
            text = (
                text[:head]
                + "\n\n... [середина файла пропущена] ...\n\n"
                + text[-tail:]
            )

        chunk = f"=== {rel} ===\n{text}"
        if total + len(chunk) > MAX_TOTAL_CHARS:
            parts.append(
                f"=== {rel} === [omitted, total limit reached]"
            )
            break

        parts.append(chunk)
        total += len(chunk)

    return "\n\n".join(parts)


def _parse_issues(text: str) -> list[str]:
    """Извлекает список проблем из секции MISSING."""
    issues: list[str] = []
    in_missing = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.upper().startswith("MISSING"):
            in_missing = True
            continue

        if not in_missing:
            continue

        match = re.match(
            r"^(\d+[.)]|[-*])\s*(.+)$", stripped,
        )
        if match:
            issues.append(match.group(2).strip())
        elif stripped and not stripped.startswith("STATUS:"):
            # Строка без маркера — присоединим к последней проблеме.
            if issues:
                issues[-1] = issues[-1] + " " + stripped

    return issues


async def check_content(
    client,
    model: str,
    task: str,
    output_dir: str | Path,
    budget: TaskBudget,
    max_completion_tokens: int = 600,
) -> dict:
    """Проверяет полноту сгенерированного сайта."""
    output_dir = Path(output_dir).resolve()

    if not budget.try_consume_model_call():
        return {
            "status": "error",
            "issues": [],
            "raw": "",
            "error": (
                "Бюджет исчерпан до контент-чека. "
                f"Состояние: {budget.snapshot()}."
            ),
        }

    files_payload = _collect_files(output_dir)

    if not files_payload:
        return {
            "status": "incomplete",
            "issues": ["Ни одного файла не создано."],
            "raw": "",
            "error": None,
        }

    payload = (
        f"ЗАДАЧА:\n{task}\n\n"
        f"ФАЙЛЫ ПРОЕКТА:\n\n{files_payload}"
    )

    messages = [
        {"role": "system", "content": CONTENT_CHECK_SYSTEM_PROMPT},
        {"role": "user", "content": payload},
    ]

    print(
        f"\n[ContentCheck] Проверяю полноту "
        f"(вызов {budget.used_model_calls}/{budget.max_model_calls})"
    )

    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_completion_tokens=max_completion_tokens,
    )

    if response.usage:
        u = response.usage
        print(
            f"[ContentCheck] Токены: вход={u.prompt_tokens}, "
            f"генерация={u.completion_tokens}"
        )

    choice = response.choices[0]
    text = choice.message.content or ""

    if choice.finish_reason == "length":
        return {
            "status": "unparsed",
            "issues": [],
            "raw": text,
            "error": (
                "Ответ контент-чека обрезан лимитом генерации."
            ),
        }

    upper = text.upper()

    if "STATUS: OK" in upper:
        print("[ContentCheck] STATUS: OK")
        return {"status": "ok", "issues": [], "raw": text, "error": None}

    if "STATUS: INCOMPLETE" in upper:
        issues = _parse_issues(text)
        print(
            f"[ContentCheck] STATUS: INCOMPLETE, "
            f"{len(issues)} проблем"
        )
        for issue in issues:
            print(f"[ContentCheck]   - {issue}")
        return {
            "status": "incomplete",
            "issues": issues,
            "raw": text,
            "error": None,
        }

    print("[ContentCheck] Не удалось распарсить STATUS")
    return {
        "status": "unparsed",
        "issues": [],
        "raw": text,
        "error": "could not parse STATUS from response",
    }