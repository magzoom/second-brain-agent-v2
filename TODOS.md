# SBA — TODO / Идеи

## Готово ✅

- [x] USD-поддержка для регулярных платежей (v2.2)
- [x] `action=update` для `finance_manage_recurring` (v2.2)
- [x] Авто-отметка оплаченных при записи расхода и импорте выписки (v2.2)
- [x] Напоминания за 2 дня вперёд (`days_ahead=2`) (v2.2)
- [x] Ежедневные платежи: не показывать если уже оплачены сегодня (v2.2)

## Следующие задачи

### Голосовой ввод (средний приоритет)
- [ ] Добавить обработку `F.voice` в `handlers.py`
- [ ] Рефактор: вынести логику агента в `_run_agent()` (единая функция для text и voice)
- [ ] Новый хэндлер `handle_voice_input`: OGG → mlx-whisper → text → агент
- [ ] Добавить `mlx-whisper` в `requirements.txt` и `setup.py`
- [ ] Проверить PATH в launchd plist (ffmpeg нужен полный путь)
- Детали: `ideas/voice-input.md`

### Smart Home Hub (низкий приоритет, требует устройств)
- [ ] Home Assistant нативно (Python venv, без Docker)
- [ ] Интеграция Tuya (шторы), SmartThings (кондиционер, TV), Яндекс (Станция)
- [ ] MCP сервер для Home Assistant → управление из Claude Code
- [ ] Инструмент `smarthome_control` в `agent.py` → управление из Telegram
- [ ] `com.smarthome.ha` launchd демон
- Детали: `ideas/smart-home-hub.md`

### Wiki-слой (низкий приоритет)
- [ ] Новый инструмент `update_wiki_page(topic, content)` в `agent.py`
- [ ] Новая таблица `wiki_pages` в БД или хранение в Google Drive (`_sba_wiki/`)
- [ ] Расширить `inbox_processor`: после классификации → обновить wiki-страницу по теме
- [ ] Lint-команда (`sba wiki-lint`): проверка противоречий и устаревших данных раз в месяц
- Детали: `ideas/wiki-layer-idea.md`
