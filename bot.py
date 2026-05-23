import os
import sqlite3
import re
import asyncio
import threading
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn

# ========== ТОКЕН БОТА (ВРЕМЕННО) ==========
# Для первого запуска вставьте свой токен сюда:
BOT_TOKEN = "8765024611:AAERwa1byuGKLAP_2OaruHpA5VbffVAUl5o"
# ВНИМАНИЕ: позже мы перенесем токен в защищенное место

ALLOWED_USERS = []
TRIGGER = "#задача"

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            original_message_id INTEGER,
            bot_message_id INTEGER,
            task_text TEXT,
            assignee TEXT,
            deadline TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def add_task(chat_id, orig_msg_id, bot_msg_id, task_text, assignee=None, deadline=None):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO tasks (chat_id, original_message_id, bot_message_id, task_text, assignee, deadline)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (chat_id, orig_msg_id, bot_msg_id, task_text, assignee, deadline))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def update_task_status(task_id, new_status):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
    conn.commit()
    conn.close()

def delete_task(task_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

def get_task(task_id):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("SELECT id, chat_id, bot_message_id, task_text, assignee, deadline, status FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0], "chat_id": row[1], "bot_message_id": row[2],
            "task_text": row[3], "assignee": row[4], "deadline": row[5], "status": row[6]
        }
    return None

# ========== ПАРСИНГ ДАТ И ОЧИСТКА ==========
MONTHS = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
    'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
}
DAY_BASES = {
    'понедельн': 0, 'пн': 0,
    'вторник': 1, 'вт': 1,
    'сред': 2, 'ср': 2,
    'четверг': 3, 'чт': 3,
    'пятниц': 4, 'пт': 4,
    'суббот': 5, 'сб': 5,
    'воскресен': 6, 'вс': 6
}
FULL_DAYS = ['понедельник', 'пн', 'вторник', 'вт', 'среда', 'ср', 'четверг', 'чт', 'пятница', 'пт', 'суббота', 'сб', 'воскресенье', 'вс']

