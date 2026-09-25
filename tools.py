from pathlib import Path


class FileToolsError(Exception):
    pass


class FileTools:
    """Файловые операции внутри разрешённой директории.

    Модель не может выйти за пределы root_dir: любой path
    нормализуется и проверяется на принадлежность.
    """

    MAX_CONTENT_BYTES = 50_000  # 50 KB

    # Минимальные размеры, ниже которых файл почти
    # наверняка — обрезанная генерация.
    MIN_SIZES = {
        ".html": 120,
        ".htm": 120,
        ".css": 60,
    }

    def __init__(self, root_dir: str | Path):
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, user_path: str) -> Path:
        if not isinstance(user_path, str) or not user_path.strip():
            raise FileToolsError(
                "path must be a non-empty string. "
                "Example: 'about.html' or 'css/style.css'."
            )

        # Запрещаем абсолютные пути, Windows-диски и ..
        if user_path.startswith(("/", "\\")):
            raise FileToolsError(
                f"absolute paths are not allowed: {user_path!r}. "
                f"Use a relative path like 'about.html' "
                f"or 'css/style.css'."
            )
        if ":" in user_path:
            raise FileToolsError(
                f"paths with drive letters or colons are not "
                f"allowed: {user_path!r}. Use a relative path "
                f"like 'about.html' or 'css/style.css'."
            )

        candidate = (self.root / user_path).resolve()

        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise FileToolsError(
                f"path escapes root: {user_path!r}"
            )

        return candidate

    def write_file(self, path: str, content: str) -> dict:
        try:
            target = self._safe_path(path)
        except FileToolsError as exc:
            return {"error": str(exc)}

        if not isinstance(content, str):
            return {"error": "content must be a string"}

        encoded = content.encode("utf-8")

        if len(encoded) > self.MAX_CONTENT_BYTES:
            return {
                "error": (
                    f"content too large: {len(encoded)} bytes "
                    f"(max {self.MAX_CONTENT_BYTES})"
                )
            }

        # Проверка минимума — защита от обрезанных генераций.
        ext = target.suffix.lower()
        min_size = self.MIN_SIZES.get(ext)
        if min_size is not None and len(encoded) < min_size:
            return {
                "error": (
                    f"content too small for {ext}: "
                    f"{len(encoded)} bytes "
                    f"(min {min_size}). Похоже, файл "
                    f"оборван — перепиши его полностью."
                )
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        return {
            "status": "ok",
            "path": path,
            "bytes": len(encoded),
        }

    def edit_file(
            self, path: str, old: str, new: str,
        ) -> dict:
            """Точечная замена в существующем файле.

            old должен встречаться РОВНО один раз.
            Иначе — ошибка, чтобы не сделать случайную замену.
            """
            try:
                target = self._safe_path(path)
            except FileToolsError as exc:
                return {"error": str(exc)}

            if not target.exists() or not target.is_file():
                return {"error": f"file not found: {path}"}

            if not isinstance(old, str) or not old:
                return {"error": "old must be non-empty string"}
            if not isinstance(new, str):
                return {"error": "new must be string"}

            content = target.read_text(encoding="utf-8")

            count = content.count(old)
            if count == 0:
                return {
                    "error": (
                        f"old_text not found in {path}. "
                        f"Check exact text — whitespace matters. "
                        f"Прочитай файл через read_file и скопируй "
                        f"фрагмент точно."
                    ),
                }
            if count > 1:
                return {
                    "error": (
                        f"old_text appears {count} times in {path}. "
                        f"Сделай фрагмент более уникальным."
                    ),
                }

            new_content = content.replace(old, new, 1)
            encoded = new_content.encode("utf-8")

            if len(encoded) > self.MAX_CONTENT_BYTES:
                return {
                    "error": (
                        f"result too large: {len(encoded)} bytes"
                    ),
                }

            target.write_text(new_content, encoding="utf-8")

            return {
                "status": "ok",
                "path": path,
                "old_bytes": len(content.encode("utf-8")),
                "new_bytes": len(encoded),
            }

    def edit_file_many(
        self,
        path: str,
        edits: list[dict],
    ) -> dict:
        """Применяет несколько замен последовательно.

        Каждая замена — {"old": str, "new": str}.
        Все old должны встречаться ровно один раз
        в текущей версии файла (с учётом предыдущих правок).

        Если хоть одна замена не проходит — файл
        НЕ изменяется (атомарность).
        """
        try:
            target = self._safe_path(path)
        except FileToolsError as exc:
            return {"error": str(exc)}

        if not target.exists() or not target.is_file():
            return {"error": f"file not found: {path}"}

        if not isinstance(edits, list) or not edits:
            return {"error": "edits must be a non-empty list"}

        content = target.read_text(encoding="utf-8")
        applied: list[dict] = []

        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return {
                    "error": f"edit[{i}] must be an object",
                }

            old = edit.get("old", "")
            new = edit.get("new", "")

            if not isinstance(old, str) or not old:
                return {"error": f"edit[{i}].old is empty"}

            if not isinstance(new, str):
                return {"error": f"edit[{i}].new is not string"}

            count = content.count(old)
            if count == 0:
                return {
                    "error": (
                        f"edit[{i}]: old_text not found. "
                        f"Предыдущие edits применены не были "
                        f"(атомарность)."
                    ),
                }
            if count > 1:
                return {
                    "error": (
                        f"edit[{i}]: old_text appears "
                        f"{count} times."
                    ),
                }

            content = content.replace(old, new, 1)
            applied.append({"old": old, "new": new})

        encoded = content.encode("utf-8")
        if len(encoded) > self.MAX_CONTENT_BYTES:
            return {"error": f"result too large: {len(encoded)}"}

        target.write_text(content, encoding="utf-8")

        return {
            "status": "ok",
            "path": path,
            "edits_applied": len(applied),
            "new_bytes": len(encoded),
        }

    def append_file(self, path: str, content: str) -> dict:
        """Добавляет содержимое в конец файла.

        Если файла нет — создаёт его. Не требует old.
        Используется для «добавить правило/блок в конец».
        """
        if not isinstance(content, str) or not content.strip():
            return {"error": "content must be non-empty string"}

        try:
            target = self._safe_path(path)
        except FileToolsError as exc:
            return {"error": str(exc)}

        existing = ""
        if target.exists() and target.is_file():
            existing = target.read_text(encoding="utf-8")

        # Нормализуем стык: гарантируем перевод строки.
        sep = ""
        if existing and not existing.endswith("\n"):
            sep = "\n\n"
        elif existing:
            sep = "\n"

        new_content = existing + sep + content.strip() + "\n"
        encoded = new_content.encode("utf-8")

        if len(encoded) > self.MAX_CONTENT_BYTES:
            return {
                "error": f"result too large: {len(encoded)}"
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")

        return {
            "status": "ok",
            "path": path,
            "appended_bytes": len(content.encode("utf-8")),
            "new_bytes": len(encoded),
        }

    def read_file(self, path: str) -> dict:
        try:
            target = self._safe_path(path)
        except FileToolsError as exc:
            return {"error": str(exc)}

        if not target.exists():
            return {"error": f"file not found: {path}"}
        if not target.is_file():
            return {"error": f"not a file: {path}"}

        content = target.read_text(encoding="utf-8")
        return {
            "status": "ok",
            "path": path,
            "content": content,
            "bytes": len(content.encode("utf-8")),
        }

    def list_files(self, subdir: str = "") -> dict:
        try:
            target = self._safe_path(subdir) if subdir else self.root
        except FileToolsError as exc:
            return {"error": str(exc)}

        if not target.is_dir():
            return {"error": f"not a directory: {subdir!r}"}

        files = []
        for item in sorted(target.rglob("*")):
            if item.is_file():
                rel = item.relative_to(self.root)
                files.append({
                    "path": str(rel).replace("\\", "/"),
                    "bytes": item.stat().st_size,
                })

        return {"status": "ok", "files": files}