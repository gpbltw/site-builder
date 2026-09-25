import argparse
import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from groq import APIError, AsyncGroq

from budget import TaskBudget
from content_checker import check_content
from planner import plan_edit
from retry_client import AsyncRetryingGroq
from section_writer import generate_section
from tools import FileTools
from verifier import SiteVerifier


MAX_PARALLEL_FILES = 2


def build_snapshot(tools, max_file_chars: int = 1500) -> str:
    files = tools.list_files().get("files", [])
    if not files:
        return "(проект пуст)"

    parts = []
    for f in files:
        path = f["path"]
        read = tools.read_file(path)
        if "error" in read:
            continue

        content = read["content"]
        if len(content) > max_file_chars:
            head = max_file_chars // 2
            tail = max_file_chars - head - 40
            content = (
                content[:head]
                + "\n... [середина обрезана] ...\n"
                + content[-tail:]
            )

        parts.append(f"=== {path} ===\n{content}")

    return "\n\n".join(parts)


async def execute_insert_section(
    client, model, tools, verifier, budget,
    file_path, description,
):
    if not file_path.endswith((".html", ".htm")):
        return {
            "status": "failed",
            "error": (
                f"insert_section работает только с HTML. "
                f"{file_path} — не HTML."
            ),
        }

    gen = await generate_section(
        client=client, model=model,
        section_name=description[:60],
        description=description,
        budget=budget,
    )
    if gen["status"] != "ok" or len(gen["html"]) < 30:
        return {
            "status": "failed",
            "error": f"секция: {gen.get('error', 'too short')}",
        }

    read = tools.read_file(file_path)
    if "error" in read:
        return {"status": "failed", "error": read["error"]}

    content = read["content"]
    if "</main>" in content:
        anchor = "</main>"
    elif "</body>" in content:
        anchor = "</body>"
    else:
        return {
            "status": "failed",
            "error": f"нет </main> или </body> в {file_path}",
        }

    if not budget.try_consume_tool_calls(1):
        return {
            "status": "limit_reached",
            "error": f"tool budget: {budget.snapshot()}",
        }

    new_content = gen["html"].strip() + "\n" + anchor
    edit = tools.edit_file(file_path, anchor, new_content)
    if "error" in edit:
        return {"status": "failed", "error": edit["error"]}

    verify = verifier.verify_html(file_path)
    return {
        "status": "ok",
        "html": gen["html"],
        "verification": verify,
    }


async def execute_append_file(
    client, model, tools, verifier, budget,
    file_path, description,
):
    if not budget.try_consume_model_call():
        return {
            "status": "limit_reached",
            "error": f"budget: {budget.snapshot()}",
        }

    is_css = file_path.endswith(".css")
    system = (
        "Ты генерируешь фрагмент для добавления в конец файла. "
        "Верни ТОЛЬКО содержимое, без ```, без пояснений.\n"
    )
    if is_css:
        system += (
            "\nТолько ЧИСТЫЙ CSS. Без SCSS-функций "
            "(lighten, darken, mix). Все скобки закрыты."
        )

    print(f"\n[Editor] append_file: {file_path}")

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": (
                f"Файл: {file_path}\n"
                f"Что добавить: {description}"
            )},
        ],
        max_completion_tokens=600,
    )

    if response.usage:
        u = response.usage
        print(
            f"[Editor] токены: вход={u.prompt_tokens}, "
            f"генерация={u.completion_tokens}"
        )

    text = (response.choices[0].message.content or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    if not text:
        return {"status": "failed", "error": "пустой ответ"}

    if not budget.try_consume_tool_calls(1):
        return {
            "status": "limit_reached",
            "error": f"tool budget: {budget.snapshot()}",
        }

    result = tools.append_file(file_path, text)
    if "error" in result:
        return {"status": "failed", "error": result["error"]}

    if file_path.endswith(".css"):
        verify = verifier.verify_css(file_path)
    elif file_path.endswith((".html", ".htm")):
        verify = verifier.verify_html(file_path)
    else:
        verify = {"status": "skipped"}

    return {"status": "ok", "verification": verify}


async def execute_write_file(
    client, model, tools, verifier, budget,
    file_path, description,
):
    if not budget.try_consume_model_call():
        return {
            "status": "limit_reached",
            "error": f"budget: {budget.snapshot()}",
        }

    is_css = file_path.endswith(".css")
    system = (
        "Ты генерируешь файл для сайта. "
        "Верни ТОЛЬКО содержимое, без ```, без пояснений.\n"
    )
    if is_css:
        system += (
            "\nПравила для CSS:\n"
            "- компактная запись;\n"
            "- CSS-переменные через :root;\n"
            "- только ЧИСТЫЙ CSS, без SCSS-функций;\n"
            "- не больше 50 строк."
        )
    else:
        system += (
            "\nПравила для HTML:\n"
            "- начинай с <!DOCTYPE html>;\n"
            "- заканчивай </body></html>."
        )

    print(f"\n[Editor] write_file: {file_path}")

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": (
                f"Файл: {file_path}\n"
                f"Задача: {description}"
            )},
        ],
        max_completion_tokens=900,
    )

    if response.usage:
        u = response.usage
        print(
            f"[Editor] токены: вход={u.prompt_tokens}, "
            f"генерация={u.completion_tokens}"
        )

    choice = response.choices[0]
    text = (choice.message.content or "").strip()

    if choice.finish_reason == "length":
        return {
            "status": "failed",
            "error": (
                f"файл обрезан лимитом ({len(text)} символов)."
            ),
        }

    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    if not text:
        return {"status": "failed", "error": "пустой ответ"}

    if not budget.try_consume_tool_calls(1):
        return {
            "status": "limit_reached",
            "error": f"tool budget: {budget.snapshot()}",
        }

    result = tools.write_file(file_path, text)
    if "error" in result:
        return {"status": "failed", "error": result["error"]}

    if file_path.endswith((".html", ".htm")):
        verify = verifier.verify_html(file_path)
    elif file_path.endswith(".css"):
        verify = verifier.verify_css(file_path)
    else:
        verify = {"status": "skipped"}

    return {
        "status": "ok",
        "bytes": result["bytes"],
        "verification": verify,
    }


