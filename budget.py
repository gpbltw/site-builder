DEFAULT_MAX_MODEL_CALLS = 6
DEFAULT_MAX_TOOL_CALLS = 6


class TaskBudget:
    """Общий бюджет одной задачи.

    Все агенты одной задачи получают один и тот же экземпляр.
    Это гарантирует, что суммарный расход не превысит лимит,
    независимо от того, сколько агентов будет запущено.
    """

    def __init__(
        self,
        max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ):
        if max_model_calls < 1:
            raise ValueError("max_model_calls must be >= 1")
        if max_tool_calls < 0:
            raise ValueError("max_tool_calls must be >= 0")

        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.used_model_calls = 0
        self.used_tool_calls = 0

    @property
    def model_calls_remaining(self) -> int:
        return self.max_model_calls - self.used_model_calls

    @property
    def tool_calls_remaining(self) -> int:
        return self.max_tool_calls - self.used_tool_calls

    def try_consume_model_call(self) -> bool:
        """Пытается зарезервировать одно обращение к модели.

        Возвращает True при успехе и False, если бюджет исчерпан.
        Успешный вызов сразу увеличивает счётчик — отката нет.
        """
        if self.used_model_calls >= self.max_model_calls:
            return False
        self.used_model_calls += 1
        return True

    def try_consume_tool_calls(self, count: int) -> bool:
        """Пытается зарезервировать count вызовов инструментов атомарно.

        Если весь запрошенный набор не помещается в остаток,
        не расходуется ничего. Это защищает от частично
        выполненного набора tool calls.
        """
        if count < 0:
            raise ValueError("count must be >= 0")
        if self.used_tool_calls + count > self.max_tool_calls:
            return False
        self.used_tool_calls += count
        return True

    def snapshot(self) -> str:
        return (
            f"model {self.used_model_calls}/{self.max_model_calls}, "
            f"tool {self.used_tool_calls}/{self.max_tool_calls}"
        )