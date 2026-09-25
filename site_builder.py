import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from groq import APIError, Groq

from agent_writer import run_writer
from budget import TaskBudget
from content_checker import check_content
from retry_client import RetryingGroq
from tools import FileTools
from verifier import SiteVerifier


def build_site(
    client,
    model: str,
    task: str,
    output_dir: str | Path,
    max_model_calls: int = 14,
    max_tool_calls: int = 18,
    max_fix_rounds: int = 1,
) -> dict:
    """Создаёт сайт с двухфазной проверкой.

    Фаза 1: writer генерирует файлы.
    Фаза 2: content-checker проверяет полноту.
    Если неполно — фикс-раунд writer'а с фокусом на пробелы.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    tools = FileTools(output_dir)
    verifier = SiteVerifier(output_dir)
    budget = TaskBudget(
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
    )

    print(f"[Builder] output={output_dir}")
    print(f"[Builder] budget start: {budget.snapshot()}")

    writer_rounds: list[dict] = []
    checks: list[dict] = []
    verified_paths: set[str] = set()

    current_task = task

    for round_num in range(max_fix_rounds + 1):
        print(f"\n=== Раунд {round_num + 1} ===")

        writer_result = run_writer(
            client=client,
            model=model,
            task=current_task,
            tools=tools,
            budget=budget,
            verifier=verifier,
            verified_paths=verified_paths,
        )
        writer_rounds.append(writer_result)

        # Обновляем verified_paths из результатов раунда.
        for entry in writer_result["tool_log"]:
            if entry["name"] != "write_file":
                continue
            r = entry["result"]
            if r.get("status") != "ok":
                continue
            v = r.get("verification")
            if v and v.get("status") == "ok":
                verified_paths.add(r["path"])

        print(
            f"[Builder] Раунд {round_num + 1}: "
            f"writer status={writer_result['status']}, "
            f"budget={budget.snapshot()}"
        )

        if writer_result["status"] in ("limit_reached", "failed"):
            print("[Builder] Writer не завершился, "
                  "прекращаю раунды")
            break

        # Фаза контент-чека.
        check = check_content(
            client=client,
            model=model,
            task=task,       # всегда исходная задача
            output_dir=output_dir,
            budget=budget,
        )
        checks.append(check)

        if check["status"] == "ok":
            print("[Builder] Контент-чек OK, задача выполнена")
            break

        if check["status"] != "incomplete":
            print(f"[Builder] Контент-чек status="
                  f"{check['status']}, останавливаюсь")
            break

        if not check["issues"]:
            print("[Builder] INCOMPLETE без списка issues, "
                  "останавливаюсь")
            break

        if round_num >= max_fix_rounds:
            print("[Builder] Лимит фикс-раундов исчерпан")
            break

        # Готовим задачу для фикс-раунда.
        issues_list = "\n".join(
            f"- {issue}" for issue in check["issues"]
        )

        # Извлекаем имена файлов из issues. Для них снимаем
        # флаг verified, чтобы модель могла их перезаписать.
        # Без этого writer получит SKIP и зациклится.
        import re
        mentioned_paths: set[str] = set()
        for issue in check["issues"]:
            for match in re.finditer(
                r"[a-zA-Z0-9_\-/]+\.(html|htm|css)", issue,
            ):
                mentioned_paths.add(match.group(0))

        for path in mentioned_paths:
            if path in verified_paths:
                verified_paths.discard(path)
                print(
                    f"[Builder] Снимаю verified с {path} "
                    f"(упомянут в issues)"
                )

        current_task = (
            f"Исходная задача:\n{task}\n\n"
            f"Файлы созданы, но не выполнены следующие "
            f"требования:\n{issues_list}\n\n"
            f"Порядок исправления:\n"
            f"1. read_file для файла, где есть проблема.\n"
            f"2. edit_file — точечная замена. НЕ write_file.\n"
            f"3. Если правок несколько — делай их "
            f"последовательно через edit_file.\n"
            f"Не переписывай файлы целиком."
        )
        print(
            f"[Builder] Запускаю фикс-раунд с "
            f"{len(check['issues'])} пунктами"
        )

    # Финальная проверка.
    verification = verifier.verify_site()

    print(f"\n[Builder] budget end: {budget.snapshot()}")
    print(f"[Verifier] status: {verification['status']}")
    if verification["errors"]:
        print("[Verifier] errors:")
        for err in verification["errors"]:
            print(f"  - {err}")
    for fname, fdata in verification["files"].items():
        print(
            f"  {fname}: {fdata['status']} "
            f"({fdata['stats'].get('bytes', 0)} bytes)"
        )

    return {
        "writer_rounds": writer_rounds,
        "checks": checks,
        "verification": verification,
        "budget_snapshot": budget.snapshot(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="output",
        help="Куда писать сайт.",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Задача для агента. Если не указана — спросит.",
    )
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=14,
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=18,
    )
    parser.add_argument(
        "--max-fix-rounds",
        type=int,
        default=1,
        help="Сколько раз пытаться исправить пробелы.",
    )
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")

    api_key = os.getenv("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL")

    if not api_key or not model:
        print("Укажи GROQ_API_KEY и GROQ_MODEL в .env.")
        return

    task = args.task
    if not task:
        print("Опиши задачу (Enter после ввода):")
        task = input("\n> ").strip()

    if not task:
        print("Пустая задача.")
        return

    with Groq(
        api_key=api_key,
        timeout=60.0,
        max_retries=0,
    ) as raw_client:
        client = RetryingGroq(raw_client)

        try:
            result = build_site(
                client=client,
                model=model,
                task=task,
                output_dir=args.output,
                max_model_calls=args.max_model_calls,
                max_tool_calls=args.max_tool_calls,
                max_fix_rounds=args.max_fix_rounds,
            )
        except (APIError, RuntimeError) as error:
            print(f"\nВыполнение остановлено: {error}")
            return

        v = result["verification"]
        rounds = result["writer_rounds"]
        checks = result["checks"]

        print("\n=== Итог ===")
        print(f"раундов writer:  {len(rounds)}")
        print(f"контент-чеков:   {len(checks)}")

        for i, c in enumerate(checks, 1):
            print(f"  чек {i}: {c['status']}")

        html_count = sum(
            1 for f in v.get("files", {})
            if f.endswith((".html", ".htm"))
        )
        css_count = sum(
            1 for f in v.get("files", {})
            if f.endswith(".css")
        )
        print(f"files:           {html_count} html, {css_count} css")
        print(f"verifier status: {v['status']}")

        if client.rate_limit_waits:
            print(
                f"rate-limit waits: {client.rate_limit_waits}"
            )

        if v["status"] != "ok":
            print("\n⚠ Сайт не полностью валиден.")


if __name__ == "__main__":
    main()