# Phrasal verbs Telegram trainer, basic and extended vocabulary.
import asyncio
import os
import random
import sqlite3
import io
import secrets
import logging
from contextlib import contextmanager
from catalog import migrate, backup, parse_import, import_rows, HEADERS
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
TOKEN = os.getenv('BOT_TOKEN')
WORDS_DB = ROOT / 'vasilyeva_phrasal_verbs_pages_4-33.sqlite3'
PROGRESS_DB = ROOT / 'progress.sqlite3'
TZ = ZoneInfo('Asia/Almaty')
DEFAULT_LIMIT = int(os.getenv('DAILY_NEW_LIMIT', '5'))
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}
if DEFAULT_LIMIT < 0:
    raise ValueError('DAILY_NEW_LIMIT должен быть >= 0')

def now():
    return datetime.now(TZ)

@contextmanager
def connect():
    db = sqlite3.connect(PROGRESS_DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('ATTACH DATABASE ? AS words', (str(WORDS_DB),))
    try:
        with db:
            yield db
    finally:
        db.close()

def init_db():
    if not WORDS_DB.exists():
        raise RuntimeError(f'Не найдена база словаря: {WORDS_DB.name}. Распакуй ZIP целиком.')
    migrate(WORDS_DB)
    backup(PROGRESS_DB)
    with connect() as db:
        n = db.execute('SELECT COUNT(*) FROM words.catalog').fetchone()[0]
        if n < 100:
            raise RuntimeError('База словаря пустая или неполная.')
        db.execute('''CREATE TABLE IF NOT EXISTS progress (
          user_id INTEGER NOT NULL, verb_id INTEGER NOT NULL,
          correct INTEGER NOT NULL DEFAULT 0, wrong INTEGER NOT NULL DEFAULT 0,
          streak INTEGER NOT NULL DEFAULT 0, introduced_date TEXT NOT NULL,
          due_at TEXT NOT NULL, PRIMARY KEY (user_id,verb_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS active_question (
          user_id INTEGER PRIMARY KEY, verb_id INTEGER NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS settings (
          user_id INTEGER PRIMARY KEY, daily_limit INTEGER NOT NULL DEFAULT 0,
          mode TEXT NOT NULL DEFAULT 'basic')''')
        cols={x['name'] for x in db.execute('PRAGMA table_info(settings)')}
        if 'mode' not in cols:
            db.execute("ALTER TABLE settings ADD COLUMN mode TEXT NOT NULL DEFAULT 'basic'")
        cols = {x['name'] for x in db.execute('PRAGMA table_info(active_question)')}
        if 'token' not in cols:
            db.execute("ALTER TABLE active_question ADD COLUMN token TEXT")
        db.execute('DELETE FROM active_question')
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            registered_at TEXT NOT NULL, last_seen TEXT NOT NULL)""")

def current_settings(db,user_id):
    row=db.execute('SELECT daily_limit,mode FROM settings WHERE user_id=?',(user_id,)).fetchone()
    return (row['daily_limit'],row['mode']) if row else (DEFAULT_LIMIT,'basic')

def collection_name(db,mode):
    row=db.execute('SELECT book,chapter FROM words.collections WHERE section=?',(mode,)).fetchone()
    return row['book']+' / '+row['chapter'] if row else mode

def totals(db,mode):
    return db.execute('SELECT COUNT(*) FROM words.catalog WHERE section=?',(mode,)).fetchone()[0]

def get_next(user_id):
    t=now()
    with connect() as db:
        limit,mode=current_settings(db,user_id)
        row=db.execute('''SELECT v.* FROM words.catalog v JOIN progress p ON p.verb_id=v.id
            WHERE p.user_id=? AND v.section=? AND p.due_at<=?
            ORDER BY p.due_at,v.sort_order LIMIT 1''',(user_id,mode,t.isoformat())).fetchone()
        if row:return row,'review',mode
        introduced=db.execute('''SELECT COUNT(*) FROM progress p JOIN words.catalog v ON v.id=p.verb_id
            WHERE p.user_id=? AND v.section=? AND p.introduced_date=?''',
            (user_id,mode,t.date().isoformat())).fetchone()[0]
        if limit and introduced>=limit:return None,'limit',mode
        row=db.execute('''SELECT v.* FROM words.catalog v LEFT JOIN progress p
            ON p.verb_id=v.id AND p.user_id=? WHERE v.section=? AND p.verb_id IS NULL
            ORDER BY v.sort_order LIMIT 1''',(user_id,mode)).fetchone()
        return row,('new' if row else 'done'),mode

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📖 Учить / повторить',callback_data='next')],
        [InlineKeyboardButton(text='📚 Режим словаря',callback_data='modes')],
        [InlineKeyboardButton(text='📊 Статистика',callback_data='stats')],
        [InlineKeyboardButton(text='⚙️ Лимит в день',callback_data='settings')]])

def mode_menu(page=0):
    with connect() as db:
        rows = db.execute('SELECT * FROM words.collections ORDER BY rowid LIMIT 9 OFFSET ?', (page*8,)).fetchall()
    buttons = [[InlineKeyboardButton(text=(r['book']+' / '+r['chapter'])[:90], callback_data='mode:'+r['section'])] for r in rows[:8]]
    nav = []
    if page: nav.append(InlineKeyboardButton(text='←', callback_data=f'page:{page-1}'))
    if len(rows)>8: nav.append(InlineKeyboardButton(text='→', callback_data=f'page:{page+1}'))
    if nav: buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def settings_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(n) if n else 'Без ограничений',callback_data=f'limit:{n}') for n in (3,5)],
        [InlineKeyboardButton(text='10',callback_data='limit:10'),InlineKeyboardButton(text='∞ Без лимита',callback_data='limit:0')]])

async def send_next(message,user_id):
    item,status,mode=get_next(user_id)
    with connect() as db:
        count=totals(db,mode)
        label=collection_name(db,mode)
    if item is None:
        msg=('Дневной лимит новых карточек достигнут. Повторения доступны независимо от лимита.'
             if status=='limit' else 'Сейчас нет новых карточек или повторений по сроку. Возвращайся позже.')
        await message.answer(msg,reply_markup=menu());return
    token = secrets.token_hex(6)
    with connect() as db:
        db.execute('''INSERT INTO active_question(user_id,verb_id,token) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET verb_id=excluded.verb_id,token=excluded.token''',(user_id,item['id'],token))
        others=db.execute('''SELECT MIN(id) id,meaning_ru FROM words.catalog
            WHERE section=? AND id<>? AND meaning_ru<>? GROUP BY meaning_ru ORDER BY RANDOM() LIMIT 3''',
            (mode,item['id'],item['meaning_ru'])).fetchall()
        if len(others)<3:
            others=db.execute('''SELECT MIN(id) id,meaning_ru FROM words.catalog
                WHERE id<>? AND meaning_ru<>? GROUP BY meaning_ru ORDER BY RANDOM() LIMIT 3''',
                (item['id'],item['meaning_ru'])).fetchall()
    choices=[(item['id'],item['meaning_ru']),*[(r['id'],r['meaning_ru']) for r in others]]
    random.shuffle(choices)
    # Telegram buttons are short: show full numbered translations in the message.
    options = '\n'.join(f'{i}. {escape(meaning)}' for i, (_, meaning) in enumerate(choices, 1))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=meaning,
            callback_data=f'answer:{token}:{item["id"]}:{id_}'
        )]
        for id_, meaning in choices
    ])

    extra = '' if item['needs_review'] else ''

    await message.answer(
        f"{'🔁 Повторение' if status == 'review' else '📖 Новый глагол'}"
        f" · {escape(label)} · №{item['sort_order']}/{count}\n\n"
        f"<b>{escape(item['verb'])}</b>"
        f"{extra}\n\nКак правильно перевести?",
        parse_mode='HTML',
        reply_markup=kb
    )

async def show_stats(message,user_id):
    with connect() as db:
        limit,mode=current_settings(db,user_id)
        s=db.execute('''SELECT COUNT(*) n,COALESCE(SUM(p.correct),0) c,COALESCE(SUM(p.wrong),0) w, COALESCE(SUM(p.streak>=3),0) mastered
            FROM progress p JOIN words.catalog v ON v.id=p.verb_id WHERE p.user_id=? AND v.section=?''',
            (user_id,mode)).fetchone()
        total=totals(db,mode)
        label=collection_name(db,mode)
    await message.answer(f"📊 Набор: {label}\nНачато: {s['n']}/{total}\nЗакреплено (3 верных подряд): {s['mastered']}\n✅ Правильно: {s['c']}\n❌ Ошибок: {s['w']}\n📅 Новых в день: {limit or 'без лимита'}",reply_markup=menu())

dp=Dispatcher()

class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        msg = event if isinstance(event, Message) else event.message
        if not msg or msg.chat.type != 'private':
            if isinstance(event, CallbackQuery): await event.answer('Открой личный чат с ботом.')
            elif msg: await msg.answer('Обучение доступно в личном чате с ботом.')
            return
        with connect() as db:
            db.execute("""INSERT INTO users VALUES(?,?,?,?,?) ON CONFLICT(user_id)
                DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_seen=excluded.last_seen""",
                (user.id,user.username,user.first_name,now().isoformat(),now().isoformat()))
        return await handler(event, data)

dp.message.outer_middleware(UserMiddleware())
dp.callback_query.outer_middleware(UserMiddleware())

@dp.message(CommandStart())
async def start(message:Message):
    await message.answer('Привет! Выбери книгу и главу в разделе «Режим словаря». /help — команды. Повторение: 1, 3, 7, 14, 30 дней.',reply_markup=menu())

@dp.message(Command('learn'))
async def learn(message:Message):await send_next(message,message.from_user.id)

@dp.message(Command('stats'))
async def stats(message:Message):await show_stats(message,message.from_user.id)

@dp.message(Command('settings'))
async def settings(message:Message):await message.answer('Сколько новых карточек в день?',reply_markup=settings_menu())

@dp.message(Command('mode'))
async def choose_mode(message:Message):await message.answer('Выбери словарь:',reply_markup=mode_menu())

@dp.callback_query(F.data=='next')
async def next_cb(q:CallbackQuery):
    await q.answer();await send_next(q.message,q.from_user.id)

@dp.callback_query(F.data=='stats')
async def stats_cb(q:CallbackQuery):
    await q.answer();await show_stats(q.message,q.from_user.id)

@dp.callback_query(F.data=='settings')
async def settings_cb(q:CallbackQuery):
    await q.answer();await q.message.answer('Количество новых карточек в день:',reply_markup=settings_menu())

@dp.callback_query(F.data=='modes')
async def modes_cb(q:CallbackQuery):
    await q.answer();await q.message.answer('Выбери режим:',reply_markup=mode_menu())

@dp.callback_query(F.data.startswith('mode:'))
async def mode_cb(q:CallbackQuery):
    mode=q.data.split(':',1)[1]
    with connect() as db:
        exists = db.execute('SELECT 1 FROM words.collections WHERE section=?',(mode,)).fetchone()
    if not exists:
        await q.answer('Неизвестный режим',show_alert=True);return
    with connect() as db:
        db.execute('''INSERT INTO settings(user_id,daily_limit,mode) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode''',(q.from_user.id,DEFAULT_LIMIT,mode))
        db.execute('DELETE FROM active_question WHERE user_id=?',(q.from_user.id,))
        label=collection_name(db,mode)
    await q.answer('Режим сохранён')
    await q.message.answer('Выбран: '+label,reply_markup=menu())

@dp.callback_query(F.data.startswith('limit:'))
async def limit_cb(q:CallbackQuery):
    try:
        limit=int(q.data.split(':',1)[1])
        if limit not in (0,3,5,10): raise ValueError
    except (ValueError,AssertionError,IndexError):
        await q.answer('Ошибка настройки',show_alert=True);return
    with connect() as db:
        db.execute('''INSERT INTO settings(user_id,daily_limit) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET daily_limit=excluded.daily_limit''',(q.from_user.id,limit))
    await q.answer('Сохранено');await q.message.answer(f'Новый лимит: {limit or "без ограничений"}',reply_markup=menu())

@dp.callback_query(F.data.startswith('answer:'))
async def answer_cb(q:CallbackQuery):
    try:
        _,token,asked_id,selected_id=q.data.split(':');asked_id=int(asked_id);selected_id=int(selected_id)
    except (ValueError,AttributeError):
        await q.answer('Неверный ответ',show_alert=True);return
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        item=db.execute('SELECT * FROM words.catalog WHERE id=?',(asked_id,)).fetchone()
        selected=db.execute('SELECT id FROM words.catalog WHERE id=?',(selected_id,)).fetchone()
        active=db.execute('SELECT verb_id,token FROM active_question WHERE user_id=?',(q.from_user.id,)).fetchone()
        if item is None or selected is None or active is None or active['verb_id']!=asked_id or active['token']!=token:
            await q.answer('Вопрос уже закрыт',show_alert=True);return
        db.execute('DELETE FROM active_question WHERE user_id=?',(q.from_user.id,))
        correct=asked_id==selected_id
        old=db.execute('SELECT streak FROM progress WHERE user_id=? AND verb_id=?',(q.from_user.id,asked_id)).fetchone()
        streak=((old['streak']+1) if old else 1) if correct else 0
        delays=(1,3,7,14,30)
        due=now()+timedelta(days=delays[min(streak-1,4)]) if correct else now()+timedelta(minutes=5)
        db.execute('''INSERT INTO progress(user_id,verb_id,correct,wrong,streak,introduced_date,due_at)
           VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,verb_id) DO UPDATE SET
           correct=progress.correct+excluded.correct,wrong=progress.wrong+excluded.wrong,
           streak=excluded.streak,due_at=excluded.due_at''',
           (q.from_user.id,asked_id,int(correct),int(not correct),streak,now().date().isoformat(),due.isoformat()))
    await q.answer('Верно!' if correct else 'Ошибка')
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logging.exception('Could not remove old keyboard')
    detail=f"\nEnglish: {escape(item['definition_en'])}" if item['definition_en'] else ''
    if item['example']:
        detail += '\nПример: ' + escape(item['example'])
    if item['example_ru']:
        detail += '\n' + escape(item['example_ru'])
    await q.message.answer(f"{'✅ Верно!' if correct else '❌ Неверно.'}\n<b>{escape(item['verb'])}</b> — {escape(item['meaning_ru'])}{detail}\n"+
        ('Повторение запланировано.' if correct else 'Повторим через 5 минут.'),parse_mode='HTML',reply_markup=menu())

@dp.callback_query(F.data.startswith('page:'))
async def page_cb(q:CallbackQuery):
    try: page=max(0,int(q.data.split(':')[1]))
    except ValueError:
        await q.answer(); return
    await q.answer()
    await q.message.answer('Выбери книгу и главу:', reply_markup=mode_menu(page))

@dp.message(Command('myid'))
async def myid(message:Message):
    await message.answer(f'Твой Telegram ID: {message.from_user.id}')

@dp.message(Command('help'))
async def help_command(message:Message):
    await message.answer('/learn — учить и повторять\n/mode — книга и глава\n/stats — личная статистика\n/settings — лимит новых слов\n/myid — твой ID\nАдминистратору: /import, /users')

@dp.message(Command('import'))
async def import_help(message:Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer('Добавлять слова может только администратор.'); return
    await message.answer('Отправь файл .xlsx или .csv (UTF-8), до 2 МБ и 1000 слов. Первый лист Excel, первая строка — заголовки: '+ '; '.join(HEADERS) + '\nПервые четыре поля обязательны. Пример и перевод примера можно оставить пустыми. Дубликаты в одной книге и главе пропускаются. Импорт только добавляет слова.')

@dp.message(Command('users'))
async def users(message:Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer('Команда только для администратора.'); return
    with connect() as db:
        count=db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        rows=db.execute("""SELECT u.user_id,u.first_name,COUNT(p.verb_id) n,
            COALESCE(SUM(p.correct),0) c,COALESCE(SUM(p.wrong),0) w
            FROM users u LEFT JOIN progress p ON p.user_id=u.user_id
            GROUP BY u.user_id ORDER BY u.last_seen DESC LIMIT 20""").fetchall()
    lines=[f"{r['user_id']} {(r['first_name'] or '')[:40]}: начато {r['n']}, верно {r['c']}, ошибок {r['w']}" for r in rows]
    await message.answer(f'Пользователей: {count}. Последние 20 активных:\n'+'\n'.join(lines))

@dp.message(F.document)
async def import_document(message:Message, bot:Bot):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer('Добавлять слова может только администратор.'); return
    doc=message.document
    if not doc.file_name or not doc.file_name.lower().endswith(('.xlsx','.csv')):
        await message.answer('Нужен файл .xlsx или .csv. Инструкция: /import'); return
    if not doc.file_size or doc.file_size>2_000_000:
        await message.answer('Размер файла должен быть до 2 МБ.'); return
    try:
        buffer=io.BytesIO()
        await bot.download(doc, destination=buffer)
        rows=await asyncio.to_thread(parse_import, buffer.getvalue(), doc.file_name)
        added,skipped=await asyncio.to_thread(import_rows, WORDS_DB, rows)
    except ValueError as exc:
        await message.answer('Импорт отменён: '+str(exc)); return
    except Exception:
        logging.exception('Import failed')
        await message.answer('Не удалось импортировать файл. Проверь формат и журнал бота.'); return
    await message.answer(f'Добавлено: {added}. Дубликатов пропущено: {skipped}. Выбери набор: /mode')

async def main():
    if not TOKEN or TOKEN=='PUT_YOUR_TOKEN_HERE':raise RuntimeError('Укажи BOT_TOKEN в .env')
    init_db()
    async with Bot(token=TOKEN) as bot:
        await dp.start_polling(bot)

if __name__=='__main__':
    try:asyncio.run(main())
    except KeyboardInterrupt:pass
