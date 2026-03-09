# Архитектура иерархической индексации Google Drive

## Цель

Legacy процесс обходит папки Drive сверху вниз и спрашивает пользователя через
Telegram что делать с каждой папкой. Решение сохраняется в БД — повторно не
спрашивается. После завершения первичной индексации legacy работает только на
новые файлы.

---

## Алгоритм обхода (сверху вниз)

```
Зашли в папку (с path_stack для breadcrumb)
│
├── Есть подпапки?
│   └── Да → прислать уведомление в Telegram [Глубже] [Саммари]
│       ├── Глубже → записать статус pending_deep, зайти следующим запуском
│       └── Саммари → callback: "⏳ Создаю саммари..."
│                     → asyncio.to_thread: Haiku генерирует + Drive создаёт _sba_summary.md
│                     → "✅ Саммари создан", записать в FTS5, статус folder_summary, стоп
│
└── Нет подпапок — только файлы:
    ├── Все файлы бинарные (.zip, .dmg, .atn) → статус folder_done, стоп
    ├── Есть медиа (.mp4, .mov, .jpg, .png) → сообщить путь без кнопок, текстовые обработать
    └── Текстовые файлы (PDF, docx, Google Docs, txt, md) → читать и индексировать
        ├── До 20 файлов → за один запуск → статус folder_done
        └── Больше 20 → порциями (is_new логика в files_registry) → статус folder_partial
                       → когда все обработаны → folder_done
```

---

## Лимит уведомлений за запуск

**5 папок за один запуск** (настраивается в config.yaml):

```yaml
schedule:
  legacy_folders_per_run: 5
```

Остальные папки без решения ждут следующего запуска в порядке обхода.

---

## Уведомления в Telegram

### Папка с подпапками → кнопки

```
📁 Archive_Career
Путь: 2_Business_Career / Career_work / Archive_Career
Внутри: 13 подпапок, 2 файла
Агент: архивные проекты и места работы 2007-2025

[Глубже] [Саммари]
```

Подсказка агента генерируется Haiku по списку имён файлов и подпапок — без чтения содержимого.

### Папка с медиа → только сообщение, без кнопок

```
📁 [Abushito] Ретушь фотографии
Путь: 5_Personal Growth / На_русском_языке / [Abushito] Ретушь фотографии
Содержит 6 видео (.mp4) — возможно стоит перенести в Google Photos.
PDF (gaid.pdf) проиндексирован.
```

---

## Статусы папок в files_registry

| Статус | Что означает |
|---|---|
| `pending_decision` | Legacy отправил кнопки, ждёт ответа пользователя |
| `pending_deep` | Нажал Глубже — зайти в подпапки следующим запуском |
| `folder_summary` | Нажал Саммари — создан _sba_summary.md, больше не трогать |
| `folder_done` | Все файлы обработаны |
| `folder_partial` | Файлов > 20 — обработка продолжается порциями (курсор через is_new в files_registry) |

Переходы статусов:
```
(нет записи) → pending_decision → pending_deep → (подпапки обрабатываются)
                                → folder_summary
(нет записи) → folder_partial → folder_done
```

Поле `path` хранит breadcrumb строкой: `"2_Business_Career / Career_work / Archive_Career"`.
Поле `depth` — не используется, убрано.

---

## Что создаётся при Саммари

Файл `_sba_summary.md` в самой папке Google Drive:

```markdown
# Archive_Career

Путь: 2_Business_Career / Career_work / Archive_Career
Тип: архив карьеры

## Содержимое
- 13 подпапок: QazIndustry, UNIFUN_2019, НАО ФОМС, ...
- Период: 2007-2025

## Описание
Архив проектов и мест работы. Содержит документацию,
переписку и материалы по каждому периоду карьеры.
```

Этот файл индексируется в FTS5 — поиск находит папку без чтения всех файлов внутри.

**Защита от inbox:** сразу после создания файла в Drive — записать его в
`files_registry` со статусом `processed`. Тогда `upsert_file` вернёт
`is_new=False` и inbox пропустит файл.

---

## Автоматические правила (без вопросов)

| Тип | Действие |
|---|---|
| `.mp4`, `.mov`, `.avi`, `.mkv` | Сообщить путь (возможно Google Photos) |
| `.jpg`, `.jpeg`, `.png`, `.heic` | Сообщить путь (возможно Google Photos) |
| `.zip`, `.dmg`, `.atn`, `.iso` | Пропустить молча |
| `.dicom`, медицинские снимки | Пропустить молча |
| Google Docs, PDF, docx, txt, md | Читать и индексировать |

---

## path_stack

`_walk_folder` принимает `path_stack: list[str]` и передаёт вглубь при рекурсии:

```python
# Вход в категорийную папку:
path_stack = ["2_Business_Career"]

# Рекурсия в подпапку:
path_stack = ["2_Business_Career", "Career_work"]

# Ещё глубже:
path_stack = ["2_Business_Career", "Career_work", "Archive_Career"]
```

Breadcrumb для уведомления: `" / ".join(path_stack)`
Записывается в `files_registry.path`.

---

## Что меняется в коде

| Файл | Что меняется |
|---|---|
| `db.py` | Новые статусы: `pending_deep`, `folder_summary`, `folder_done`, `folder_partial`. Метод `get_pending_deep_folders()`. Метод `get_entry_type()` уже есть |
| `legacy_processor.py` | Новая логика: найти папки `pending_deep` → обойти содержимое → отправить уведомления (лимит 5/запуск). Удалить старый `_walk_folder` с паттернами |
| `bot/handlers.py` | Callback: `folder_deep_{id}`, `folder_summary_{id}` |
| `bot/keyboards.py` | Клавиатура [Глубже] [Саммари] |
| `notifier.py` | `send_folder_decision()`, `send_media_notification()` |
| `integrations/google_drive.py` | `create_summary_file(folder_id, content)` |
| `config.yaml.example` | `legacy_folders_per_run: 5`. Убрать `index_rules` |

## Что убирается

- `index_rules` из config.yaml и config.yaml.example
- `_walk_folder()` с паттернами fnmatch
- `_matches()` функция
- `skip_patterns`, `summary_patterns` из `_process_gdrive_legacy`
