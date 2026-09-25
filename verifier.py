import re
from html.parser import HTMLParser
from pathlib import Path


class _HTMLCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.has_title = False
        self.has_h1 = False
        # (kind, href), kind ∈ {"page", "resource"}
        self.internal_links: list[tuple[str, str]] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self.has_h1 = True

        if tag == "a" and "href" in attrs_dict:
            self._collect(attrs_dict["href"], "page")
        if tag == "link" and "href" in attrs_dict:
            self._collect(attrs_dict["href"], "resource")
        if tag == "script" and "src" in attrs_dict:
            self._collect(attrs_dict["src"], "resource")
        if tag == "img" and "src" in attrs_dict:
            self._collect(attrs_dict["src"], "resource")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and data.strip():
            self.has_title = True

    def _collect(self, href: str, kind: str):
        if not href:
            return
        href = href.strip()
        if href.startswith((
            "http://", "https://", "//",
            "mailto:", "tel:", "javascript:",
            "#", "data:",
        )):
            return
        self.internal_links.append((kind, href))


class SiteVerifier:
    def __init__(self, root_dir: str | Path):
        self.root = Path(root_dir).resolve()

    def verify_html(self, rel_path: str) -> dict:
        """Проверяет один HTML-файл.

        Возвращает dict:
          status: "ok" | "warning" | "error"
          errors: [...]
          warnings: [...]
          stats: {...}
        """
        target = self.root / rel_path

        if not target.exists():
            return {
                "status": "error",
                "errors": [f"file not found: {rel_path}"],
                "warnings": [],
                "stats": {},
            }

        text = target.read_text(encoding="utf-8")
        parser = _HTMLCollector()

        try:
            parser.feed(text)
        except Exception as exc:
            return {
                "status": "error",
                "errors": [f"HTML parse error: {exc}"],
                "warnings": [],
                "stats": {},
            }

        errors: list[str] = []
        warnings: list[str] = []

        # Обязательные элементы.
        if not parser.has_title:
            errors.append("missing <title>")
        if not parser.has_h1:
            warnings.append("missing <h1>")

        # Проверка завершённости документа: ловим обрезку.
        lowered = text.lower()
        if "</html>" not in lowered:
            errors.append("missing closing </html> tag")
        if "</body>" not in lowered:
            errors.append("missing closing </body> tag")

        # Синтаксис открывающих тегов: ищем `<tag attr`
        # без закрывающего `>` до конца строки.
        # Модель иногда теряет `>` при генерации.
        malformed_pattern = re.compile(
            r"<(section|article|header|main|div|nav|ul|li|p|h[1-6])"
            r"\s[^>]*$",
            re.MULTILINE,
        )

        for tag in malformed_pattern.findall(text):
            errors.append(
                f"malformed opening tag: <{tag} без '>'"
            )

        # Проверяем линки. Различаем:
        #  - <a href="..."> — переход на страницу: ошибка,
        #    если файла нет.
        #  - <link href>, <script src>, <img src> —
        #    ресурсы, могут быть подключены позже: warning.
        for kind, link in parser.internal_links:
            path_part = link.split("#")[0].split("?")[0]
            if not path_part:
                continue
            resolved = (target.parent / path_part).resolve()
            if resolved.exists():
                continue

            if kind == "page":
                errors.append(f"broken page link: {link}")
            else:
                warnings.append(
                    f"missing resource (may be added later): {link}"
                )

        if errors:
            status = "error"
        elif warnings:
            status = "warning"
        else:
            status = "ok"

        return {
            "status": status,
            "errors": errors,
            "warnings": warnings,
            "stats": {
                "bytes": len(text.encode("utf-8")),
                "internal_links": len(parser.internal_links),
                "missing_pages": sum(
                    1 for e in errors if "broken page" in e
                ),
                "missing_resources": sum(
                    1 for w in warnings if "missing resource" in w
                ),
            },
        }

    def verify_css(self, rel_path: str) -> dict:
        """Простые проверки CSS-файла.

        Не парсим по-настоящему — только ловим явные
        признаки обрезки и грубые синтаксические ошибки.
        """
        target = self.root / rel_path

        if not target.exists():
            return {
                "status": "error",
                "errors": [f"file not found: {rel_path}"],
                "warnings": [],
                "stats": {},
            }

        text = target.read_text(encoding="utf-8")

        # Убираем комментарии для проверок.
        no_comments = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

        errors: list[str] = []
        warnings: list[str] = []

        # Запрещённые функции препроцессоров: в чистом CSS
        # их нет. Модель часто путает SCSS и CSS.
        forbidden_functions = (
            "lighten(", "darken(", "saturate(",
            "desaturate(", "fadein(", "fadeout(",
            "fade(", "mix(", "adjust-hue(",
            "tint(", "shade(", "rgba(var(",
        )
        lowered = no_comments.lower()
        for fn in forbidden_functions:
            if fn in lowered:
                errors.append(
                    f"SCSS function in plain CSS: {fn.rstrip('(')}"
                )

        # Детектор дубликатов селекторов.
        # Два правила с одним селектором = тихий баг:
        # второе перекрывает первое, dead code.
        selector_pattern = re.compile(
            r"([^{}]+?)\s*\{", re.MULTILINE,
        )
        seen: dict[str, int] = {}
        for match in selector_pattern.finditer(no_comments):
            selector = " ".join(
                match.group(1).split()
            ).strip()

            if not selector or selector.startswith("@"):
                continue

            seen[selector] = seen.get(selector, 0) + 1

        duplicates = [
            s for s, count in seen.items() if count > 1
        ]
        for dup in duplicates:
            errors.append(
                f"duplicate selector: {dup}"
            )

        # Баланс фигурных скобок.
        opens = no_comments.count("{")
        closes = no_comments.count("}")

        if opens != closes:
            errors.append(
                f"unbalanced braces: {opens} '{{' vs {closes} '}}'"
            )

        # Обрезка: файл не должен заканчиваться на ':', ',', '{'.
        stripped = no_comments.rstrip()
        if stripped and stripped[-1] in (":", ",", "{"):
            errors.append(
                f"file looks truncated: ends with {stripped[-1]!r}"
            )

        # Обрезка hex-цвета: '#e6e' в конце.
        if re.search(r"#[0-9a-fA-F]{1,5}\s*$", stripped):
            errors.append(
                "file looks truncated: incomplete hex color"
            )

        if not stripped:
            warnings.append("CSS file is empty")

        if errors:
            status = "error"
        elif warnings:
            status = "warning"
        else:
            status = "ok"

        return {
            "status": status,
            "errors": errors,
            "warnings": warnings,
            "stats": {"bytes": len(text.encode("utf-8"))},
        }

    def verify_site(self, entry: str = "index.html") -> dict:
        """Проверяет весь сайт.

        Обходит все *.html, *.htm, *.css в корне.
        Возвращает агрегированный результат.
        """
        files = []
        for ext in ("*.html", "*.htm", "*.css"):
            for item in sorted(self.root.rglob(ext)):
                rel = item.relative_to(self.root).as_posix()
                files.append(rel)

        if not files:
            return {
                "status": "error",
                "errors": ["no HTML or CSS files found"],
                "files": {},
                "entry_exists": False,
            }

        results = {}
        for f in files:
            if f.endswith(".css"):
                results[f] = self.verify_css(f)
            else:
                results[f] = self.verify_html(f)

        all_errors = []
        for fname, r in results.items():
            for e in r["errors"]:
                all_errors.append(f"{fname}: {e}")

        overall = "ok"
        if all_errors:
            overall = "error"
        elif any(
            r["status"] == "warning" for r in results.values()
        ):
            overall = "warning"

        return {
            "status": overall,
            "errors": all_errors,
            "files": results,
            "entry_exists": entry in files,
        }