def parse_deadline(text):
    now = datetime.now()
    text_lower = text.lower()
    for base, weekday in DAY_BASES.items():
        if re.search(rf'\b{base}', text_lower):
            days_ahead = (weekday - now.weekday() + 7) % 7
            if days_ahead == 0:
                days_ahead = 7
            return (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    if "завтра" in text_lower:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if "послезавтра" in text_lower:
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")
    match = re.search(r'через\s+(\d+)\s+дн', text_lower)
    if match:
        return (now + timedelta(days=int(match.group(1)))).strftime("%Y-%m-%d")
    match_ru = re.search(r'(\d{1,2})\s+(' + '|'.join(MONTHS.keys()) + r')(?:\s+(\d{4}))?', text_lower)
    if match_ru:
        day = int(match_ru.group(1))
        month = MONTHS[match_ru.group(2)]
        year = int(match_ru.group(3)) if match_ru.group(3) else now.year
        try:
            dt = datetime(year, month, day)
            if dt < now and not match_ru.group(3):
                dt = dt.replace(year=now.year + 1)
            return dt.strftime("%Y-%m-%d")
        except:
            pass
    match_num = re.search(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?', text)
    if match_num:
        day = int(match_num.group(1))
        month = int(match_num.group(2))
        year = int(match_num.group(3)) if match_num.group(3) else now.year
        try:
            dt = datetime(year, month, day)
            if dt < now and not match_num.group(3):
                dt = dt.replace(year=now.year + 1)
            return dt.strftime("%Y-%m-%d")
        except:
            pass
    return None

def parse_assignee(text):
    match = re.search(r'@([a-zA-Z0-9_]+)', text)
    return match.group(1) if match else None

def extract_deadline_phrase(text, deadline_date):
    if not deadline_date:
        return None
    text_lower = text.lower()
    dt = datetime.strptime(deadline_date, "%Y-%m-%d")
    phrases_to_try = []
    date_dot = dt.strftime("%d.%m.%Y")
    date_dot_short = dt.strftime("%d.%m")
    for prefix in ['до ', 'к ', 'в ', '']:
        phrases_to_try.append(prefix + date_dot)
        phrases_to_try.append(prefix + date_dot_short)
    month_ru = [m for m, num in MONTHS.items() if num == dt.month][0]
    date_ru = f"{dt.day} {month_ru}"
    date_ru_year = f"{dt.day} {month_ru} {dt.year}"
    for prefix in ['до ', 'к ', 'в ', '']:
        phrases_to_try.append(prefix + date_ru)
        phrases_to_try.append(prefix + date_ru_year)
    for day_base, _ in DAY_BASES.items():
        pattern = r'(до\s+|к\s+|в\s+)?' + re.escape(day_base) + r'\w*'
        match = re.search(pattern, text_lower)
        if match:
            phrases_to_try.append(match.group(0))
    for rel in ['завтра', 'послезавтра']:
        if rel in text_lower:
            phrases_to_try.append(rel)
    match_thru = re.search(r'через\s+\d+\s+дн', text_lower)
    if match_thru:
        phrases_to_try.append(match_thru.group(0))
    found = None
    for phrase in sorted(phrases_to_try, key=len, reverse=True):
        if phrase in text_lower:
            found = phrase
            break
    return found

def clean_task_text(original_text, assignee, deadline_phrase):
    cleaned = original_text
    if deadline_phrase:
        cleaned = re.sub(re.escape(deadline_phrase), '', cleaned, flags=re.IGNORECASE)
    if assignee:
        cleaned = re.sub(rf'@({re.escape(assignee)})(?![a-zA-Z0-9_])', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = re.sub(r'[.,!?;:]$', '', cleaned).strip()
    return cleaned

def extract_tasks_from_message(text):
    if text.startswith(TRIGGER):
        body = text[len(TRIGGER):].strip()
    else:
        body = text
    lines = body.split("\n")
    tasks = []
    current_task = None
    for line in lines:
        match = re.match(r'^\s*\d+[\.\)]\s+(.+)$', line) or re.match(r'^\s*\*\s+(.+)$', line)
        if match:
            if current_task:
                tasks.append(current_task)
            task_line = match.group(1).strip()
            assignee = parse_assignee(task_line)
            deadline_date = parse_deadline(task_line)
            deadline_phrase = extract_deadline_phrase(task_line, deadline_date) if deadline_date else None
            current_task = {
                "text": task_line,
                "assignee": assignee,
                "deadline_date": deadline_date,
                "deadline_phrase": deadline_phrase
            }
        else:
            if current_task:
                current_task["text"] += " " + line.strip()
                if not current_task["assignee"]:
                    current_task["assignee"] = parse_assignee(current_task["text"])
                if not current_task["deadline_date"]:
                    current_task["deadline_date"] = parse_deadline(current_task["text"])
                    if current_task["deadline_date"]:
                        current_task["deadline_phrase"] = extract_deadline_phrase(current_task["text"], current_task["deadline_date"])
    if current_task:
        tasks.append(current_task)
    if not tasks:
        assignee = parse_assignee(body)
        deadline_date = parse_deadline(body)
        deadline_phrase = extract_deadline_phrase(body, deadline_date) if deadline_date else None
        tasks = [{
            "text": body,
            "assignee": assignee,
            "deadline_date": deadline_date,
            "deadline_phrase": deadline_phrase
        }]
    return tasks

# ========== КНОПКИ И КАРТОЧКИ ==========
def get_status_keyboard(task_id, current_status):
    buttons = []
    if current_status != "active":
        buttons.append(InlineKeyboardButton(text="🔵 Активно", callback_data=f"status_{task_id}_active"))
    if current_status != "inprogress":
        buttons.append(InlineKeyboardButton(text="🟡 В работе", callback_data=f"status_{task_id}_inprogress"))
    if current_status != "done":
        buttons.append(InlineKeyboardButton(text="✅ Завершено", callback_data=f"status_{task_id}_done"))
    buttons.append(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{task_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

def escape_md(text):
    if not text:
        return text
    chars = r'_*[]()~`>#+-=|{}.!'
    for ch in chars:
        text = text.replace(ch, '\\' + ch)
    return text

async def send_task_card(message: types.Message, original_task_text, assignee, deadline_date, deadline_phrase):
    cleaned = clean_task_text(original_task_text, assignee, deadline_phrase)
    safe_cleaned = escape_md(cleaned)
    safe_assignee = escape_md(assignee) if assignee else None
    text = f"📋 **Задача**\n\n{safe_cleaned}\n"
    if safe_assignee:
        text += f"👤 Ответственный: @{safe_assignee}\n"
    if deadline_date:
        try:
            d = datetime.strptime(deadline_date, "%Y-%m-%d")
            text += f"⏰ Дедлайн: {d.strftime('%d.%m.%Y')}\n"
        except:
            text += f"⏰ Дедлайн: {escape_md(deadline_date)}\n"
    sent_msg = await message.reply(text, parse_mode="Markdown")
    task_id = add_task(
        message.chat.id,
        message.message_id,
        sent_msg.message_id,
        cleaned,
        assignee,
        deadline_date
    )
    keyboard = get_status_keyboard(task_id, "active")
    await sent_msg.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

# ========== ОБРАБОТЧИКИ БОТА ==========
async def handle_message(message: types.Message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    if not message.text:
        return
    text = message.text.strip()
    if text == "/get_chat_id":
        await message.reply(f"🆔 **ID этого чата:** `{message.chat.id}`", parse_mode="Markdown")
        return
    if text.startswith(TRIGGER):
        tasks = extract_tasks_from_message(text)
        if not tasks:
            await message.reply("❌ Не распознано. Пример:\n#задача 1. Сделать отчёт @ivanov до пятницы")
            return
        for task in tasks:
            await send_task_card(message, task["text"], task["assignee"],
                                 task["deadline_date"], task.get("deadline_phrase"))
        return

async def handle_callback(callback_query: types.CallbackQuery):
    data = callback_query.data
    if data.startswith("status_"):
        _, task_id, new_status = data.split("_")
        task_id = int(task_id)
        task = get_task(task_id)
        if not task:
            await callback_query.answer("Задача не найдена", show_alert=True)
            return
        update_task_status(task_id, new_status)
        deadline_phrase = extract_deadline_phrase(task['task_text'], task['deadline']) if task['deadline'] else None
        cleaned = clean_task_text(task['task_text'], task['assignee'], deadline_phrase)
        safe_cleaned = escape_md(cleaned)
        safe_assignee = escape_md(task['assignee']) if task['assignee'] else None
        text = f"📋 **Задача**\n\n{safe_cleaned}\n"
        if safe_assignee:
            text += f"👤 Ответственный: @{safe_assignee}\n"
        if task['deadline']:
            try:
                d = datetime.strptime(task['deadline'], "%Y-%m-%d")
                text += f"⏰ Дедлайн: {d.strftime('%d.%m.%Y')}\n"
            except:
                text += f"⏰ Дедлайн: {escape_md(task['deadline'])}\n"
        status_emoji = {"active": "🔵 Активно", "inprogress": "🟡 В работе", "done": "✅ Завершено"}
        text += f"\n*Статус:* {status_emoji.get(new_status, new_status)}"
        keyboard = get_status_keyboard(task_id, new_status)
        await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await callback_query.answer("Статус обновлён")
    elif data.startswith("delete_"):
        task_id = int(data.split("_")[1])
        task = get_task(task_id)
        if not task:
            await callback_query.answer("Задача не найдена", show_alert=True)
            return
        delete_task(task_id)
        await callback_query.message.delete()
        await callback_query.answer("Задача удалена", show_alert=False)

# ========== API ДЛЯ MINI APP ==========
app_fastapi = FastAPI()
app_fastapi.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TaskUpdate(BaseModel):
    task_id: int
    text: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    deadline: Optional[str] = None

@app_fastapi.get("/tasks")
def get_tasks(chat_id: int):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("SELECT id, task_text, assignee, deadline, status FROM tasks WHERE chat_id = ? ORDER BY created_at DESC", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "text": r[1], "assignee": r[2], "deadline": r[3], "status": r[4]} for r in rows]

@app_fastapi.post("/tasks/update")
async def update_task(task: TaskUpdate):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    if task.text is not None:
        c.execute("UPDATE tasks SET task_text = ? WHERE id = ?", (task.text, task.task_id))
    if task.status is not None:
        c.execute("UPDATE tasks SET status = ? WHERE id = ?", (task.status, task.task_id))
    if task.assignee is not None:
        c.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (task.assignee, task.task_id))
    if task.deadline is not None:
        c.execute("UPDATE tasks SET deadline = ? WHERE id = ?", (task.deadline, task.task_id))
    conn.commit()
    conn.close()
    # Обновляем карточку в Telegram (простой перезапуск: найдем сообщение бота и отредактируем)
    task_data = get_task(task.task_id)
    if task_data:
        deadline_phrase = extract_deadline_phrase(task_data['task_text'], task_data['deadline']) if task_data['deadline'] else None
        cleaned = clean_task_text(task_data['task_text'], task_data['assignee'], deadline_phrase)
        safe_cleaned = escape_md(cleaned)
        safe_assignee = escape_md(task_data['assignee']) if task_data['assignee'] else None
        text = f"📋 **Задача**\n\n{safe_cleaned}\n"
        if safe_assignee:
            text += f"👤 Ответственный: @{safe_assignee}\n"
        if task_data['deadline']:
            try:
                d = datetime.strptime(task_data['deadline'], "%Y-%m-%d")
                text += f"⏰ Дедлайн: {d.strftime('%d.%m.%Y')}\n"
            except:
                text += f"⏰ Дедлайн: {escape_md(task_data['deadline'])}\n"
        status_emoji = {"active": "🔵 Активно", "inprogress": "🟡 В работе", "done": "✅ Завершено"}
        text += f"\n*Статус:* {status_emoji.get(task_data['status'], task_data['status'])}"
        keyboard = get_status_keyboard(task_data['id'], task_data['status'])
        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.edit_message_text(text, chat_id=task_data['chat_id'], message_id=task_data['bot_message_id'],
                                        reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            print(f"Ошибка обновления: {e}")
    return {"ok": True}

@app_fastapi.delete("/tasks/{task_id}")
async def delete_task_endpoint(task_id: int):
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("SELECT chat_id, bot_message_id FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    chat_id, bot_msg_id = row
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=bot_msg_id)
    except:
        pass
    return {"ok": True}

@app_fastapi.get("/health")
def health():
    return {"status": "ok"}

# ========== ЗАПУСК БОТА И API В ПОТОКАХ ==========
def run_bot():
    asyncio.run(bot_main())

async def bot_main():
    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook()
    init_db()
    dp = Dispatcher()
    dp.message.register(handle_message)
    dp.callback_query.register(handle_callback)
    print("✅ Бот запущен")
    await dp.start_polling(bot)

def run_api():
    uvicorn.run(app_fastapi, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    # Запускаем бота в отдельном потоке
    import threading
    threading.Thread(target=run_bot, daemon=True).start()
    # Запускаем API в основном потоке
    run_api()