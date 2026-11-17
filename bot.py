# Phrasal verbs Telegram trainer, basic and extended vocabulary.
import asyncio
import os
import random
import re
import sqlite3
import io
import secrets
import logging
from contextlib import contextmanager
from catalog import migrate, backup, parse_import, import_rows, HEADERS
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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

DEFAULT_REMINDER_HOUR = 7
REMINDER_HOURS = (6, 7, 8, 9, 12, 18, 20, 21)


# ---------------------------------------------------------------- time ----
def now():
    """Local time (Asia/Almaty): used for dates and for display only."""
    return datetime.now(TZ)

def now_utc():
    return datetime.now(timezone.utc)

def utc_iso(dt):
    """Canonical UTC string for due_at. One fixed format, so text comparison is safe."""
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


# ------------------------------------------------------------------ db ----
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

def migrate_due_at_utc(db):
    """One-time migration (guarded by user_version): old local due_at -> UTC."""
    if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
        return
    for r in db.execute('SELECT user_id,verb_id,due_at FROM progress').fetchall():
        dt = datetime.fromisoformat(r['due_at'])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        db.execute('UPDATE progress SET due_at=? WHERE user_id=? AND verb_id=?',
                   (utc_iso(dt), r['user_id'], r['verb_id']))
    db.execute('PRAGMA user_version=1')

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
        db.execute(f"""CREATE TABLE IF NOT EXISTS settings (
          user_id INTEGER PRIMARY KEY, daily_limit INTEGER NOT NULL DEFAULT {DEFAULT_LIMIT},
          mode TEXT NOT NULL DEFAULT 'basic',
          reminder_hour INTEGER DEFAULT {DEFAULT_REMINDER_HOUR}, last_reminder_date TEXT)""")
        cols = {x['name'] for x in db.execute('PRAGMA table_info(settings)')}
        if 'mode' not in cols:
            db.execute("ALTER TABLE settings ADD COLUMN mode TEXT NOT NULL DEFAULT 'basic'")
        if 'reminder_hour' not in cols:
            db.execute(f'ALTER TABLE settings ADD COLUMN reminder_hour INTEGER DEFAULT {DEFAULT_REMINDER_HOUR}')
        if 'last_reminder_date' not in cols:
            db.execute('ALTER TABLE settings ADD COLUMN last_reminder_date TEXT')
        cols = {x['name'] for x in db.execute('PRAGMA table_info(active_question)')}
        if 'token' not in cols:
            db.execute("ALTER TABLE active_question ADD COLUMN token TEXT")
        if 'direction' not in cols:
            db.execute("ALTER TABLE active_question ADD COLUMN direction TEXT NOT NULL DEFAULT 'en_ru'")
        db.execute('DELETE FROM active_question')
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            registered_at TEXT NOT NULL, last_seen TEXT NOT NULL)""")
        migrate_due_at_utc(db)

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
        row=db.execute('''SELECT v.*, p.streak AS streak FROM words.catalog v JOIN progress p ON p.verb_id=v.id
            WHERE p.user_id=? AND v.section=? AND p.due_at<=?
            ORDER BY p.due_at,v.sort_order LIMIT 1''',(user_id,mode,utc_iso(now_utc()))).fetchone()
        if row:return row,'review',mode
        introduced=db.execute('''SELECT COUNT(*) FROM progress p JOIN words.catalog v ON v.id=p.verb_id
            WHERE p.user_id=? AND v.section=? AND p.introduced_date=?''',
            (user_id,mode,t.date().isoformat())).fetchone()[0]
        if limit and introduced>=limit:return None,'limit',mode
        row=db.execute('''SELECT v.*, 0 AS streak FROM words.catalog v LEFT JOIN progress p
            ON p.verb_id=v.id AND p.user_id=? WHERE v.section=? AND p.verb_id IS NULL
            ORDER BY v.sort_order LIMIT 1''',(user_id,mode)).fetchone()
        return row,('new' if row else 'done'),mode


# ------------------------------------------- question building (stages 2-4) ----
PLACEHOLDERS = {'sth', 'smth', 'smb', 'sb', 'something', 'somebody', 'someone', 'one', 'ones', 'oneself'}

# Irregular forms of common base verbs (regular -s/-ing/-ed forms are generated).
_IRREGULAR = ('be:am,is,are,was,were,been,being|bear:bore,borne|beat:beaten|begin:began,begun|blow:blew,blown|'
    'break:broke,broken|bring:brought|build:built|burn:burnt|buy:bought|catch:caught|come:came|deal:dealt|'
    'do:did,done|draw:drew,drawn|drink:drank,drunk|drive:drove,driven|eat:ate,eaten|fall:fell,fallen|'
    'feed:fed|feel:felt|fight:fought|find:found|fly:flew,flown|forget:forgot,forgotten|get:got,gotten|'
    'give:gave,given|go:went,gone|grow:grew,grown|hang:hung|have:has,had|hide:hid,hidden|hold:held|'
    'keep:kept|know:knew,known|lay:laid|lead:led|leave:left|lend:lent|lie:lay,lain|lose:lost|make:made|'
    'mean:meant|meet:met|pay:paid|ride:rode,ridden|ring:rang,rung|rise:rose,risen|run:ran|say:said|'
    'see:saw,seen|sell:sold|send:sent|shake:shook,shaken|shoot:shot|show:shown|sing:sang,sung|sink:sank,sunk|'
    'sit:sat|sleep:slept|speak:spoke,spoken|spend:spent|stand:stood|steal:stole,stolen|stick:stuck|'
    'strike:struck|swear:swore,sworn|sweep:swept|swim:swam,swum|swing:swung|take:took,taken|teach:taught|'
    'tear:tore,torn|tell:told|think:thought|throw:threw,thrown|understand:understood|wake:woke,woken|'
    'wear:wore,worn|win:won|write:wrote,written')
IRREGULAR = {k: tuple(v.split(',')) for k, v in (p.split(':') for p in _IRREGULAR.split('|'))}

def norm_text(s):
    return ' '.join(str(s).split()).casefold()

def verb_words(verb):
    """Words of a phrasal verb without placeholders like sth/smb/one's."""
    out = []
    for w in verb.lower().split():
        w = w.strip('.,;:!?()[]')
        if re.fullmatch(r'[a-z]+', w) and w not in PLACEHOLDERS:
            out.append(w)
    return out

