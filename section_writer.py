from budget import TaskBudget


SECTION_SYSTEM_PROMPT = (
    "Ты генерируешь HTML-фрагменты для веб-страниц. "
    "На вход получаешь название и описание секции. "
    "Возвращаешь ТОЛЬКО HTML-фрагмент.\n"
    "\n"
    "Правила:\n"
    "1. Язык контента — РУССКИЙ. Все заголовки и текст "
    "на русском, если не сказано иначе.\n"
    "2. Никаких <html>, <head>, <body>, <!DOCTYPE>.\n"
    "3. НЕ оборачивай ответ в markdown-код (```html). "
    "Просто чистый HTML.\n"
    "4. Используй <section> для группировки, "
    "<article> для единиц контента, <h2> для заголовка "
    "секции, <h3> для подзаголовков.\n"
    "5. Текст внутри <article> оборачивай в <p>.\n"
    "6. Не больше 15 строк HTML.\n"
    "7. Не добавляй <style> и <script>.\n"
    "8. Если просят N элементов — сделай ровно N.\n"
    "9. Не пиши пояснений — только HTML-фрагмент.\n"
)

def _strip_code_fence(text: str) -> str:
    """Снимает markdown-обёртку ```html ... ``` или ``` ... ```."""
    text = text.strip()

    if text.startswith("```"):
        # Убираем первую строку с ``` или ```html.
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]

        # Убираем закрывающие ```.
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    return text.strip()

def generate_section(
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

    response = client.chat.completions.create(
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

    # Снимаем markdown-обёртку, если модель её добавила.
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

    return {
        "status": "ok",
        "html": text,
        "error": None,
        "tokens": tokens,
    }