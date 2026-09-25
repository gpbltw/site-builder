import json
from pathlib import Path

from budget import TaskBudget
from tools import FileTools
from verifier import SiteVerifier


WRITER_SYSTEM_PROMPT = (
    "Ты веб-разработчик. Создаёшь и правишь статические "
    "HTML-страницы.\n"
    "\n"
    "Инструменты:\n"
    "- write_file(path, content) — создать НОВЫЙ файл или "
    "полностью перезаписать существующий.\n"
    "- edit_file(path, old, new) — точечная замена в файле. "
    "Используй для ПРАВОК, а не для создания файлов.\n"
    "- read_file(path) — прочитать файл.\n"
    "- list_files() — список файлов.\n"
    "\n"
    "Правило выбора инструмента:\n"
    "- Файла нет → write_file.\n"
    "- Файл есть, но нужна правка → read_file, потом "
    "edit_file.\n"
    "- НИКОГДА не переписывай существующий файл целиком "
    "через write_file, если задача — исправить что-то "
    "точечно. Используй edit_file.\n"
    "\n"
    "Порядок работы:\n"
    "1. css/style.css (write_file).\n"
    "2. index.html (write_file).\n"
    "3. about.html (write_file), если запрошена.\n"
    "4. Если harness сообщает о пробеле — read_file + "
    "edit_file. НЕ write_file.\n"
    "\n"
    "Требования к CSS:\n"
    "- файл не больше 40 строк;\n"
    "- только body, h1, p, a, header, main;\n"
    "- все скобки закрыты, полные hex-цвета.\n"
    "\n"
    "Требования к HTML:\n"
    "- <!DOCTYPE html> в начале, </html> в конце;\n"
    "- <meta charset=\"UTF-8\"> и <title> в <head>;\n"
    "- <h1> в <body>;\n"
    "- все теги закрыты.\n"
    "\n"
    "Когда всё готово — короткое резюме без tool calls.\n"
)


WRITER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Записать файл. Перезаписывает существующий. "
                "Используй только для НОВЫХ файлов или "
                "полной замены содержимого."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Заменить фрагмент в существующем файле. "
                "old должен встречаться в файле РОВНО один раз. "
                "Используй для точечных правок ВМЕСТО "
                "перезаписи всего файла через write_file. "
                "Перед вызовом прочитай файл через read_file, "
                "чтобы увидеть точный текст фрагмента."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {
                        "type": "string",
                        "description": (
                            "Уникальный фрагмент для замены. "
                            "Скопируй точно из файла."
                        ),
                    },
                    "new": {
                        "type": "string",
                        "description": "На что заменить.",
                    },
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать файл проекта.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Список файлов проекта.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


def _auto_close_html(content: str) -> tuple[str, list[str]]:
    """Гарантирует, что HTML-документ закрыт тегами.

    Модель qwen3.8 часто останавливается на середине документа
    (finish_reason='stop', но </body></html> отсутствуют).
    Harness автопочинит это и логирует факт правки.
    """
    fixes: list[str] = []
    lowered = content.lower()

    if "</body>" not in lowered:
        content = content.rstrip() + "\n</body>"
        fixes.append("appended </body>")

    if "</html>" not in content.lower():
        content = content.rstrip() + "\n</html>"
        fixes.append("appended </html>")

    if fixes:
        content = content + "\n"

    return content, fixes


def _execute_tool(
    name: str,
    raw_arguments: str,
    tools: FileTools,
    verifier: SiteVerifier,
) -> dict:
    try:
        args = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {"error": "arguments must be valid JSON"}

    if not isinstance(args, dict):
        return {"error": "arguments must be JSON object"}

    if name == "write_file":
        if set(args) != {"path", "content"}:
            return {"error": "expect exactly path, content"}

        path = args["path"]
        content = args["content"]
        fixes: list[str] = []

        # Автопочинка HTML: дописать </body></html>, если их нет.
        if path.endswith((".html", ".htm")):
            content, fixes = _auto_close_html(content)
            if fixes:
                print(
                    f"[Harness] Auto-close HTML: "
                    f"{', '.join(fixes)}"
                )

        result = tools.write_file(path, content)

        if fixes:
            result["harness_fixes"] = fixes

        # Проверяем сразу после записи — HTML и CSS.
        if "error" not in result:
            if path.endswith((".html", ".htm")):
                result["verification"] = verifier.verify_html(path)
            elif path.endswith(".css"):
                result["verification"] = verifier.verify_css(path)

        return result

    if name == "edit_file":
        if set(args) != {"path", "old", "new"}:
            return {"error": "expect exactly path, old, new"}

        path = args["path"]
        result = tools.edit_file(
            path, args["old"], args["new"],
        )

        if "error" not in result:
            if path.endswith((".html", ".htm")):
                result["verification"] = verifier.verify_html(path)
            elif path.endswith(".css"):
                result["verification"] = verifier.verify_css(path)

        return result

    if name == "read_file":
        if set(args) != {"path"}:
            return {"error": "expect exactly path"}
        return tools.read_file(args["path"])

    if name == "list_files":
        if args:
            return {"error": "list_files takes no arguments"}
        return tools.list_files()

    return {"error": f"unknown tool: {name}"}


