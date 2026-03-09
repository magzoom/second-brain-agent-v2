"""
Migration script: Apple Reminders → Google Tasks

Run once from the project root:
    .venv/bin/python migrate_reminders.py

Reads all incomplete tasks from Apple Reminders category lists
and creates them in Google Tasks under the same list names.
"""

import subprocess
import sys
import yaml
from pathlib import Path


CONFIG_PATH = Path.home() / ".sba" / "config.yaml"

CATEGORY_LISTS = [
    "1_Health_Energy",
    "2_Business_Career",
    "3_Finance",
    "4_Family_Relationships",
    "5_Personal Growth",
    "6_Brightness life",
    "7_Spirituality",
]


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Config not found: {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_apple_reminders_tasks() -> list[dict]:
    """Read all incomplete tasks from Apple Reminders via JXA."""
    list_names_json = str(CATEGORY_LISTS).replace("'", '"')
    jxa = f"""
var app = Application('Reminders');
var targetLists = {list_names_json};
var output = [];
targetLists.forEach(function(listName) {{
    try {{
        var matched = app.lists.whose({{name: listName}});
        if (matched.length === 0) return;
        var incomplete = matched[0].reminders.whose({{completed: false}});
        var names = incomplete.name();
        var bodies = incomplete.body();
        var dueDates = incomplete.dueDate();
        for (var i = 0; i < names.length; i++) {{
            var dueStr = '';
            try {{
                if (dueDates[i] !== null) {{
                    var d = new Date(dueDates[i]);
                    var y = d.getFullYear();
                    var mo = String(d.getMonth() + 1).padStart(2, '0');
                    var day = String(d.getDate()).padStart(2, '0');
                    var hr = String(d.getHours()).padStart(2, '0');
                    var mn = String(d.getMinutes()).padStart(2, '0');
                    dueStr = y + '-' + mo + '-' + day + 'T' + hr + ':' + mn;
                }}
            }} catch(e) {{}}
            var note = bodies[i] || '';
            output.push(names[i] + '|||' + listName + '|||' + dueStr + '|||' + note.replace(/\\n/g, ' '));
        }}
    }} catch(e) {{}}
}});
output.join('~~~');
"""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(f"JXA error: {result.stderr[:200]}")
        return []

    tasks = []
    for chunk in result.stdout.strip().split("~~~"):
        chunk = chunk.strip()
        if not chunk or "|||" not in chunk:
            continue
        parts = chunk.split("|||")
        if len(parts) < 2:
            continue
        title = parts[0].strip()
        list_name = parts[1].strip()
        due_raw = parts[2].strip() if len(parts) > 2 else ""
        notes = parts[3].strip() if len(parts) > 3 else ""

        due_date = None
        due_time = None
        if due_raw and "T" in due_raw:
            date_part, time_part = due_raw.split("T", 1)
            due_date = date_part
            due_time = time_part[:5]

        tasks.append({
            "title": title,
            "list_name": list_name,
            "due_date": due_date,
            "due_time": due_time,
            "notes": notes or None,
        })

    return tasks


FAILED_FILE = Path(__file__).parent / "migrate_failed.json"


def migrate(config: dict, tasks: list[dict]) -> None:
    import time
    import json as _json

    sys.path.insert(0, str(Path(__file__).parent))
    from sba.integrations.google_tasks import build_service, create_task

    print(f"\nBuilding Google Tasks service...")
    service = build_service(config)
    print("Authenticated.\n")

    ok = 0
    failed = []
    for i, t in enumerate(tasks):
        task_id = create_task(
            service=service,
            title=t["title"],
            list_name=t["list_name"],
            due_date=t["due_date"],
            due_time=t["due_time"],
            notes=t["notes"],
        )
        if task_id:
            print(f"  ✓ [{t['list_name']}] {t['title']}")
            ok += 1
        else:
            print(f"  ✗ [{t['list_name']}] {t['title']} — FAILED")
            failed.append(t)
        # Small delay to avoid rate limiting
        if (i + 1) % 10 == 0:
            time.sleep(1)
        else:
            time.sleep(0.2)

    if failed:
        FAILED_FILE.write_text(_json.dumps(failed, ensure_ascii=False, indent=2))
        print(f"\nFailed tasks saved to {FAILED_FILE}")

    print(f"\nDone: {ok} migrated, {len(failed)} failed.")


if __name__ == "__main__":
    import json as _json

    config = load_config()

    # Retry mode: re-run only previously failed tasks
    if "--retry" in sys.argv and FAILED_FILE.exists():
        tasks = _json.loads(FAILED_FILE.read_text())
        print(f"Retrying {len(tasks)} failed tasks...")
        FAILED_FILE.unlink()
        migrate(config, tasks)
        sys.exit(0)

    print("Reading Apple Reminders...")
    tasks = get_apple_reminders_tasks()
    if not tasks:
        print("No incomplete tasks found in category lists. Nothing to migrate.")
        sys.exit(0)

    print(f"Found {len(tasks)} incomplete tasks:")
    for t in tasks:
        due = f" [{t['due_date']}]" if t["due_date"] else ""
        print(f"  [{t['list_name']}] {t['title']}{due}")

    answer = input(f"\nMigrate all {len(tasks)} tasks to Google Tasks? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    migrate(config, tasks)
