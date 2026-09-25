import pytest

from tools import FileTools


HTML_FIXTURE = (
    "<!DOCTYPE html>\n"
    "<html lang=\"ru\">\n"
    "<head>\n"
    "    <meta charset=\"UTF-8\">\n"
    "    <title>Test Page</title>\n"
    "</head>\n"
    "<body>\n"
    "    <h1>Hello World</h1>\n"
    "    <p>Это тестовая страница с достаточным объёмом.</p>\n"
    "</body>\n"
    "</html>\n"
)

CSS_FIXTURE = (
    "* { margin: 0; padding: 0; box-sizing: border-box; }\n"
    "body {\n"
    "    font-family: system-ui, sans-serif;\n"
    "    background: #111;\n"
    "    color: #eee;\n"
    "    line-height: 1.6;\n"
    "}\n"
    "h1 { font-size: 2rem; margin-bottom: 1rem; }\n"
)


def test_write_and_read_roundtrip(tmp_path):
    tools = FileTools(tmp_path)

    result = tools.write_file("index.html", HTML_FIXTURE)
    assert result["status"] == "ok"

    read = tools.read_file("index.html")
    assert read["content"] == HTML_FIXTURE


def test_rejects_parent_traversal(tmp_path):
    tools = FileTools(tmp_path)

    result = tools.write_file("../evil.txt", "x" * 200)
    assert "error" in result
    assert "escapes root" in result["error"]


def test_rejects_absolute_path(tmp_path):
    tools = FileTools(tmp_path)

    result = tools.write_file("/etc/passwd", "x" * 200)
    assert "error" in result


def test_rejects_windows_drive(tmp_path):
    tools = FileTools(tmp_path)

    result = tools.write_file(
        "C:/Windows/system32/x.html", HTML_FIXTURE,
    )
    assert "error" in result


def test_rejects_oversized_content(tmp_path):
    tools = FileTools(tmp_path)

    big = "x" * 60_000
    result = tools.write_file("big.txt", big)
    assert "error" in result
    assert "too large" in result["error"]


def test_rejects_truncated_html(tmp_path):
    """Минимальный размер для .html — защита от обрезков."""
    tools = FileTools(tmp_path)

    result = tools.write_file("index.html", "<html lang=\"")
    assert "error" in result
    assert "too small" in result["error"]


def test_rejects_truncated_css(tmp_path):
    tools = FileTools(tmp_path)

    result = tools.write_file("style.css", "body {")
    assert "error" in result
    assert "too small" in result["error"]


def test_list_files(tmp_path):
    tools = FileTools(tmp_path)

    tools.write_file("a.html", HTML_FIXTURE)
    tools.write_file("sub/b.css", CSS_FIXTURE)

    result = tools.list_files()
    assert result["status"] == "ok"

    paths = {f["path"] for f in result["files"]}
    assert paths == {"a.html", "sub/b.css"}

def test_edit_file_many_applies_all(tmp_path):
    tools = FileTools(tmp_path)
    tools.write_file(
        "a.html",
        HTML_FIXTURE.replace("Hello World", "Hello World")
    )

    result = tools.edit_file_many("a.html", [
        {"old": "Hello World", "new": "Привет"},
        {"old": "Test Page", "new": "Тест"},
    ])

    assert result["status"] == "ok"
    assert result["edits_applied"] == 2

    read = tools.read_file("a.html")
    assert "Привет" in read["content"]
    assert "Тест" in read["content"]


def test_edit_file_many_atomic_on_failure(tmp_path):
    """Если один edit не находит old — ничего не меняется."""
    tools = FileTools(tmp_path)
    tools.write_file("a.html", HTML_FIXTURE)

    result = tools.edit_file_many("a.html", [
        {"old": "Hello World", "new": "Привет"},
        {"old": "НЕТ_ТАКОГО_ТЕКСТА", "new": "x"},
    ])

    assert "error" in result
    # Первая правка НЕ должна быть применена.
    read = tools.read_file("a.html")
    assert "Hello World" in read["content"]
    assert "Привет" not in read["content"]