async def execute_edit_file(
    client, model, tools, verifier, budget,
    file_path, instruction,
):
    read = tools.read_file(file_path)
    if "error" in read:
        return {"status": "failed", "error": read["error"]}

    if not budget.try_consume_model_call():
        return {
            "status": "limit_reached",
            "error": f"budget: {budget.snapshot()}",
        }

    system = (
        "Ты редактор HTML/CSS. Возвращаешь ТОЛЬКО JSON:\n"
        '{"edits": [{"old": "...", "new": "..."}, ...]}\n'
        "\n"
        "Правила:\n"
        "1. Один edit — одна замена.\n"
        "2. old должен ТОЧНО совпадать с фрагментом.\n"
        "3. old должен встречаться ровно один раз.\n"
        "4. Без markdown, только JSON.\n"
    )
    user = (
        f"Файл: {file_path}\n"
        f"Инструкция: {instruction}\n\n"
        f"Содержимое файла:\n{read['content']}"
    )

    print(f"\n[Editor] {file_path}: {instruction[:80]}")

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_completion_tokens=800,
    )

    if response.usage:
        u = response.usage
        print(
            f"[Editor] токены: вход={u.prompt_tokens}, "
            f"генерация={u.completion_tokens}"
        )

    text = (response.choices[0].message.content or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {
            "status": "failed",
            "error": f"не JSON: {text[:150]}",
        }

    edits = data.get("edits", [])
    if not isinstance(edits, list) or not edits:
        return {"status": "failed", "error": "нет edits"}

    print(f"[Editor] правок в ответе: {len(edits)}")

    if not budget.try_consume_tool_calls(1):
        return {
            "status": "limit_reached",
            "error": f"tool budget: {budget.snapshot()}",
        }

    result = tools.edit_file_many(file_path, edits)

    if "error" in result:
        print(f"[Editor] edit_file_many error: {result['error']}")

        if not budget.try_consume_model_call():
            return {
                "status": "failed",
                "error": (
                    f"first attempt failed: {result['error']}"
                ),
            }

        print("[Editor] retry с обратной связью")

        retry_user = (
            f"Файл: {file_path}\n"
            f"Инструкция: {instruction}\n\n"
            f"ПРЕДЫДУЩАЯ ПОПЫТКА ПРОВАЛИЛАСЬ:\n"
            f"{result['error']}\n\n"
            f"Содержимое файла (актуальное):\n"
            f"{tools.read_file(file_path)['content']}\n\n"
            f"Верни новые edits. Копируй old ТОЧНО "
            f"из файла выше."
        )

        retry_response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": retry_user},
            ],
            max_completion_tokens=800,
        )

        retry_text = (
            retry_response.choices[0].message.content or ""
        ).strip()
        if retry_text.startswith("```"):
            first_nl = retry_text.find("\n")
            if first_nl != -1:
                retry_text = retry_text[first_nl + 1:]
            if retry_text.rstrip().endswith("```"):
                retry_text = retry_text.rstrip()[:-3]

        try:
            retry_data = json.loads(retry_text)
        except json.JSONDecodeError:
            return {
                "status": "failed",
                "error": f"retry: не JSON",
            }

        retry_edits = retry_data.get("edits", [])
        if not retry_edits:
            return {
                "status": "failed",
                "error": "retry: пустой edits",
            }

        if not budget.try_consume_tool_calls(1):
            return {
                "status": "limit_reached",
                "error": f"tool budget: {budget.snapshot()}",
            }

        print(f"[Editor] retry правок: {len(retry_edits)}")
        result = tools.edit_file_many(file_path, retry_edits)

        if "error" in result:
            return {
                "status": "failed",
                "error": f"retry тоже провалился: {result['error']}",
            }

    if file_path.endswith((".html", ".htm")):
        verify = verifier.verify_html(file_path)
    elif file_path.endswith(".css"):
        verify = verifier.verify_css(file_path)
    else:
        verify = {"status": "skipped"}

    return {
        "status": "ok",
        "edits_applied": result.get("edits_applied"),
        "verification": verify,
    }


