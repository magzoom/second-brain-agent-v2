"""
Applies auto-generated tool code to sba/agent.py.
Patches three locations: function definition, tools=[] list, allowed_tools=[].
Called when user approves a propose_tool_addition request.
"""
import subprocess
import sys
from pathlib import Path

_AGENT_PY = Path(__file__).parent / "agent.py"
_BACKUP_PY = _AGENT_PY.with_suffix(".py.bak")

# Stable markers for the three insertion points
_MARKER_FN = "\n_main_server = create_sdk_mcp_server("
_MARKER_TOOLS = "        _propose_extension_tool,\n    ],"
_MARKER_ALLOWED = '            "mcp__sba__propose_capability_extension",'


def apply_pending_tool(tool_name: str, tool_fn_name: str, tool_code: str) -> tuple[bool, str]:
    """
    Patches agent.py to register a new tool.
    Returns (success, error_message).
    Rolls back automatically on any failure.
    """
    content = _AGENT_PY.read_text(encoding="utf-8")

    # 1. Validate syntax of generated code
    try:
        compile(tool_code, "<tool_code>", "exec")
    except SyntaxError as e:
        return False, f"Синтаксическая ошибка в коде инструмента: {e}"

    # 2. Check not already present
    if tool_fn_name in content:
        return False, f"Функция {tool_fn_name} уже существует в agent.py"

    # 3. Verify all markers exist before touching file
    for marker, label in [
        (_MARKER_FN, "_main_server"),
        (_MARKER_TOOLS, "tools=[]"),
        (_MARKER_ALLOWED, "allowed_tools"),
    ]:
        if marker not in content:
            return False, f"Не найдена точка вставки: {label}"

    # 4. Backup
    _BACKUP_PY.write_text(content, encoding="utf-8")

    try:
        new_content = content

        # Insert function code before _main_server
        new_content = new_content.replace(
            _MARKER_FN,
            f"\n\n{tool_code.strip()}\n{_MARKER_FN}",
        )

        # Add function reference to tools=[]
        new_content = new_content.replace(
            _MARKER_TOOLS,
            f"        _propose_extension_tool,\n        {tool_fn_name},\n    ],",
        )

        # Add mcp tool name to allowed_tools
        new_content = new_content.replace(
            _MARKER_ALLOWED,
            f'            "mcp__sba__propose_capability_extension",\n            "mcp__sba__{tool_name}",',
        )

        # Write patched file
        _AGENT_PY.write_text(new_content, encoding="utf-8")

        # Validate import
        result = subprocess.run(
            [sys.executable, "-c", "from sba import agent"],
            capture_output=True, text=True,
            cwd=_AGENT_PY.parent.parent,
        )
        if result.returncode != 0:
            _rollback(content)
            return False, f"Ошибка импорта после патча:\n{result.stderr[:400]}"

        _BACKUP_PY.unlink(missing_ok=True)
        return True, "ok"

    except Exception as e:
        _rollback(content)
        return False, f"Неожиданная ошибка: {e}"


def _rollback(original_content: str) -> None:
    try:
        _AGENT_PY.write_text(original_content, encoding="utf-8")
        _BACKUP_PY.unlink(missing_ok=True)
    except Exception:
        pass