def verb_forms(base):
    forms = {base, base + 's', base + 'es', base + 'ing', base + 'ed',
             base + base[-1] + 'ing', base + base[-1] + 'ed'}
    if base.endswith('e'):
        forms |= {base + 'd', base[:-1] + 'ing'}
    if base.endswith('ie'):
        forms.add(base[:-2] + 'ying')
    if len(base) > 1 and base.endswith('y') and base[-2] not in 'aeiou':
        forms |= {base[:-1] + 'ies', base[:-1] + 'ied'}
    forms |= set(IRREGULAR.get(base, ()))
    return forms

def mask_verb(text, verb):
    """Replace the target phrasal verb (with simple inflections) by ____.
    Returns the text unchanged if the verb is not found."""
    words = verb_words(verb)
    if not words:
        return text
    forms = '|'.join(sorted((re.escape(f) for f in verb_forms(words[0])), key=len, reverse=True))
    head = rf'\b(?:{forms})\b'
    if len(words) > 1:
        tail = r'\s+'.join(re.escape(w) for w in words[1:])
        masked = re.sub(rf'{head}\s+{tail}\b', '____', text, flags=re.I)
        if masked != text:
            return masked
    return re.sub(head, '____', text, flags=re.I)

def question_hint(item, direction):
    """Context line shown BEFORE the answer. In ru_en the target verb is hidden."""
    if item['example']:
        icon, text = '💬', item['example']
    elif item['definition_en']:
        icon, text = '📝', item['definition_en']
    else:
        return ''
    if direction == 'ru_en':
        masked = mask_verb(text, item['verb'])
        if masked == text and icon == '💬':
            return ''  # could not hide the verb: the example will be shown after the answer
        text = masked
    return f'{icon} <i>{escape(text)}</i>'

def option_text(row, direction):
    return row['verb'] if direction == 'ru_en' else row['meaning_ru']

