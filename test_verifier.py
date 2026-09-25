from verifier import SiteVerifier


def test_valid_page(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><head><title>Hi</title></head>"
        "<body><h1>Hello</h1></body></html>",
        encoding="utf-8",
    )

    v = SiteVerifier(tmp_path)
    result = v.verify_html("index.html")

    assert result["status"] == "ok"
    assert result["errors"] == []


def test_missing_title(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body><h1>Hi</h1></body></html>",
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_html("index.html")

    assert result["status"] == "error"
    assert any("title" in e for e in result["errors"])


def test_missing_h1_is_warning(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><head><title>Hi</title></head><body></body></html>",
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_html("index.html")

    assert result["status"] == "warning"
    assert any("h1" in w for w in result["warnings"])


def test_broken_internal_link(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html><head><title>T</title></head>'
        '<body><h1>Hi</h1>'
        '<a href="missing.html">x</a></body></html>',
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_html("index.html")

    assert result["status"] == "error"
    assert any("missing.html" in e for e in result["errors"])


def test_external_links_ignored(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html><head><title>T</title></head>'
        '<body><h1>Hi</h1>'
        '<a href="https://example.com">x</a>'
        '<a href="mailto:a@b.c">m</a>'
        '<a href="#section">s</a>'
        '</body></html>',
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_html("index.html")

    assert result["status"] == "ok"
    assert result["stats"]["internal_links"] == 0


def test_verify_site_with_two_pages(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html><head><title>Home</title></head>'
        '<body><h1>Home</h1>'
        '<a href="about.html">About</a></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "about.html").write_text(
        '<html><head><title>About</title></head>'
        '<body><h1>About</h1>'
        '<a href="index.html">Home</a></body></html>',
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_site()

    assert result["status"] == "ok"
    assert result["entry_exists"] is True
    assert len(result["files"]) == 2

def test_missing_closing_html(tmp_path):
    """Обрезка документа должна ловиться."""
    (tmp_path / "index.html").write_text(
        "<html><head><title>T</title></head>"
        "<body><h1>Hi</h1>",
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_html("index.html")

    assert result["status"] == "error"
    assert any("</html>" in e for e in result["errors"])
    assert any("</body>" in e for e in result["errors"])


def test_missing_closing_body_only(tmp_path):
    """Если </html> есть, но </body> — нет, тоже ошибка."""
    (tmp_path / "index.html").write_text(
        "<html><head><title>T</title></head>"
        "<body><h1>Hi</h1></html>",
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_html("index.html")

    assert result["status"] == "error"
    assert any("</body>" in e for e in result["errors"])

def test_rejects_scss_function(tmp_path):
    (tmp_path / "style.css").write_text(
        ":root{--accent:#7c9aff}\n"
        "a:hover{color:lighten(var(--accent))}\n",
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_css("style.css")

    assert result["status"] == "error"
    assert any(
        "SCSS" in e for e in result["errors"]
    )

def test_rejects_duplicate_selector(tmp_path):
    (tmp_path / "style.css").write_text(
        ":root{--x:#fff}\n"
        "body{color:red;padding:1rem}\n"
        "body{color:blue;margin:0}\n",
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_css("style.css")

    assert result["status"] == "error"
    assert any(
        "duplicate selector" in e
        and "body" in e
        for e in result["errors"]
    )

def test_malformed_opening_tag(tmp_path):
    """Открывающий тег без '>' должен ловиться."""
    (tmp_path / "index.html").write_text(
        '<!DOCTYPE html>\n'
        '<html><head><title>T</title></head>\n'
        '<body>\n'
        '<h1>Hi</h1>\n'
        '<section class="advantages"\n'
        '  <h2>Преимущества</h2>\n'
        '<article><p>Текст.</p></article>\n'
        '</section>\n'
        '</body></html>',
        encoding="utf-8",
    )

    result = SiteVerifier(tmp_path).verify_html("index.html")

    assert result["status"] == "error"
    assert any(
        "malformed" in e and "section" in e
        for e in result["errors"]
    )