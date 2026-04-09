"""Telegram bot inline keyboards."""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


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