def similarity(verb_a, verb_b):
    """2 = same base verb (give up / give in), 1 = shared particle (give up / pick up), 0 = unrelated."""
    a, b = verb_words(verb_a), verb_words(verb_b)
    if not a or not b:
        return 0
    if a[0] == b[0]:
        return 2
    return 1 if set(a[1:]) & set(b[1:]) else 0

def pick_distractors(db, item, mode, direction, n=3):
    """Wrong options as (id, text). Similar verbs first, then random ones from the same
    section, then random ones from other sections. Options never repeat or contradict the answer."""
    bad_verb, bad_meaning = norm_text(item['verb']), norm_text(item['meaning_ru'])
    seen = {norm_text(option_text(item, direction))}
    picked = []

    def take(rows):
        for r in rows:
            if len(picked) >= n:
                return
            # same verb or same meaning as the answer would also be "correct"
            if norm_text(r['verb']) == bad_verb or norm_text(r['meaning_ru']) == bad_meaning:
                continue
            text = option_text(r, direction)
            if norm_text(text) in seen:
                continue
            seen.add(norm_text(text))
            picked.append((r['id'], text))

    same = db.execute('SELECT id,verb,meaning_ru FROM words.catalog WHERE section=? AND id<>?',
                      (mode, item['id'])).fetchall()
    random.shuffle(same)
    take(sorted(same, key=lambda r: -similarity(item['verb'], r['verb'])))  # stable: random inside a group
    if len(picked) < n:
        take(db.execute('''SELECT id,verb,meaning_ru FROM words.catalog
            WHERE section<>? ORDER BY RANDOM() LIMIT 100''', (mode,)).fetchall())
    return picked


# ------------------------------------------------------------------ ui ----
def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📖 Учить / повторить',callback_data='next')],
        [InlineKeyboardButton(text='📚 Режим словаря',callback_data='modes')],
        [InlineKeyboardButton(text='📊 Статистика',callback_data='stats')],
        [InlineKeyboardButton(text='⚙️ Лимит в день',callback_data='settings')],
        [InlineKeyboardButton(text='⏰ Напоминание',callback_data='remind')]])

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

