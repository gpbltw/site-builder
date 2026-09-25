import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from groq import APIError, Groq

from budget import TaskBudget
from retry_client import RetryingGroq
from section_writer import generate_section
from tools import FileTools
from verifier import SiteVerifier


INSERT_MARKER = "<!-- INSERT-HERE -->"


@dataclass
class SectionTask:
    page: str
    name: str
    description: str


def ensure_marker(
    tools: FileTools,
    page_path: str,
) -> dict:
    """Гарантирует наличие маркера вставки.

    Маркер вставляется перед </main>, если он есть.
    Иначе — перед </body>.
    """
    read = tools.read_file(page_path)
    if "error" in read:
        return {"error": read["error"]}

    content = read["content"]

    if INSERT_MARKER in content:
        return {"status": "already_present"}

    if "</main>" in content:
        old = "</main>"
        new = f"{INSERT_MARKER}\n</main>"
    elif "</body>" in content:
        old = "</body>"
        new = f"{INSERT_MARKER}\n</body>"
    else:
        return {
            "error": (
                f"нет </main> или </body> в {page_path}, "
                f"некуда вставлять маркер"
            ),
        }

    return tools.edit_file(page_path, old, new)


def insert_section(
    tools: FileTools,
    verifier: SiteVerifier,
    budget: TaskBudget,
    page_path: str,
    html_fragment: str,
) -> dict:
    """Заменяет маркер на fragment + маркер."""
    if not html_fragment.strip():
        return {"error": "empty fragment"}

    # Списываем один tool call за edit.
    if not budget.try_consume_tool_calls(1):
        return {
            "error": (
                "Tool budget исчерпан до вставки. "
                f"{budget.snapshot()}"
            ),
        }

    old = INSERT_MARKER
    new = f"{html_fragment.strip()}\n{INSERT_MARKER}"

    result = tools.edit_file(page_path, old, new)

    if "error" in result:
        return result

    if page_path.endswith((".html", ".htm")):
        result["verification"] = verifier.verify_html(page_path)

    return result


def expand_site(
    client,
    model: str,
    tasks: list[SectionTask],
    output_dir: str | Path,
    max_model_calls: int = 20,
) -> dict:
    output_dir = Path(output_dir).resolve()
    tools = FileTools(output_dir)
    verifier = SiteVerifier(output_dir)
    budget = TaskBudget(
        max_model_calls=max_model_calls,
        max_tool_calls=len(tasks) * 2 + 4,
    )

    print(f"[Expander] output={output_dir}")
    print(f"[Expander] sections={len(tasks)}")
    print(f"[Expander] budget start: {budget.snapshot()}")

    # Phase 1: маркеры на всех уникальных страницах.
    unique_pages = list({t.page for t in tasks})
    for page in unique_pages:
        r = ensure_marker(tools, page)
        if "error" in r:
            print(f"[Expander] ERROR на {page}: {r['error']}")
            return {
                "status": "failed",
                "error": f"marker setup failed for {page}: {r['error']}",
            }
        status = r.get("status", "ok")
        print(f"[Expander] маркер на {page}: {status}")

    # Phase 2: сгенерировать и вставить каждую секцию.
    results: list[dict] = []

    for task in tasks:
        gen = generate_section(
            client=client,
            model=model,
            section_name=task.name,
            description=task.description,
            budget=budget,
        )
        entry = {"task": task, "generation": gen}

        if gen["status"] != "ok":
            print(
                f"[Expander] секция {task.name} не сгенерирована: "
                f"{gen['error']}"
            )
            results.append(entry)
            continue

        if len(gen["html"]) < 30:
            print(
                f"[Expander] секция {task.name} слишком короткая "
                f"({len(gen['html'])} байт), пропускаю"
            )
            gen["status"] = "too_short"
            results.append(entry)
            continue

        insert = insert_section(
            tools, verifier, budget, task.page, gen["html"],
        )
        entry["insert"] = insert

        if "error" in insert:
            print(
                f"[Expander] не вставил {task.name}: "
                f"{insert['error']}"
            )
        else:
            v = insert.get("verification", {})
            print(
                f"[Expander] {task.name} → {task.page} "
                f"({v.get('status', '?')})"
            )

        results.append(entry)

    # Убираем маркер из всех страниц — он был служебным.
    for page in unique_pages:
        read = tools.read_file(page)
        if "error" in read:
            continue
        if INSERT_MARKER not in read["content"]:
            continue
        # Маркер с окружающими пустыми строками.
        cleanup = tools.edit_file(
            page,
            f"\n{INSERT_MARKER}",
            "",
        )
        if "error" not in cleanup:
            print(f"[Expander] убрал маркер из {page}")
        else:
            # Попробуем без ведущего \n.
            cleanup = tools.edit_file(
                page, INSERT_MARKER, "",
            )
            if "error" in cleanup:
                print(
                    f"[Expander] не смог убрать маркер "
                    f"из {page}: {cleanup['error']}"
                )

    # Финальная проверка сайта.
    site = verifier.verify_site()

    print(f"\n[Expander] budget end: {budget.snapshot()}")
    print(f"[Verifier] status: {site['status']}")
    for fname, fdata in site["files"].items():
        print(
            f"  {fname}: {fdata['status']} "
            f"({fdata['stats'].get('bytes', 0)} bytes)"
        )

    return {
        "status": site["status"],
        "results": results,
        "verification": site,
        "budget": budget.snapshot(),
    }


def load_config(path: str | Path) -> list[SectionTask]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    tasks = []
    for item in data.get("sections", []):
        tasks.append(SectionTask(
            page=item["page"],
            name=item["name"],
            description=item["description"],
        ))
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        required=True,
        help="JSON-файл со списком секций.",
    )
    parser.add_argument("--output", default="output")
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=20,
    )
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    api_key = os.getenv("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL")
    if not api_key or not model:
        print("Укажи GROQ_API_KEY и GROQ_MODEL в .env.")
        return

    tasks = load_config(args.config)
    if not tasks:
        print("Конфиг пуст.")
        return

    with Groq(
        api_key=api_key, timeout=60.0, max_retries=0,
    ) as raw:
        client = RetryingGroq(raw)
        try:
            result = expand_site(
                client, model, tasks, args.output,
                max_model_calls=args.max_model_calls,
            )
        except (APIError, RuntimeError) as e:
            print(f"\nОстановлено: {e}")
            return

        print(f"\nИтог: {result['status']}")
        if client.rate_limit_waits:
            print(
                f"rate-limit waits: {client.rate_limit_waits}"
            )


if __name__ == "__main__":
    main()