def run_writer(
    client,
    model: str,
    task: str,
    tools: FileTools,
    budget: TaskBudget,
    verifier: SiteVerifier,
    max_completion_tokens: int = 1000,
    max_writes_per_path: int = 3,
    max_edits_per_path: int = 5,
    max_reads_per_path: int = 3,
    max_consecutive_same_error: int = 2,
    verified_paths: set[str] | None = None,
) -> dict:
    """Запускает writer-агента с файловыми инструментами.

    Логика:
    - HTML автозакрывается (</body></html>), если модель оборвала;
    - write_file в verified-путь → SKIP;
    - edit_file в verified-путь разрешён (точечная правка);
    - два write одного пути за один ответ → REJECT;
    - одинаковая ошибка дважды подряд → SPIRAL, выход;
    - если итерация без реальных записей и сайт валиден → completed.
    """
    messages = [
        {"role": "system", "content": WRITER_SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    tool_log: list[dict] = []
    model_calls_used = 0
    write_counts: dict[str, int] = {}
    edit_counts: dict[str, int] = {}
    read_counts: dict[str, int] = {}
    if verified_paths is None:
        verified_paths = set()
    consecutive_failures: dict[str, int] = {}
    last_error_by_path: dict[str, str] = {}

    while True:
        if not budget.try_consume_model_call():
            return {
                "status": "limit_reached",
                "answer": None,
                "error": (
                    "Бюджет исчерпан по model calls. "
                    f"Состояние: {budget.snapshot()}."
                ),
                "model_calls": model_calls_used,
                "tool_calls": len(tool_log),
                "tool_log": tool_log,
            }

        model_calls_used += 1

        print(
            f"\n[Writer] Вызов "
            f"{budget.used_model_calls}/{budget.max_model_calls}"
        )

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=WRITER_TOOLS,
            tool_choice="auto",
            max_completion_tokens=max_completion_tokens,
        )

        if response.usage:
            u = response.usage
            print(
                f"[Токены] Вход: {u.prompt_tokens}; "
                f"генерация: {u.completion_tokens}"
            )

        choice = response.choices[0]
        message = choice.message

        if choice.finish_reason == "length":
            return {
                "status": "limit_reached",
                "answer": message.content,
                "error": (
                    "Writer достиг лимита генерации. "
                    "Файл слишком большой для одного вызова."
                ),
                "model_calls": model_calls_used,
                "tool_calls": len(tool_log),
                "tool_log": tool_log,
            }

        if not message.tool_calls:
            return {
                "status": "completed",
                "answer": message.content or "(no text)",
                "error": None,
                "model_calls": model_calls_used,
                "tool_calls": len(tool_log),
                "tool_log": tool_log,
            }

        requested = len(message.tool_calls)
        if not budget.try_consume_tool_calls(requested):
            return {
                "status": "limit_reached",
                "answer": None,
                "error": (
                    "Бюджет исчерпан по tool calls. "
                    f"Состояние: {budget.snapshot()}."
                ),
                "model_calls": model_calls_used,
                "tool_calls": len(tool_log),
                "tool_log": tool_log,
            }

        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.function.name,
                        "arguments": c.function.arguments,
                    },
                }
                for c in message.tool_calls
            ],
        })

        real_writes_this_iter = 0
        skipped_writes_this_iter = 0
        paths_in_this_response: set[str] = set()

        for call in message.tool_calls:
            args_preview = call.function.arguments[:100]

            # --- write_file ---
            if call.function.name == "write_file":
                try:
                    parsed = json.loads(call.function.arguments)
                    path_key = parsed.get("path", "?")
                except Exception:
                    path_key = "?"

                # Запрет на два write_file одного пути в одном ответе.
                if path_key in paths_in_this_response:
                    print(
                        f"[Tool] REJECT: {path_key} — "
                        f"второй write в одном ответе"
                    )
                    result = {
                        "error": (
                            f"{path_key} уже был записан в этом "
                            f"ответе. Один путь — одна запись "
                            f"за ответ. Если нужна правка — "
                            f"сделай её следующим вызовом."
                        ),
                    }
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            result, ensure_ascii=False,
                        ),
                    })
                    continue

                paths_in_this_response.add(path_key)

                # Уже проверенный файл — пропускаем без записи.
                if path_key in verified_paths:
                    skipped_writes_this_iter += 1
                    print(
                        f"[Tool] SKIP: {path_key} "
                        f"(already verified)"
                    )
                    result = {
                        "status": "skipped",
                        "info": (
                            f"{path_key} уже прошёл проверку. "
                            f"Полная перезапись не нужна. "
                            f"Если нужна правка — используй "
                            f"edit_file(path, old, new). "
                            f"Если всё готово — короткое "
                            f"резюме текстом без tool calls."
                        ),
                    }
                    tool_log.append({
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                        "result": result,
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            result, ensure_ascii=False,
                        ),
                    })
                    continue

                write_counts[path_key] = (
                    write_counts.get(path_key, 0) + 1
                )

                if write_counts[path_key] > max_writes_per_path:
                    print(
                        f"[Tool] СТОП: path={path_key} "
                        f"перезаписан "
                        f"{write_counts[path_key]} раз."
                    )
                    return {
                        "status": "failed",
                        "answer": None,
                        "error": (
                            f"Модель зациклилась на записи "
                            f"{path_key}: "
                            f"{write_counts[path_key]} попыток."
                        ),
                        "model_calls": model_calls_used,
                        "tool_calls": len(tool_log),
                        "tool_log": tool_log,
                    }

                real_writes_this_iter += 1

                # Показываем path, а не content — иначе
                # не видно, какой путь запрашивается.
                try:
                    parsed_preview = json.loads(
                        call.function.arguments
                    )
                    path_preview = parsed_preview.get("path", "?")
                except Exception:
                    path_preview = "?"
                print(f"[Tool] write_file(path={path_preview!r})")

                result = _execute_tool(
                    call.function.name,
                    call.function.arguments,
                    tools,
                    verifier,
                )

                v = result.get("verification")

                if v and v.get("status") == "ok":
                    verified_paths.add(path_key)
                    consecutive_failures.pop(path_key, None)
                    last_error_by_path.pop(path_key, None)

                elif v and v.get("status") in ("error", "warning"):
                    consecutive_failures[path_key] = (
                        consecutive_failures.get(path_key, 0) + 1
                    )
                    error_signature = "|".join(
                        v.get("errors", [])
                    )

                    if (
                        last_error_by_path.get(path_key)
                        == error_signature
                        and consecutive_failures[path_key]
                        >= max_consecutive_same_error
                    ):
                        print(
                            f"[Tool] СПИРАЛЬ: {path_key} "
                            f"повторяет одну и ту же "
                            f"ошибку "
                            f"{consecutive_failures[path_key]} раз."
                        )
                        tool_log.append({
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                            "result": result,
                        })
                        return {
                            "status": "failed",
                            "answer": None,
                            "error": (
                                f"Модель повторяет одну и ту "
                                f"же ошибку для {path_key}: "
                                f"{error_signature}. "
                                f"Файл сохранён в текущем виде."
                            ),
                            "model_calls": model_calls_used,
                            "tool_calls": len(tool_log),
                            "tool_log": tool_log,
                        }

                    last_error_by_path[path_key] = error_signature

                tool_log.append({
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                    "result": result,
                })

                # Логирование.
                if "error" in result:
                    print(f"[Tool] error: {result['error']}")
                else:
                    print(f"[Tool] ok: {path_key}")
                    if v:
                        print(
                            f"[Verify] {path_key}: "
                            f"{v['status']}"
                        )
                        for err in v["errors"]:
                            print(f"[Verify]   error: {err}")
                        for w in v["warnings"]:
                            print(f"[Verify]   warning: {w}")

                # Подсказки после двух провалов подряд.
                fails = consecutive_failures.get(path_key, 0)

                if fails >= 2 and path_key.endswith(".css"):
                    result["harness_hint"] = (
                        "CSS дважды не прошёл проверку. "
                        "Упрости: только body { background, "
                        "color, font-family }, h1 { font-size }, "
                        "p { max-width, margin }. "
                        "Не больше 15 строк. Все скобки закрыты."
                    )
                    print(
                        f"[Tool] HINT: упрости CSS {path_key}"
                    )

                elif fails >= 2 and path_key.endswith(
                    (".html", ".htm")
                ):
                    result["harness_hint"] = (
                        "HTML дважды не прошёл проверку. "
                        "Скорее всего, генерация оборвана. "
                        "Перепиши файл КОРОЧЕ: один короткий "
                        "абзац, и ОБЯЗАТЕЛЬНО закончи "
                        "тегами </body> и </html>."
                    )
                    print(
                        f"[Tool] HINT: перепиши HTML {path_key}"
                    )

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(
                        result, ensure_ascii=False,
                    ),
                })
                continue

            # --- edit_file ---
            if call.function.name == "edit_file":
                try:
                    parsed = json.loads(call.function.arguments)
                    path_key = parsed.get("path", "?")
                except Exception:
                    path_key = "?"

                edit_counts[path_key] = (
                    edit_counts.get(path_key, 0) + 1
                )

                if edit_counts[path_key] > max_edits_per_path:
                    print(
                        f"[Tool] СТОП: {path_key} правился "
                        f"{edit_counts[path_key]} раз."
                    )
                    return {
                        "status": "failed",
                        "answer": None,
                        "error": (
                            f"Слишком много правок {path_key}: "
                            f"{edit_counts[path_key]}."
                        ),
                        "model_calls": model_calls_used,
                        "tool_calls": len(tool_log),
                        "tool_log": tool_log,
                    }

                print(f"[Tool] edit_file(path={path_key!r})")

                result = _execute_tool(
                    call.function.name,
                    call.function.arguments,
                    tools,
                    verifier,
                )

                v = result.get("verification")

                if v and v.get("status") == "ok":
                    verified_paths.add(path_key)
                    consecutive_failures.pop(path_key, None)
                    last_error_by_path.pop(path_key, None)

                tool_log.append({
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                    "result": result,
                })

                if "error" in result:
                    print(f"[Tool] error: {result['error']}")
                else:
                    print(f"[Tool] ok: {path_key}")
                    if v:
                        print(
                            f"[Verify] {path_key}: "
                            f"{v['status']}"
                        )
                        for err in v["errors"]:
                            print(f"[Verify]   error: {err}")
                        for w in v["warnings"]:
                            print(f"[Verify]   warning: {w}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(
                        result, ensure_ascii=False,
                    ),
                })
                continue

            # --- read_file ---
            if call.function.name == "read_file":
                try:
                    parsed = json.loads(call.function.arguments)
                    path_key = parsed.get("path", "?")
                except Exception:
                    path_key = "?"

                read_counts[path_key] = (
                    read_counts.get(path_key, 0) + 1
                )

                if read_counts[path_key] > max_reads_per_path:
                    print(
                        f"[Tool] СТОП: {path_key} прочитан "
                        f"{read_counts[path_key]} раз."
                    )
                    return {
                        "status": "failed",
                        "answer": None,
                        "error": (
                            f"Модель зациклилась на чтении "
                            f"{path_key}."
                        ),
                        "model_calls": model_calls_used,
                        "tool_calls": len(tool_log),
                        "tool_log": tool_log,
                    }

            # --- любой другой tool ---
            print(
                f"[Tool] {call.function.name}({args_preview}...)"
            )
            result = _execute_tool(
                call.function.name,
                call.function.arguments,
                tools,
                verifier,
            )
            tool_log.append({
                "name": call.function.name,
                "arguments": call.function.arguments,
                "result": result,
            })
            if "error" in result:
                print(f"[Tool] error: {result['error']}")
            else:
                print(f"[Tool] ok: {result.get('path', '?')}")

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(
                    result, ensure_ascii=False,
                ),
            })

        # --- Проверка завершения ---
        if (
            real_writes_this_iter == 0
            and skipped_writes_this_iter > 0
        ):
            site = verifier.verify_site()
            if site["status"] in ("ok", "warning"):
                skip_only_iterations = locals().get(
                    "_skip_only_iterations", 0
                ) + 1
                locals()["_skip_only_iterations"] = (
                    skip_only_iterations
                )

                if skip_only_iterations >= 2:
                    print(
                        "\n[Writer] Дважды без новых записей. "
                        "Завершаю."
                    )
                    return {
                        "status": "completed",
                        "answer": (
                            message.content
                            or "(модель остановилась, "
                            "но не дала резюме)"
                        ),
                        "error": None,
                        "model_calls": model_calls_used,
                        "tool_calls": len(tool_log),
                        "tool_log": tool_log,
                    }

                print(
                    "\n[Writer] SKIP без новых записей. "
                    "Спрашиваю модель о завершённости."
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": message.tool_calls[0].id,
                    "content": json.dumps({
                        "status": "hint",
                        "message": (
                            "Ты пытался перезаписать файл, "
                            "который уже прошёл проверку. "
                            "Полная перезапись не нужна. "
                            "Если нужна правка — используй "
                            "edit_file(path, old, new). "
                            "Проверь задачу: все ли файлы "
                            "созданы и содержат требуемые "
                            "элементы? Если чего-то не "
                            "хватает — используй edit_file. "
                            "Если всё готово — ответь коротким "
                            "текстом БЕЗ tool calls."
                        ),
                    }, ensure_ascii=False),
                })
                continue