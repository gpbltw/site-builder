import pytest
from types import SimpleNamespace

from budget import TaskBudget
from section_writer import (
    _is_mostly_russian,
    _strip_code_fence,
    generate_section,
)


def make_response(content, finish_reason="stop"):
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=50, completion_tokens=100,
        ),
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content),
        )],
    )


class FakeClient:
    """Fake async-клиент.

    Если передать список responses — возвращает их
    по очереди (для retry-сценариев). Если один —
    всегда его.
    """

    def __init__(self, response_or_responses):
        if isinstance(response_or_responses, list):
            self.responses = list(response_or_responses)
        else:
            self.responses = [response_or_responses]

        self.requests = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError(
                "FakeClient: больше нет responses"
            )
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


# --- generate_section: базовые сценарии ---


@pytest.mark.asyncio
async def test_generate_section_ok():
    client = FakeClient(make_response(
        '<section><h2>Преимущества</h2>'
        '<article><h3>Скорость</h3>'
        '<p>Система обрабатывает задачи за секунды.</p>'
        '</article></section>'
    ))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = await generate_section(
        client, "fake-model", "features", "3 cards", budget,
    )

    assert result["status"] == "ok"
    assert "<section>" in result["html"]
    assert "Преимущества" in result["html"]


@pytest.mark.asyncio
async def test_generate_section_rejects_full_html():
    client = FakeClient(make_response(
        '<!DOCTYPE html><html><body><h1>X</h1></body></html>'
    ))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = await generate_section(
        client, "fake-model", "hero", "intro", budget,
    )

    assert result["status"] == "failed"
    assert "целый HTML" in result["error"]


@pytest.mark.asyncio
async def test_generate_section_empty():
    client = FakeClient(make_response(""))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = await generate_section(
        client, "fake-model", "x", "y", budget,
    )

    assert result["status"] == "empty"


@pytest.mark.asyncio
async def test_generate_section_budget_exhausted():
    client = FakeClient(make_response("<section></section>"))
    budget = TaskBudget(max_model_calls=1, max_tool_calls=5)

    # Израсходовать бюджет до вызова.
    assert budget.try_consume_model_call() is True

    result = await generate_section(
        client, "fake-model", "x", "y", budget,
    )

    assert result["status"] == "limit_reached"


@pytest.mark.asyncio
async def test_generate_section_strips_code_fence():
    client = FakeClient(make_response(
        "```html\n"
        '<section><h2>Заголовок</h2>'
        '<p>Текст на русском языке.</p></section>'
        "\n```"
    ))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = await generate_section(
        client, "fake-model", "x", "y", budget,
    )

    assert result["status"] == "ok"
    assert "```" not in result["html"]
    assert result["html"].startswith("<section>")


@pytest.mark.asyncio
async def test_generate_section_truncated():
    client = FakeClient(make_response(
        '<section><h2>Обрыв',
        finish_reason="length",
    ))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = await generate_section(
        client, "fake-model", "x", "y", budget,
    )

    assert result["status"] == "limit_reached"


# --- retry на не-русский язык ---


@pytest.mark.asyncio
async def test_generate_section_retries_on_english():
    """Модель вернула английский → retry → русский."""
    client = FakeClient([
        make_response(
            '<section><h2>Features</h2>'
            '<p>Autonomous agents collaborate.</p></section>'
        ),
        make_response(
            '<section><h2>Преимущества</h2>'
            '<p>Автономные агенты взаимодействуют.</p></section>'
        ),
    ])
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = await generate_section(
        client, "fake-model", "features", "3 cards", budget,
    )

    assert result["status"] == "ok"
    assert "Преимущества" in result["html"]
    # Два model calls: первый + retry.
    assert budget.used_model_calls == 2


@pytest.mark.asyncio
async def test_generate_section_keeps_first_when_retry_fails():
    """Оба ответа не на русском → остаётся первый."""
    client = FakeClient([
        make_response(
            '<section><h2>Features</h2>'
            '<p>Autonomous agents collaborate.</p></section>'
        ),
        make_response(
            '<section><h2>Funciones</h2>'
            '<p>Los agentes colaboran.</p></section>'
        ),
    ])
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = await generate_section(
        client, "fake-model", "features", "3 cards", budget,
    )

    # Функция не сдаётся, но и не падает —
    # возвращает текст как есть.
    assert result["status"] == "ok"
    assert budget.used_model_calls == 2


@pytest.mark.asyncio
async def test_generate_section_no_retry_when_budget_exhausted():
    """Не-русский, но бюджета на retry нет."""
    client = FakeClient(make_response(
        '<section><h2>Features</h2>'
        '<p>Autonomous agents collaborate.</p></section>'
    ))
    budget = TaskBudget(max_model_calls=1, max_tool_calls=5)

    result = await generate_section(
        client, "fake-model", "features", "3 cards", budget,
    )

    assert result["status"] == "limit_reached"
    assert "retry" in result["error"].lower()


# --- helpers ---


def test_is_mostly_russian():
    assert _is_mostly_russian(
        "<h2>Преимущества</h2><p>Текст.</p>"
    )
    assert not _is_mostly_russian(
        "<h2>Features</h2><p>Autonomous agents.</p>"
    )
    assert not _is_mostly_russian(
        "<h2>Como Funciona</h2><p>Nuestros agentes.</p>"
    )
    assert not _is_mostly_russian("")


def test_is_mostly_russian_short_text():
    """Кириллицы меньше 5 символов — false."""
    assert not _is_mostly_russian("Аа")
    assert not _is_mostly_russian("<p>ОК</p>")


def test_strip_code_fence_html():
    assert _strip_code_fence(
        "```html\n<section>hi</section>\n```"
    ) == "<section>hi</section>"


def test_strip_code_fence_plain():
    assert _strip_code_fence(
        "```\n<section>hi</section>\n```"
    ) == "<section>hi</section>"


def test_strip_code_fence_no_fence():
    assert _strip_code_fence(
        "<section>hi</section>"
    ) == "<section>hi</section>"