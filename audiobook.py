# Audiobook chapter -> text + ready-made Claude prompt. No Telegram code here.
import re
import threading

MAX_FILE_SIZE = 20 * 1024 * 1024  # Telegram Bot API download limit
WHISPER_MODEL = 'small'

_model = None
_model_lock = threading.Lock()


def parse_caption(caption):
    """'Книга; Глава N' -> ('Книга', N). Raises ValueError if the format is wrong."""
    parts = [p.strip() for p in (caption or '').split(';')]
    if len(parts) != 2 or not parts[0]:
        raise ValueError('Подпись должна быть такой: Книга; Глава N')
    m = re.search(r'\d+', parts[1])
    if not m:
        raise ValueError('Не найден номер главы. Пример подписи: Harry Potter; Глава 3')
    return parts[0], int(m.group())


def _get_model():
    """Load faster-whisper once (first call downloads the model, ~500 MB)."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(WHISPER_MODEL, device='cpu', compute_type='int8')
        return _model


def transcribe(path):
    """Blocking speech recognition of an English audio file. Run it in a thread."""
    segments, _info = _get_model().transcribe(str(path), language='en')
    text = '\n'.join(s.text.strip() for s in segments if s.text.strip())  # generator: work happens here
    if not text:
        raise ValueError('В файле не удалось распознать речь.')
    return text


def txt_filename(book, chapter):
    safe = re.sub(r'[^\w\- ]+', '', book).strip().replace(' ', '_')[:60] or 'book'
    return f'{safe}_chapter_{chapter}.txt'


def build_prompt(book, chapter):
    return (f'Вот текст главы {chapter} книги {book}. Мой уровень A2. '
            '1) Выбери 15 полезных слов и фраз и выдай их CSV-строками формата '
            'книга;глава;слово;перевод;пример;перевод примера. '
            '2) Кратко перескажи главу. '
            '3) Затем будь моим собеседником: задавай простые вопросы по главе, '
            'используй эти слова, коротко исправляй мои ошибки.')
