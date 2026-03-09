"""
Deduplication script: removes duplicate tasks in Google Tasks.
Keeps the first task by title, deletes the rest.
Run: .venv/bin/python dedup_tasks.py
"""

import sys
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sba.integrations.google_tasks import build_service

config = yaml.safe_load(open(Path.home() / ".sba/config.yaml"))
service = build_service(config)

lists = service.tasklists().list(maxResults=100).execute().get("items", [])

total_deleted = 0

for tl in lists:
    list_id = tl["id"]
    list_name = tl["title"]

    tasks = service.tasks().list(
        tasklist=list_id,
        showCompleted=False,
        showHidden=False,
        maxResults=500,
    ).execute().get("items", [])

    seen = {}
    to_delete = []
    for t in tasks:
        title = t["title"].strip()
        if title in seen:
            to_delete.append(t["id"])
        else:
            seen[title] = t["id"]

    if to_delete:
        print(f"[{list_name}] найдено {len(to_delete)} дубликатов — удаляю...")
        for task_id in to_delete:
            service.tasks().delete(tasklist=list_id, task=task_id).execute()
            total_deleted += 1
    else:
        print(f"[{list_name}] дубликатов нет")

print(f"\nГотово. Удалено {total_deleted} дубликатов.")