async def execute_operation(
    client, model, tools, verifier, budget, op,
):
    op_type = op["op"]
    file_path = op["file"]

    if op_type == "insert_section":
        return await execute_insert_section(
            client, model, tools, verifier, budget,
            file_path, op["description"],
        )
    if op_type == "append_file":
        return await execute_append_file(
            client, model, tools, verifier, budget,
            file_path, op["description"],
        )
    if op_type == "edit_file":
        return await execute_edit_file(
            client, model, tools, verifier, budget,
            file_path, op["instruction"],
        )
    if op_type == "write_file":
        return await execute_write_file(
            client, model, tools, verifier, budget,
            file_path, op["description"],
        )
    return {"status": "failed", "error": f"unknown op {op_type}"}


async def execute_operations_grouped(
    client, model, tools, verifier, budget, operations,
):
    """Группирует операции по файлам.

    Внутри одного файла — строго последовательно.
    Разные файлы — параллельно, но не более
    MAX_PARALLEL_FILES одновременно.
    """
    by_file: dict[str, list[dict]] = defaultdict(list)
    for op in operations:
        by_file[op["file"]].append(op)

    print(
        f"\n[Editor] групп по файлам: {len(by_file)} "
        f"(max параллельно: {MAX_PARALLEL_FILES})"
    )

    semaphore = asyncio.Semaphore(MAX_PARALLEL_FILES)

    async def run_file_group(file_path, ops):
        results = []
        async with semaphore:
            for i, op in enumerate(ops, 1):
                print(
                    f"[Editor] {file_path} "
                    f"({i}/{len(ops)}): {op['op']}"
                )
                r = await execute_operation(
                    client, model, tools, verifier, budget, op,
                )
                results.append({"op": op, "result": r})
                if r["status"] != "ok":
                    print(
                        f"[Editor] ошибка в {file_path}: "
                        f"{r.get('error')}"
                    )
        return file_path, results

    tasks = [
        run_file_group(path, ops)
        for path, ops in by_file.items()
    ]
    grouped_results = await asyncio.gather(*tasks)

    # Разворачиваем обратно в исходный порядок.
    flat: list[dict] = []
    for _, results in grouped_results:
        flat.extend(results)
    return flat


async def edit_site(
    client,
    model: str,
    task: str,
    output_dir: str | Path,
    max_model_calls: int = 20,
    max_tool_calls: int = 20,
) -> dict:
    output_dir = Path(output_dir).resolve()
    if not output_dir.exists():
        return {
            "status": "failed",
            "error": f"output dir not found: {output_dir}",
        }

    tools = FileTools(output_dir)
    verifier = SiteVerifier(output_dir)
    budget = TaskBudget(
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
    )

    print(f"[Editor] output={output_dir}")
    print(f"[Editor] budget start: {budget.snapshot()}")

    snapshot = build_snapshot(tools)
    print(f"[Editor] snapshot: {len(snapshot)} символов")

    plan = await plan_edit(
        client=client,
        model=model,
        task=task,
        project_snapshot=snapshot,
        budget=budget,
    )
    if plan["status"] != "ok":
        return {
            "status": "failed",
            "error": f"план: {plan['error']}",
            "plan": plan,
        }

    if not plan["operations"]:
        print(
            "\n[Editor] план пустой — менять нечего, "
            "перехожу к финальной проверке"
        )
        operations_results = []
    else:
        operations_results = await execute_operations_grouped(
            client, model, tools, verifier, budget,
            plan["operations"],
        )

    check = await check_content(
        client=client,
        model=model,
        task=task,
        output_dir=output_dir,
        budget=budget,
    )

    verify = verifier.verify_site()

    print(f"\n[Editor] budget end: {budget.snapshot()}")
    print(f"[Editor] контент-чек: {check['status']}")
    print(f"[Verifier] status: {verify['status']}")

    return {
        "status": verify["status"],
        "plan": plan,
        "operations": operations_results,
        "check": check,
        "verification": verify,
    }


async def async_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--output", default="output")
    parser.add_argument("--max-model-calls", type=int, default=20)
    parser.add_argument("--max-tool-calls", type=int, default=20)
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    api_key = os.getenv("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL")
    if not api_key or not model:
        print("Укажи GROQ_API_KEY и GROQ_MODEL в .env.")
        return 1

    async with AsyncGroq(
        api_key=api_key, timeout=60.0, max_retries=0,
    ) as raw_client:
        client = AsyncRetryingGroq(raw_client)

        try:
            result = await edit_site(
                client, model, args.task, args.output,
                max_model_calls=args.max_model_calls,
                max_tool_calls=args.max_tool_calls,
            )
        except (APIError, RuntimeError) as e:
            print(f"\nОстановлено: {e}")
            return 1

        print(f"\n=== Итог ===")
        print(f"статус: {result['status']}")
        if "check" in result:
            print(f"контент-чек: {result['check']['status']}")
        if client.rate_limit_waits:
            print(
                f"rate-limit waits: {client.rate_limit_waits}"
            )

    return 0


def main() -> None:
    exit_code = asyncio.run(async_main())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()