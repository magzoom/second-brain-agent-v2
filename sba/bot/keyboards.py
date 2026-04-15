"""Telegram bot inline keyboards."""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

CATEGORIES = [
    "1_Health_Energy", "2_Business_Career", "3_Finance",
    "4_Family_Relationships", "5_Personal Growth", "6_Brightness life", "7_Spirituality",
]

CATEGORY_LABELS = {
    "1_Health_Energy": "🏋 Здоровье",
    "2_Business_Career": "💼 Карьера",
    "3_Finance": "💰 Финансы",
    "4_Family_Relationships": "👨‍👩‍👧 Семья",
    "5_Personal Growth": "📚 Рост",
    "6_Brightness life": "✨ Яркость",
    "7_Spirituality": "🕌 Духовность",
}


def inbox_suggest_keyboard(reg_id: int, category: str) -> InlineKeyboardMarkup:
    """[✅ Suggested Category] [📂 Другая] [🗑 Удалить]"""
    label = CATEGORY_LABELS.get(category, category)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ {label}", callback_data=f"inbox_ok:{reg_id}"),
        InlineKeyboardButton(text="📂 Другая", callback_data=f"inbox_other:{reg_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"inbox_del:{reg_id}"),
    ]])


def inbox_all_categories_keyboard(reg_id: int) -> InlineKeyboardMarkup:
    """All 7 categories in rows of 2 + Delete button."""
    rows = []
    row = []
    for cat in CATEGORIES:
        label = CATEGORY_LABELS[cat]
        row.append(InlineKeyboardButton(text=label, callback_data=f"inbox_pick:{reg_id}:{cat}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"inbox_del:{reg_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def folder_decision_keyboard(reg_id: int, has_subfolders: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_subfolders:
        buttons.append(InlineKeyboardButton(text="📂 Глубже", callback_data=f"folder_deep:{reg_id}"))
    buttons.append(InlineKeyboardButton(text="📝 Саммари", callback_data=f"folder_summary:{reg_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def recurring_check_keyboard(recurring_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, оплачено", callback_data=f"recur_paid:{recurring_id}"),
        InlineKeyboardButton(text="❌ Нет, не оплачено", callback_data=f"recur_unpaid:{recurring_id}"),
    ]])


def confirm_delete_keyboard(deletion_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Удалить", callback_data=f"confirm_del:{deletion_id}"),
        InlineKeyboardButton(text="❌ Оставить", callback_data=f"cancel_del:{deletion_id}"),
    ]])
