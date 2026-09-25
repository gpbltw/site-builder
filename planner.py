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
    "2. insert_section работает ТОЛЬКО с HTML-файлами "
    "(.html, .htm). Он вставляет фрагмент перед </main> "
    "или </body>.\n"
    "2a. Для CSS-файлов (.css) НИКОГДА не используй "
    "insert_section. Чтобы добавить правило в конец "
    "CSS-файла — используй edit_file с instruction "
    "'добавить в конец файла правило ...'.\n"
    "2b. Если задача — 'добавить в конец файла', "
    "используй append_file, а не edit_file. "
    "edit_file для этого ненадёжен — он требует "
    "точного old-фрагмента, который модель может "
    "указать неточно.\n"
    "3. instruction для edit_file должна быть ЯВНОЙ и "
    "ПОЛНОЙ. Не сужай задачу: если пользователь сказал "
    "'переведи блок' — инструкция должна быть 'перевести "
    "ВЕСЬ текст в секции X на русский', а не 'перевести "
    "заголовок'. Указывай: что найти, где именно, что "
    "сделать.\n"
    "4. description для insert_section — ОПИСАНИЕ "
    "содержимого на русском, не HTML. Например: 'три "
    "карточки преимуществ: h2 + 3 article с h3 и p'.\n"
    "5. Не смешивай файлы в одной операции.\n"
    "6. Возвращай ТОЛЬКО JSON без markdown.\n"
    "\n"
    "Формат:\n"
    "{\n"
    '  "operations": [\n'
    '    {"op": "edit_file", "file": "index.html", '
    '"instruction": "Перевести на русский ВЕСЬ текст '
    'внутри <section class=\\"how-it-works\\">: заголовок '
    'h2 и оба параграфа p. Сохранить структуру тегов."}\n'
    "  ]\n"
    "}"
)


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

    # Ищем первую { ... последнюю }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


VALID_OPS = {"insert_section", "append_file", "edit_file", "write_file"}


def _validate_plan(data: dict) -> list[dict]:
    ops = data.get("operations", [])
    if not isinstance(ops, list):
        return []

    valid = []
    for item in ops:
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        f = item.get("file")
        if op not in VALID_OPS or not isinstance(f, str):
            continue
        if op == "insert_section":
            if not item.get("description"):
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

    return valid[:4]  # жёсткий лимит


def plan_edit(
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
        f"Составь JSON-план операций."
    )

    print(f"\n[Planner] Составляю план")

    response = client.chat.completions.create(
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

    ops = _validate_plan(data)
    if not ops:
        print(f"[Planner] план пустой или невалидный")
        return {
            "status": "empty",
            "operations": [],
            "raw": text,
            "error": "no valid operations in plan",
        }

    print(f"[Planner] план: {len(ops)} операций")
    for op in ops:
        print(
            f"[Planner]   {op['op']} → {op['file']}"
        )

    return {
        "status": "ok",
        "operations": ops,
        "raw": text,
        "error": None,
    }