def remind_menu():
    rows = [[InlineKeyboardButton(text=f'{h:02d}:00', callback_data=f'remind:{h}') for h in REMINDER_HOURS[i:i+4]]
            for i in range(0, len(REMINDER_HOURS), 4)]
    rows.append([InlineKeyboardButton(text='🔕 Выключить', callback_data='remind:off')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def send_next(message,user_id):
    item,status,mode=get_next(user_id)
    with connect() as db:
        count=totals(db,mode)
        label=collection_name(db,mode)
    if item is None:
        msg=('Дневной лимит новых карточек достигнут. Повторения доступны независимо от лимита.'
             if status=='limit' else 'Сейчас нет новых карточек или повторений по сроку. Возвращайся позже.')
        await message.answer(msg,reply_markup=menu());return
    # even streak (and every new card) = EN -> RU, odd streak = RU -> EN
    direction = 'ru_en' if item['streak'] % 2 else 'en_ru'
    token = secrets.token_hex(6)
    with connect() as db:
        db.execute('''INSERT INTO active_question(user_id,verb_id,token,direction) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET verb_id=excluded.verb_id,token=excluded.token,
            direction=excluded.direction''',(user_id,item['id'],token,direction))
        others = pick_distractors(db, item, mode, direction)
    choices=[(item['id'],option_text(item,direction)),*others]
    random.shuffle(choices)
    # Telegram buttons are short: full numbered options go into the message, buttons are just numbers.
    options = '\n'.join(f'{i}. {escape(text)}' for i, (_, text) in enumerate(choices, 1))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(i), callback_data=f'answer:{token}:{item["id"]}:{id_}')
        for i, (id_, _) in enumerate(choices, 1)]])
    if direction == 'en_ru':
        prompt, question = item['verb'], 'Как правильно перевести?'
    else:
        prompt, question = item['meaning_ru'], 'Как это сказать по-английски?'
    hint = question_hint(item, direction)
    await message.answer(
        f"{'🔁 Повторение' if status == 'review' else '📖 Новый глагол'}"
        f" · {escape(label)} · №{item['sort_order']}/{count}\n\n"
        f"<b>{escape(prompt)}</b>"
        + (f"\n{hint}" if hint else '')
        + f"\n\n{options}\n\n{question}",
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


# ------------------------------------------------------ reminders (stage 5) ----
def get_reminder_hour(db, user_id):
    row = db.execute('SELECT reminder_hour FROM settings WHERE user_id=?', (user_id,)).fetchone()
    return row['reminder_hour'] if row else DEFAULT_REMINDER_HOUR

def set_reminder_hour(db, user_id, hour):
    """hour=None turns reminders off."""
    db.execute('''INSERT INTO settings(user_id,daily_limit,reminder_hour) VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET reminder_hour=excluded.reminder_hour''',
        (user_id, DEFAULT_LIMIT, hour))

def disable_reminders(user_id):
    with connect() as db:
        set_reminder_hour(db, user_id, None)

def mark_reminded(user_id):
    with connect() as db:
        db.execute('''INSERT INTO settings(user_id,daily_limit,last_reminder_date) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET last_reminder_date=excluded.last_reminder_date''',
            (user_id, DEFAULT_LIMIT, now().date().isoformat()))

def collect_reminders():
    """[(user_id, due_count)] for users whose reminder hour is now and who were not reminded today.
    Users without any due card are skipped. Users without a settings row use the default hour."""
    t = now()
    today = t.date().isoformat()
    end_of_day = utc_iso(datetime.combine(t.date() + timedelta(days=1), datetime.min.time(), tzinfo=TZ))
    result = []
    with connect() as db:
        rows = db.execute('''SELECT u.user_id, COALESCE(s.mode,'basic') AS mode
            FROM users u LEFT JOIN settings s ON s.user_id=u.user_id
            WHERE (CASE WHEN s.user_id IS NULL THEN ? ELSE s.reminder_hour END)=?
              AND COALESCE(s.last_reminder_date,'')<>?''',
            (DEFAULT_REMINDER_HOUR, t.hour, today)).fetchall()
        for r in rows:
            n = db.execute('''SELECT COUNT(*) FROM progress p JOIN words.catalog v ON v.id=p.verb_id
                WHERE p.user_id=? AND v.section=? AND p.due_at<?''',
                (r['user_id'], r['mode'], end_of_day)).fetchone()[0]
            if n:
                result.append((r['user_id'], n))
    return result

def greeting(hour):
    if hour < 12:
        return '☀️ Доброе утро!'
    return '👋 Привет!' if hour < 18 else '🌙 Добрый вечер!'

async def reminder_loop(bot):
    while True:
        try:
            due = await asyncio.to_thread(collect_reminders)
            for user_id, n in due:
                try:
                    await bot.send_message(user_id, f'{greeting(now().hour)} Сегодня повторений: {n}',
                                           reply_markup=menu())
                except TelegramForbiddenError:
                    await asyncio.to_thread(disable_reminders, user_id)  # user blocked the bot
                    continue
                except TelegramBadRequest:
                    logging.exception('Reminder rejected for %s', user_id)
                    await asyncio.to_thread(mark_reminded, user_id)  # do not retry every minute today
                    continue
                except Exception:
                    logging.exception('Reminder failed for %s', user_id)
                    continue  # will be retried on the next tick within this hour
                await asyncio.to_thread(mark_reminded, user_id)
                await asyncio.sleep(0.05)
        except Exception:
            logging.exception('Reminder loop iteration failed')
        await asyncio.sleep(60)


# ------------------------------------------------------------- handlers ----
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

async def show_remind(message,user_id):
    with connect() as db:
        hour=get_reminder_hour(db,user_id)
    status=f'каждый день в {hour:02d}:00 (время Астаны)' if hour is not None else 'выключено'
    await message.answer(f'⏰ Напоминание о повторениях: {status}.\nВыбери время:',reply_markup=remind_menu())

@dp.message(Command('remind'))
async def remind(message:Message):await show_remind(message,message.from_user.id)

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

@dp.callback_query(F.data=='remind')
async def remind_menu_cb(q:CallbackQuery):
    await q.answer();await show_remind(q.message,q.from_user.id)

@dp.callback_query(F.data.startswith('remind:'))
async def remind_cb(q:CallbackQuery):
    value=q.data.split(':',1)[1]
    if value=='off':
        hour=None
    else:
        try:
            hour=int(value)
        except ValueError:
            hour=-1
        if hour not in REMINDER_HOURS:
            await q.answer('Ошибка настройки',show_alert=True);return
    with connect() as db:
        set_reminder_hour(db,q.from_user.id,hour)
    await q.answer('Сохранено')
    await q.message.answer('🔕 Напоминание выключено.' if hour is None else f'⏰ Напоминание в {hour:02d}:00 (время Астаны).',reply_markup=menu())

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
    closed=False
    # No awaits inside this block: the write lock must not be held while waiting for the network.
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        item=db.execute('SELECT * FROM words.catalog WHERE id=?',(asked_id,)).fetchone()
        selected=db.execute('SELECT id,verb,meaning_ru FROM words.catalog WHERE id=?',(selected_id,)).fetchone()
        active=db.execute('SELECT verb_id,token,direction FROM active_question WHERE user_id=?',(q.from_user.id,)).fetchone()
        if item is None or selected is None or active is None or active['verb_id']!=asked_id or active['token']!=token:
            closed=True
        else:
            direction=active['direction']
            db.execute('DELETE FROM active_question WHERE user_id=?',(q.from_user.id,))
            correct=asked_id==selected_id
            old=db.execute('SELECT streak FROM progress WHERE user_id=? AND verb_id=?',(q.from_user.id,asked_id)).fetchone()
            streak=((old['streak']+1) if old else 1) if correct else 0
            delays=(1,3,7,14,30)
            due=now_utc()+(timedelta(days=delays[min(streak-1,4)]) if correct else timedelta(minutes=5))
            db.execute('''INSERT INTO progress(user_id,verb_id,correct,wrong,streak,introduced_date,due_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,verb_id) DO UPDATE SET
               correct=progress.correct+excluded.correct,wrong=progress.wrong+excluded.wrong,
               streak=excluded.streak,due_at=excluded.due_at''',
               (q.from_user.id,asked_id,int(correct),int(not correct),streak,now().date().isoformat(),utc_iso(due)))
    if closed:
        await q.answer('Вопрос уже закрыт',show_alert=True);return
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
    if not correct:
        # show what the wrong choice actually means, so similar verbs get separated in memory
        detail += f"\nТвой ответ: {escape(selected['verb'])} — {escape(selected['meaning_ru'])}"
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
    await message.answer('/learn — учить и повторять\n/mode — книга и глава\n/stats — личная статистика\n/settings — лимит новых слов\n/remind — время напоминания\n/myid — твой ID\nАдминистратору: /import, /users')

@dp.message(Command('import'))
async def import_help(message:Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer('Добавлять слова может только администратор.'); return
    await message.answer('Отправь файл .xlsx или .csv (UTF-8), до 2 МБ и 1000 слов. Первый лист Excel, первая строка — заголовки: '+ '; '.join(HEADERS) + '\nПервые четыре поля обязательны. Пример и перевод примера можно оставить пустыми. Дубликаты в одной книге и главе пропускаются, но если у уже существующего слова нет примера, он будет дописан. Остальное импорт не меняет.')

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
        added,updated,skipped=await asyncio.to_thread(import_rows, WORDS_DB, rows)
    except ValueError as exc:
        await message.answer('Импорт отменён: '+str(exc)); return
    except Exception:
        logging.exception('Import failed')
        await message.answer('Не удалось импортировать файл. Проверь формат и журнал бота.'); return
    await message.answer(f'Добавлено: {added}. Примеров дописано: {updated}. Дубликатов пропущено: {skipped}. Выбери набор: /mode')

async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN or TOKEN=='PUT_YOUR_TOKEN_HERE':raise RuntimeError('Укажи BOT_TOKEN в .env')
    init_db()
    async with Bot(token=TOKEN) as bot:
        reminders=asyncio.create_task(reminder_loop(bot))
        try:
            await dp.start_polling(bot)
        finally:
            reminders.cancel()

if __name__=='__main__':
    try:asyncio.run(main())
    except KeyboardInterrupt:pass
