"""
Research Agent — subagent for SBA 2.0.

Used inside Main Agent (agent.py) as an AgentDefinition.
Provides:
  - search_personal_knowledge — FTS5 search in local DB
  - WebSearch, WebFetch — internet search (built-in SDK tools)

The FTS5 tool needs DB access; set via setup_research(db) before first use.
"""

import logging
from typing import Any, Optional

from claude_agent_sdk import AgentDefinition, tool, create_sdk_mcp_server

from sba.db import Database

logger = logging.getLogger(__name__)

_db: Optional[Database] = None


def setup_research(db: Database) -> None:
    """Inject DB reference for the FTS5 search tool."""
    global _db
    _db = db


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


RESEARCH_SYSTEM_PROMPT = """Ты — исследовательский агент. Найди информацию по запросу.

Используй:
1. search_personal_knowledge — сначала поиск в личной базе знаний
2. WebSearch — если нужно найти в интернете
3. WebFetch — для детального изучения страниц

Отвечай на русском. Формат: краткий синтез + ключевые факты + источники.
Если WebSearch недоступен — ответь только из личной базы с пометкой об ограничении."""


@tool("search_personal_knowledge", "Поиск в личной базе знаний (Drive + Notes).", {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer", "default": 5},
    },
    "required": ["query"],
})
async def search_personal_knowledge_tool(args: dict[str, Any]) -> dict[str, Any]:
    if not _db:
        return _ok("База знаний недоступна")
    results = await _db.search_fts(args.get("query", ""), int(args.get("limit", 5)))
    if not results:
        return _ok("В личной базе знаний ничего не найдено.")
    lines = [f"• {r['title']} [{r['source_type']}] — {r.get('snippet', '')}" for r in results]
    return _ok("Результаты из личной базы:\n" + "\n".join(lines))


research_mcp_server = create_sdk_mcp_server(
    name="research_tools",
    tools=[search_personal_knowledge_tool],
)

research_agent_definition = AgentDefinition(
    description="Исследовательский агент: поиск в интернете и личной базе знаний.",
    prompt=RESEARCH_SYSTEM_PROMPT,
    tools=["WebSearch", "WebFetch", "mcp__research_tools__search_personal_knowledge"],
)
