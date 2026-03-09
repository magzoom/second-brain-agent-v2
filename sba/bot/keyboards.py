"""Telegram bot inline keyboards."""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def confirm_delete_keyboard(deletion_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Удалить", callback_data=f"confirm_del:{deletion_id}"),
        InlineKeyboardButton(text="❌ Оставить", callback_data=f"cancel_del:{deletion_id}"),
    ]])
