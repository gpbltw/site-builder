# Site Builder Harness

Учебная платформа для генерации и редактирования статических сайтов
через LLM-агентов. Построена вокруг **слабой модели** (qwen3.8-27b на
Groq free tier) и компенсирует её дефекты на уровне harness, а не
промптов.

Главная проблема, которую решает проект: qwen3.8 не может сгенерировать
большой HTML-документ за один вызов — обрывается на 300–400 токенах,
теряет закрывающие теги, соскакивает на другие языки. Решение —
**разбить задачу на маленькие генерации**, каждая из которых физически
не может обрезаться.

## Три режима работы

| Режим | Задача | Как работает |
|---|---|---|
| `site_builder.py` | Создать сайт с нуля | writer-agent + content-check + fix-round |
| `site_editor.py` | Изменить существующий сайт | planner → executor → check |
| `site_expander.py` | Массовая заливка секций | JSON-конфиг + генерация + вставка |

### `site_builder` — создание сайта

```
Задача → writer-agent (write_file/edit_file/read_file)
         ↓
       content-checker (полнота)
         ↓
    OK? ─── да → финал
     │
     нет
     ↓
   fix-round: снимаем verified с проблемных файлов,
              writer правит через edit_file
```

### `site_editor` — редактирование

```
Задача на естественном языке
         ↓
   PLANNER (1 model call):
   видит снимок проекта, планирует 1-4 операции
         ↓
   EXECUTOR (N model calls):
   insert_section | append_file | edit_file | write_file
   каждая операция — маленькая генерация
         ↓
   CHECKER (1 model call):
   подтверждает полноту
```

**Пример плана для задачи «добавь FAQ и переведи h1»:**

```json
{
  "operations": [
    {"op": "edit_file", "file": "index.html",
     "instruction": "Заменить h1 на 'Agentic Systems'"},
    {"op": "insert_section", "file": "index.html",
     "description": "Секция FAQ: h2 + 3 пары dt/dd"}
  ]
}
```

### `site_expander` — batch

Устаревший режим, оставлен как пример. Использует JSON-конфиг со
списком секций. `site_editor` покрывает его функции.

## Компоненты

| Файл | Роль |
|---|---|
| `tools.py` | Файловые операции с изоляцией пути |
| `verifier.py` | Структурная проверка HTML и CSS |
| `content_checker.py` | LLM-проверка полноты (соответствие задаче) |
| `planner.py` | Планирование операций по задаче |
| `section_writer.py` | Генерация одной HTML-секции |
| `agent_writer.py` | Writer-agent с tool calling (для builder) |
| `site_builder.py` | Оркестратор создания сайта |
| `site_editor.py` | Оркестратор редактирования |
| `site_expander.py` | Массовая заливка по конфигу |
| `budget.py` | `TaskBudget` — общий счётчик вызовов |
| `retry_client.py` | Retry при 429 с задержкой из ответа Groq |

## Инструменты

Все операции над файлами изолированы в `output/`. Модель не может
выйти за пределы этой директории.

| Tool | Требует `old` | Когда использовать |
|---|---|---|
| `write_file(path, content)` | Нет | Создать файл или перезаписать целиком |
| `append_file(path, content)` | Нет | Добавить в конец файла |
| `edit_file(path, old, new)` | Да | Точечная замена |
| `edit_file_many(path, edits)` | Да, список | Несколько замен атомарно |
| `read_file(path)` | — | Прочитать |
| `list_files()` | — | Список файлов |

**Ограничения:**
- Не более 50 KB на файл;
- Для `.html` минимальный размер 120 B, для `.css` — 60 B
  (защита от обрезков);
- Запрещены абсолютные пути, `..`, drive letters, colon.

**Атомарность `edit_file_many`:** если хоть одна замена не находит
`old` — не применяется ничего. Файл не остаётся в половинчатом
состоянии.

## Верификация

### Структурная (`verifier.py`)

**HTML:**
- `<title>` есть — error;
- `<h1>` есть — warning;
- `</body>` и `</html>` есть — error;
- `<a href>` ведут на существующие файлы — error;
- `<link>/<script>/<img>` missing — warning.

**CSS:**
- Баланс `{` и `}`;
- Не заканчивается на `:`/`,`/`{`;
- Нет обрезанного hex-цвета (`#e6e` вместо `#e6e6e6`);
- Нет SCSS-функций (`lighten`, `darken`, `mix`, ...) — в чистом CSS
  их не существует.

**Различие page/resource ссылок:** `index.html` может ссылаться на
`about.html`, которого ещё нет — это транзиент, не ошибка.

### Содержательная (`content_checker.py`)

Отдельный model call. Модель получает задачу и содержимое всех файлов
проекта (начало + конец, если файл большой). Возвращает:

```
STATUS: OK
```

или

```
STATUS: INCOMPLETE
MISSING
1. В файле index.html отсутствует ссылка на about.html
2. В файле about.html секция team пуста
```

## Защиты от дефектов LLM

Каждый механизм появился как ответ на конкретный сбой.

