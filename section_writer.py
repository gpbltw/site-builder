import re

from budget import TaskBudget


SECTION_SYSTEM_PROMPT = (
    "Ты генерируешь HTML-фрагменты для русскоязычного сайта. "
    "Все заголовки и текст — только на русском языке.\n"
    "\n"
    "ПРИМЕР правильного ответа:\n"
    "<section>\n"
    "  <h2>Преимущества</h2>\n"
    "  <article><h3>Скорость</h3>"
    "<p>Система обрабатывает задачи за секунды.</p></article>\n"
    "  <article><h3>Надёжность</h3>"
    "<p>Результат воспроизводим при каждом запуске.</p></article>\n"
    "</section>\n"
    "\n"
    "ПРАВИЛА:\n"
    "1. Весь текст внутри тегов — на русском. "
    "НИКАКИХ английских, испанских или других языков.\n"
    "2. Никаких <html>, <head>, <body>, <!DOCTYPE>.\n"
    "3. НЕ оборачивай ответ в ```html — только чистый HTML.\n"
    "4. Используй <section>, <article>, <h2>, <h3>, <p>.\n"
    "5. Не больше 15 строк. Без <style> и <script>.\n"
    "6. Если просят N элементов — сделай ровно N.\n"
    "7. Только HTML, без пояснений.\n"
)


_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_SPANISH_HINTS = re.compile(
    r"\b(como|funciona|nuestros|agentes|aprenden?|"
    r"resultados|automática|precisión|tiempo|real)\b",
    re.IGNORECASE,
)


def _is_mostly_russian(text: str) -> bool:
    """Проверяет, что текст преимущественно на русском.

    Эвристика: кириллических букв должно быть больше, чем
    латинских, и хотя бы 5 штук. Испанские маркеры —
    отдельный сигнал.
    """
    cyr = len(_CYRILLIC_RE.findall(text))
    lat = len(_LATIN_RE.findall(text))

    if cyr < 5:
        return False
    if lat > cyr:
        return False
    if _SPANISH_HINTS.search(text):
        return False
    return True


def _strip_code_fence(text: str) -> str:
    """Снимает markdown-обёртку ```html ... ``` или ``` ... ```."""
    text = text.strip()

    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]

        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    return text.strip()


async def generate_section(
    client,
    model: str,
    section_name: str,
    description: str,
    budget: TaskBudget,
    max_completion_tokens: int = 500,
) -> dict:
    """Генерирует HTML-фрагмент для одной секции.

    Возвращает:
      {
        "status": "ok" | "limit_reached" | "failed" | "empty",
        "html": str,
        "error": str | None,
        "tokens": {...},
      }
    """
    if not budget.try_consume_model_call():
        return {
            "status": "limit_reached",
            "html": "",
            "error": f"Бюджет исчерпан. {budget.snapshot()}",
            "tokens": {},
        }

    user_prompt = (
        f"Секция: {section_name}\n"
        f"Описание: {description}\n\n"
        f"Сгенерируй HTML-фрагмент для этой секции."
    )

    print(f"\n[SectionGen] {section_name}")

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SECTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_completion_tokens=max_completion_tokens,
    )

    tokens = {}
    if response.usage:
        tokens = {
            "input": response.usage.prompt_tokens,
            "output": response.usage.completion_tokens,
        }
        print(
            f"[SectionGen] токены: вход={tokens['input']}, "
            f"генерация={tokens['output']}"
        )

    choice = response.choices[0]
    text = (choice.message.content or "").strip()

    if choice.finish_reason == "length":
        return {
            "status": "limit_reached",
            "html": text,
            "error": "Секция обрезана лимитом генерации",
            "tokens": tokens,
        }

    if not text:
        return {
            "status": "empty",
            "html": "",
            "error": "Модель вернула пустой текст",
            "tokens": tokens,
        }

    # Снимаем markdown-обёртку.
    text = _strip_code_fence(text)

    if not text:
        return {
            "status": "empty",
            "html": "",
            "error": "После снятия markdown-обёртки пусто",
            "tokens": tokens,
        }

    # Модель могла вернуть целый HTML вместо фрагмента.
    lowered = text.lower()
    if "<!doctype" in lowered or "<html" in lowered:
        return {
            "status": "failed",
            "html": "",
            "error": (
                "Модель вернула целый HTML вместо фрагмента. "
                "Ожидался только <section>."
            ),
            "tokens": tokens,
        }

    # Проверка языка. Если не русский — retry.
    if not _is_mostly_russian(text):
        print(f"[SectionGen] язык не русский — пробую retry")

        if not budget.try_consume_model_call():
            return {
                "status": "limit_reached",
                "html": text,
                "error": (
                    "Нет бюджета на retry по языку. "
                    f"{budget.snapshot()}"
                ),
                "tokens": tokens,
            }

        retry_response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты генерируешь HTML-фрагмент. "
                        "Весь текст на РУССКОМ языке. "
                        "Только <section>, <article>, "
                        "<h2>, <h3>, <p>. Без ```html "
                        "и без пояснений."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Секция: {section_name}\n"
                        f"Описание: {description}\n\n"
                        f"Верни ТОЛЬКО HTML, ВЕСЬ ТЕКСТ "
                        f"НА РУССКОМ ЯЗЫКЕ."
                    ),
                },
            ],
            max_completion_tokens=max_completion_tokens,
        )

        retry_choice = retry_response.choices[0]
        retry_text = (
            retry_choice.message.content or ""
        ).strip()
        retry_text = _strip_code_fence(retry_text)

        if retry_response.usage:
            tokens["retry_input"] = (
                retry_response.usage.prompt_tokens
            )
            tokens["retry_output"] = (
                retry_response.usage.completion_tokens
            )

        if retry_text and _is_mostly_russian(retry_text):
            print(f"[SectionGen] retry успешен")
            text = retry_text
        else:
            print(
                f"[SectionGen] retry тоже не русский, "
                f"оставляю как есть"
            )

    return {
        "status": "ok",
        "html": text,
        "error": None,
        "tokens": tokens,
    }