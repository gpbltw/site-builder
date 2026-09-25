import json
import re

from budget import TaskBudget


PLANNER_SYSTEM_PROMPT = (
    "Ты планировщик правок сайта. Пользователь даёт задачу "
    "на русском. Ты возвращаешь JSON-план операций.\n"
    "\n"
    "Доступные операции:\n"
    "- insert_section: добавить HTML-секцию в конец <main> "
    "файла. Только для .html. Параметры: file, description.\n"
    "- append_file: добавить блок в конец файла без "
    "указания old. Используй для «добавь правило в конец "
    "CSS», «допиши в конец файла». Параметры: file, "
    "description.\n"
    "- edit_file: точечная правка. Параметры: file, "
    "instruction (что изменить).\n"
    "- write_file: создать новый файл. Параметры: file, "
    "description.\n"
    "\n"
    "Правила:\n"
    "1. Разбивай задачу на 1-4 операции. Каждая операция — "
    "одна логическая правка.\n"
    "2. insert_section работает ТОЛЬКО с HTML-файлами. "
    "Он вставляет фрагмент перед </main> или </body>.\n"
    "2a. Для CSS-файлов НИКОГДА не используй "
    "insert_section. Чтобы добавить правило в конец "
    "CSS — используй append_file. Чтобы добавить правило "
    "в середину CSS — edit_file.\n"
    "2b. Если задача — 'добавить в конец файла', "
    "используй append_file, а не edit_file. "
    "append_file не требует old и не может промахнуться.\n"
    "2c. КРИТИЧНО: если задача про ОБЩИЙ UI-элемент "
    "(кнопка переключения темы, навигация, footer, общий "
    "скрипт, header) — применяй его ко ВСЕМ HTML-страницам "
    "проекта. Посмотри snapshot: если в проекте больше "
    "одного .html — добавь операцию для каждой страницы.\n"
    "3. instruction для edit_file должна быть ЯВНОЙ и "
    "ПОЛНОЙ. Не сужай задачу: если пользователь сказал "
    "'переведи блок' — инструкция должна быть 'перевести "
    "ВЕСЬ текст в секции X на русский', а не 'перевести "
    "заголовок'. Указывай: что найти, где именно, что "
    "сделать.\n"
    "4. description для insert_section и append_file — "
    "ОПИСАНИЕ содержимого на русском, не HTML. Например: "
    "'три карточки преимуществ: h2 + 3 article с h3 и p'.\n"
    "5. Не смешивай файлы в одной операции. Одна "
    "операция — один файл.\n"
    "6. Возвращай ТОЛЬКО JSON без markdown.\n"
    "\n"
    "Примеры:\n"
    "\n"
    "Задача: 'Добавь переключение темы на сайте'\n"
    "План (для сайта из index.html и about.html):\n"
    "{\n"
    '  "operations": [\n'
    '    {"op": "append_file", "file": "css/style.css", '
    '"description": "блок :root.light с переменными '
    'светлой темы, стили .theme-toggle"},\n'
    '    {"op": "edit_file", "file": "index.html", '
    '"instruction": "Добавить кнопку темы и скрипт '
    'переключения перед </body>"},\n'
    '    {"op": "edit_file", "file": "about.html", '
    '"instruction": "Добавить кнопку темы и скрипт '
    'переключения перед </body>"}\n'
    "  ]\n"
    "}\n"
    "\n"
    "Задача: 'Переведи блок how-it-works на русский'\n"
    "{\n"
    '  "operations": [\n'
    '    {"op": "edit_file", "file": "index.html", '
    '"instruction": "Перевести на русский ВЕСЬ текст '
    'внутри <section class=\\"how-it-works\\">: заголовок '
    'h2 и оба параграфа p."}\n'
    "  ]\n"
    "}\n"
)


VALID_OPS = {
    "insert_section", "append_file", "edit_file", "write_file",
}
MAX_OPERATIONS = 4


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _extract_json(text: str) -> dict | None:
    """Ищет JSON в тексте — модель иногда оборачивает."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _validate_plan(data: dict) -> list[dict]:
    """Проверяет план и возвращает список валидных операций."""
    ops = data.get("operations", [])
    if not isinstance(ops, list):
        return []

    valid = []
    for item in ops:
        if not isinstance(item, dict):
            continue

        op = item.get("op")
        f = item.get("file")

        if op not in VALID_OPS:
            continue
        if not isinstance(f, str) or not f.strip():
            continue

        if op == "insert_section":
            if not item.get("description"):
                continue
            if not f.endswith((".html", ".htm")):
                # Ранняя отбраковка: insert_section только для HTML.
                continue

        if op == "append_file":
            if not item.get("description"):
                continue

        if op == "edit_file":
            if not item.get("instruction"):
                continue

        if op == "write_file":
            if not item.get("description"):
                continue

        valid.append(item)

    return valid[:MAX_OPERATIONS]


async def plan_edit(
    client,
    model: str,
    task: str,
    project_snapshot: str,
    budget: TaskBudget,
    max_completion_tokens: int = 500,
) -> dict:
    """Планирует список операций над сайтом.

    Возвращает:
      {
        "status": "ok" | "empty" | "failed" | "limit_reached",
        "operations": [...],
        "raw": str,
        "error": str | None,
      }
    """
    if not budget.try_consume_model_call():
        return {
            "status": "limit_reached",
            "operations": [],
            "raw": "",
            "error": f"Бюджет исчерпан. {budget.snapshot()}",
        }

    user_prompt = (
        f"ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:\n{task}\n\n"
        f"СТРУКТУРА ПРОЕКТА:\n{project_snapshot}\n\n"
        f"Составь JSON-план операций. Помни: если задача "
        f"про ОБЩИЙ UI — примени его ко ВСЕМ HTML-страницам "
        f"проекта. Не больше {MAX_OPERATIONS} операций."
    )

    print(f"\n[Planner] Составляю план")

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_completion_tokens=max_completion_tokens,
    )

    if response.usage:
        u = response.usage
        print(
            f"[Planner] токены: вход={u.prompt_tokens}, "
            f"генерация={u.completion_tokens}"
        )

    choice = response.choices[0]
    text = _strip_fence(choice.message.content or "")

    if choice.finish_reason == "length":
        return {
            "status": "failed",
            "operations": [],
            "raw": text,
            "error": "План обрезан лимитом генерации",
        }

    data = _extract_json(text)
    if data is None:
        print(f"[Planner] не смог распарсить JSON: {text[:200]}")
        return {
            "status": "failed",
            "operations": [],
            "raw": text,
            "error": "could not parse JSON plan",
        }

    # Отличаем "нечего делать" от "не смог распарсить".
    raw_ops = data.get("operations", [])
    if isinstance(raw_ops, list) and len(raw_ops) == 0:
        print(
            "[Planner] план: 0 операций "
            "(модель решила, что менять нечего)"
        )
        return {
            "status": "ok",
            "operations": [],
            "raw": text,
            "error": None,
        }

    ops = _validate_plan(data)
    if not ops:
        print("[Planner] невалидные операции в плане")
        return {
            "status": "failed",
            "operations": [],
            "raw": text,
            "error": "no valid operations after validation",
        }

    print(f"[Planner] план: {len(ops)} операций")
    for op in ops:
        preview = op.get("instruction") or op.get("description", "")
        preview = preview[:70]
        print(
            f"[Planner]   {op['op']} → {op['file']} — {preview}"
        )

    return {
        "status": "ok",
        "operations": ops,
        "raw": text,
        "error": None,
    }