| Дефект | Защита |
|---|---|
| Модель обрывает HTML на середине | Auto-close `</body></html>` |
| Модель генерирует 28-байтовый мусор | `MIN_SIZES` в `write_file` |
| Модель вставляет SCSS-функции в CSS | Forbidden functions detector |
| Модель неточно копирует `old` в edit | Retry с обратной связью |
| Модель зацикливается на одной ошибке | SPIRAL detector |
| Модель повторяет одну правку | `max_edits_per_path=5` |
| Модель соскакивает на другой язык | Детектор кириллицы + retry |
| Модель оборачивает HTML в ` ```html ` | `_strip_code_fence` |
| Модель выбирает `insert_section` для CSS | Раннее отклонение в executor |

## Метрики реальных прогонов

### `site_builder`: 3 прогона двухстраничного сайта

| Метрика | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| `model_calls` | 3 | 5 | 8 |
| Auto-close HTML | 0 | 2 | 2 |
| ContentCheck | ok | ok | ok |
| Итог | **ok** | **ok** | **ok** |

**Успех: 3/3.**

### `site_editor`: визуализация

Задача — превратить плоский сайт в современный дизайн через серию
правок на естественном языке.

| Задача | model_calls | tool_calls | ContentCheck |
|---|---:|---:|---|
| Переписать CSS-фундамент | 3 | 1 | ok (после фикса) |
| Стили section + header | 3 | 1 | ok |
| Features через grid (естественный язык) | 3 | 1 | ok |
| Features grid + remove flex | 3 | 1 | ok |
| Декоративный фон (5 фигур) | 6 | 4 | ok |
| Добавить shape-5 | 3 | 1 | ok |
| Удалить дубликат shape-5 | 3 | 1 | ok |
| Intro banner | 3 | 1 | ok |

**Средняя стоимость правки: 3 model calls, 1 tool call.**

### Пример: задача на естественном языке

```
"Мне кажется, сайт выглядит слишком плоским. Хочу, чтобы 
карточки преимуществ на главной выглядели как современные 
плитки: встали в один ряд на широком экране, но переносились 
на узком. У каждой плитки должно быть лёгкое свечение и 
заметная, но не кричащая граница. И чтобы при наведении на 
плитку было приятное ощущение — не резкий скачок, а плавное 
изменение. Заголовок раздела хочу на всю ширину плиток сверху."
```

Модель перевела это в:

```css
.features{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  gap:24px;
}
.features h2{grid-column:1/-1;margin-bottom:16px;text-align:left}
.features article{
  background:linear-gradient(145deg,#14141f,#0a0a14);
  border:1px solid rgba(124,154,255,0.3);
  box-shadow:0 0 15px rgba(124,154,255,0.1);
  border-radius:12px;
  padding:24px;
  transition:all 0.3s ease-in-out;
}
.features article:hover{
  transform:translateY(-4px);
  box-shadow:0 0 25px rgba(124,154,255,0.25);
  border-color:rgba(124,154,255,0.5);
}
```

**Ни одной SCSS-функции, современные практики, 3 model calls.**

## Ограничения

### Модели

- **OTPM = 1000 токенов/мин** на free tier Groq;
- один вызов `max_completion_tokens` не может превышать ~1000;
- qwen3.8 иногда вставляет символы из других алфавитов;
- иногда обрывается **без** `finish_reason=length`.

### Harness

- только `output/` для записи;
- нет выполнения команд, только файловые операции;
- нет multi-file edit (правки по одному файлу за раз);
- verifier не проверяет баланс вложенных тегов (`<main>` может
  остаться открытым — браузеры рендерят, но HTML формально невалиден);
- не проверяется семантика контента (только наличие требуемых
  элементов);
- нет детектора дубликатов CSS-селекторов (обсуждался, не реализован).

### Planner

- жёсткий лимит 4 операций на задачу;
- не понимает задач, требующих циклов;
- может сузить задачу, если instruction расплывчат.

## Философия

**Модель предлагает — harness решает и выполняет.**

LLM не имеет доступа к файловой системе. Всё, что она делает —
генерирует `tool_calls` с аргументами. Harness проверяет аргументы,
разрешает или отклоняет, выполняет безопасную операцию, возвращает
результат модели.

Три следствия:

1. **Предсказуемость.** Никаких скрытых действий.
2. **Восстанавливаемость.** Дефекты LLM ловятся на уровне harness.
3. **Измеримость.** `model_calls`, `tool_calls`, `rate-limit waits` —
   данные для сравнения архитектур.

**Главный принцип:** harness должен быть честным. Если задача не
выполнена — он говорит «не выполнена», а не выдаёт ложный success.

## Запуск

### Установка

```bash
python -m venv .venv
.venv\Scripts\activate.bat        # Windows
pip install -r requirements.txt
```

### `.env`

```dotenv
GROQ_API_KEY=твой_ключ
GROQ_MODEL=qwen/qwen3.8-27b
```

### Создать сайт

```bash
python site_builder.py --task "Создай двухстраничный сайт про агентные системы..."
```

### Изменить сайт

```bash
python site_editor.py --task "Добавь FAQ из трёх вопросов и переведи h1"
```

### С параметрами

```bash
python site_editor.py \
  --task "..." \
  --output output \
  --max-model-calls 20 \
  --max-tool-calls 20
```

## Тесты

```
25 passed
```

Покрытие:

- `test_tools.py` (10) — path traversal, oversized content,
  truncated HTML/CSS, `edit_file_many` атомарность, list_files;
- `test_verifier.py` (9) — valid HTML, missing title/h1, broken page
  link, external links, verify_site, truncated document, SCSS-функции;
- `test_section_writer.py` (5) — ok, full-HTML reject, empty,
  markdown-fence strip, budget exhausted;

Все тесты работают **без обращений к Groq**.


## Структура

```
site-builder/
├── output/                    # сгенерированный сайт (gitignored)
│   ├── index.html
│   ├── about.html
│   └── css/style.css
├── tools.py
├── verifier.py
├── content_checker.py
├── section_writer.py
├── planner.py
├── agent_writer.py
├── site_builder.py
├── site_editor.py
├── site_expander.py
├── budget.py
├── retry_client.py
├── test_tools.py
├── test_verifier.py
├── test_section_writer.py
├── expand_config.json
├── requirements.txt
├── .env
├── .env.example
└── .gitignore
```