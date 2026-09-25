from types import SimpleNamespace

from budget import TaskBudget
from section_writer import generate_section


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
    def __init__(self, response):
        self.response = response
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=self._create,
            ),
        )

    def _create(self, **kwargs):
        return self.response


def test_generate_section_ok():
    client = FakeClient(make_response(
        '<section><h2>Features</h2>'
        '<article><h3>A</h3></article>'
        '<article><h3>B</h3></article></section>'
    ))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = generate_section(
        client, "fake-model", "features", "3 cards", budget,
    )

    assert result["status"] == "ok"
    assert "<section>" in result["html"]


def test_generate_section_rejects_full_html():
    client = FakeClient(make_response(
        '<!DOCTYPE html><html><body><h1>X</h1></body></html>'
    ))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = generate_section(
        client, "fake-model", "hero", "intro", budget,
    )

    assert result["status"] == "failed"
    assert "целый HTML" in result["error"]


def test_generate_section_empty():
    client = FakeClient(make_response(""))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = generate_section(
        client, "fake-model", "x", "y", budget,
    )

    assert result["status"] == "empty"


def test_generate_section_budget_exhausted():
    client = FakeClient(make_response("<section></section>"))
    budget = TaskBudget(max_model_calls=1, max_tool_calls=5)

    # Израсходовать бюджет до вызова.
    assert budget.try_consume_model_call() is True

    result = generate_section(
        client, "fake-model", "x", "y", budget,
    )

    assert result["status"] == "limit_reached"

def test_generate_section_strips_code_fence():
    client = FakeClient(make_response(
        "```html\n"
        '<section><h2>X</h2><p>Y</p></section>'
        "\n```"
    ))
    budget = TaskBudget(max_model_calls=5, max_tool_calls=5)

    result = generate_section(
        client, "fake-model", "x", "y", budget,
    )

    assert result["status"] == "ok"
    assert "```" not in result["html"]
    assert result["html"].startswith("<section>")