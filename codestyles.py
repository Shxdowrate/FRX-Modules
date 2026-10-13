"""
CodeStyles — красивая картинка из кода в разных стилях.

  .code [стиль] [тема]   ответом на сообщение с кодом или на файл (.py, .js, .txt…), или код после команды
                         (первая строка — стиль/тема, код — со второй строки)
  .codestyles            список стилей и тем

Стили:  vscode · webide · mac · carbon · sublime · terminal · github · notepad · retro · paper
Темы подсветки (любая к любому стилю): dracula, one-dark, monokai, nord, github-dark, night-owl, material,
gruvbox-dark, solarized-dark, solarized-light, xcode, vs, zenburn и др. — без темы у каждого стиля своя.

Всё рисуется прямо в FRX (Pillow + Pygments, ставятся сами), без внешних сервисов; только стиль carbon сначала
пробует сервис Carbonara (как carbon.now.sh) и при его недоступности рисует такую же картинку сам.
Шрифт JetBrains Mono скачивается один раз (≈0.5 МБ); нет сети — берётся моноширинный шрифт системы.
"""

import asyncio
import html
import io
import os
import sys
from pathlib import Path

import aiohttp

import decorators
import engine
import visual

NAME = "CodeStyles"
MODULE_NAME = "codestyles"
VERSION = "1.1"
AUTHOR = "FRX"
DESCRIPTION = "Картинка из кода: VS Code, WebIDE, macOS, Carbon, терминал, GitHub, ретро и др. — .code ответом на код"
REQUIREMENTS = ["pygments"]   # ставятся сами (core/requirements.py)

STYLES = {
    'vscode':   ('VS Code',   'окно Visual Studio Code: вкладка, номера строк, статус-бар', 'vscode-dark'),
    'webide':   ('WebIDE',    'FRX WebIDE: сайт FRX, вкладка webide — тёплая подсветка, номера строк, панель справа', 'frx'),
    'mac':      ('macOS',     'окно macOS на градиенте, без лишнего', 'one-dark'),
    'carbon':   ('Carbon',    'как carbon.now.sh (сервис Carbonara, без сети — локально)', 'dracula'),
    'sublime':  ('Sublime',   'Sublime Text: вкладки, мини-карта', 'monokai'),
    'terminal': ('Terminal',  'терминал: $ cat файл и вывод', 'terminal'),
    'github':   ('GitHub',    'светлая карточка gist с номерами строк', 'github-light'),
    'notepad':  ('Notepad',   'Блокнот Windows 11', 'notepad'),
    'retro':    ('Retro CRT', 'зелёный ламповый монитор со строками развёртки', 'crt'),
    'paper':    ('Paper',     'светлый лист, как в книге', 'paper'),
}
ALIASES = {'vs': 'vscode', 'code': 'vscode', 'vsc': 'vscode', 'web': 'webide', 'ide': 'webide', 'idea': 'webide',
           'jetbrains': 'webide', 'macos': 'mac', 'osx': 'mac', 'cb': 'carbon', 'sub': 'sublime', 'term': 'terminal',
           'console': 'terminal', 'cmd': 'terminal', 'gh': 'github', 'gist': 'github', 'crt': 'retro', 'light': 'paper'}

EXT_LANG = {'.py': 'python', '.js': 'javascript', '.ts': 'typescript', '.json': 'json', '.html': 'html', '.css': 'css',
            '.go': 'go', '.rs': 'rust', '.java': 'java', '.c': 'c', '.h': 'c', '.cpp': 'cpp', '.cs': 'csharp',
            '.sh': 'bash', '.sql': 'sql', '.php': 'php', '.kt': 'kotlin', '.lua': 'lua', '.rb': 'ruby', '.swift': 'swift',
            '.yml': 'yaml', '.yaml': 'yaml', '.md': 'markdown', '.xml': 'xml', '.dart': 'dart', '.toml': 'toml'}
LANG_EXT = {'python': 'py', 'javascript': 'js', 'typescript': 'ts', 'json': 'json', 'html': 'html', 'css': 'css', 'go': 'go',
            'rust': 'rs', 'java': 'java', 'c': 'c', 'cpp': 'cpp', 'csharp': 'cs', 'bash': 'sh', 'sql': 'sql', 'php': 'php',
            'kotlin': 'kt', 'lua': 'lua', 'ruby': 'rb', 'swift': 'swift', 'yaml': 'yml', 'markdown': 'md', 'xml': 'xml'}

FONT_URL = 'https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/fonts/ttf/JetBrainsMono-{}.ttf'
SCALE = 2           # рисуем в 2× — картинка чёткая и на телефоне
MAX_COLS = 110      # длиннее — переносим


def _esc(x):
    return html.escape(str(x), quote=False)


# --------------------------------------------------------------------------- зависимости

_dep_lock = asyncio.Lock()


async def _deps():
    async with _dep_lock:
        missing = []
        try:
            import PIL  # noqa: F401
        except ImportError:
            missing.append('Pillow')
        try:
            import pygments  # noqa: F401
        except ImportError:
            missing.append('Pygments')
        if missing:
            proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pip', 'install', *missing,
                                                        '--disable-pip-version-check', stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError("не удалось поставить " + ", ".join(missing) + ": " + out.decode('utf-8', 'replace')[-200:])
            import importlib
            importlib.invalidate_caches()


def _font_dir():
    try:
        from core.paths import DATA_DIR
        d = Path(DATA_DIR) / 'cache' / 'fonts'
    except Exception:
        d = Path('data') / 'cache' / 'fonts'
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _fonts():
    """{'regular': path|None, 'bold': path|None} — JetBrains Mono (скачивается один раз)."""
    d = _font_dir()
    out = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        for kind, name in (('regular', 'Regular'), ('bold', 'Bold')):
            p = d / f'JetBrainsMono-{name}.ttf'
            if not p.is_file() or p.stat().st_size < 50_000:
                try:
                    async with s.get(FONT_URL.format(name)) as r:
                        if r.status == 200:
                            data = await r.read()
                            if len(data) > 50_000:
                                p.write_bytes(data)
                except Exception:
                    pass
            out[kind] = str(p) if p.is_file() and p.stat().st_size > 50_000 else None
    return out


def _load_font(path, size, bold=False):
    from PIL import ImageFont
    cands = [path] if path else []
    cands += (['consolab.ttf', 'DejaVuSansMono-Bold.ttf'] if bold else []) + \
             ['consola.ttf', 'CascadiaMono.ttf', 'DejaVuSansMono.ttf', 'Menlo.ttc', 'LiberationMono-Regular.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf']
    for c in cands:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _gothic(size):
    """Шрифт сайта FRX — Century Gothic Bold (есть в Windows с Office); нет — жирный без засечек."""
    from PIL import ImageFont
    for c in ('GOTHICB.TTF', 'gothicb.ttf', 'CenturyGothic-Bold.ttf', 'segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _ui_font(size, bold=False):
    from PIL import ImageFont
    for c in (['segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf'] if bold else []) + \
             ['segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf', 'Helvetica.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


# --------------------------------------------------------------------------- палитры

def _palette_custom(name):
    from pygments.token import Comment, Keyword, Name, Number, Operator, Punctuation, String, Token
    P = {
        'vscode-dark': dict(bg='#1e1e1e', fg='#d4d4d4', items={
            Keyword: '#569cd6', Keyword.Namespace: '#c586c0', Keyword.Constant: '#569cd6', Name.Builtin: '#4ec9b0',
            Name.Function: '#dcdcaa', Name.Class: '#4ec9b0', Name.Decorator: '#dcdcaa', String: '#ce9178',
            String.Doc: '#6a9955', Number: '#b5cea8', Comment: '#6a9955', Operator: '#d4d4d4', Name.Variable: '#9cdcfe',
            Name.Attribute: '#9cdcfe', Name.Tag: '#569cd6', Punctuation: '#d4d4d4', Name.Builtin.Pseudo: '#569cd6'}),
        'darcula': dict(bg='#2b2b2b', fg='#a9b7c6', items={
            Keyword: '#cc7832', Keyword.Constant: '#cc7832', Name.Builtin: '#8888c6', Name.Function: '#ffc66d',
            Name.Class: '#a9b7c6', Name.Decorator: '#bbb529', String: '#6a8759', String.Doc: '#629755',
            Number: '#6897bb', Comment: '#808080', Operator: '#a9b7c6', Name.Builtin.Pseudo: '#94558d', Name.Tag: '#e8bf6a',
            Name.Attribute: '#bababa'}),
        'github-light': dict(bg='#ffffff', fg='#24292f', items={
            Keyword: '#cf222e', Name.Builtin: '#8250df', Name.Function: '#8250df', Name.Class: '#953800',
            Name.Decorator: '#8250df', String: '#0a3069', Number: '#0550ae', Comment: '#6e7781', Operator: '#cf222e',
            Keyword.Constant: '#0550ae', Name.Tag: '#116329', Name.Attribute: '#0550ae', Name.Builtin.Pseudo: '#0550ae'}),
        'terminal': dict(bg='#0c0c0c', fg='#cccccc', items={
            Keyword: '#3b8eea', Name.Builtin: '#29b8db', Name.Function: '#e5e510', Name.Class: '#23d18b',
            String: '#23d18b', Number: '#d670d6', Comment: '#767676', Operator: '#cccccc', Name.Decorator: '#e5e510'}),
        'notepad': dict(bg='#ffffff', fg='#1b1b1b', items={}),
        # тёплая палитра FRX WebIDE (core/web/static/webide.css: .t-kw, .t-str, …)
        'frx': dict(bg='#151211', fg='#d4d4d4', items={
            Keyword: '#ff5a3c', Keyword.Namespace: '#ff7a59', Keyword.Constant: '#ff8a00', Name.Builtin: '#ff9e5e',
            Name.Builtin.Pseudo: '#ff8a00', Name.Function: '#ffe1c2', Name.Class: '#ff9e5e', Name.Decorator: '#ff8a00',
            String: '#f2c28c', String.Doc: '#6f6a66', Number: '#ffb36b', Comment: '#6f6a66', Name.Variable: '#f3d6c4',
            Name.Attribute: '#f3d6c4', Name.Tag: '#ff5a3c', Operator: '#d4d4d4', Punctuation: '#d4d4d4'}),
        'crt': dict(bg='#031a09', fg='#33ff66', items={Comment: '#1f9e45', String: '#9dffb5', Keyword: '#66ff99',
                                                        Number: '#ccffd9'}),
        'paper': dict(bg='#fbf8f1', fg='#2d2a26', items={
            Keyword: '#8a3b12', Name.Function: '#1f4e79', Name.Class: '#1f4e79', String: '#4d7a2a', Number: '#7b2d8b',
            Comment: '#9a9284', Name.Decorator: '#a0522d', Name.Builtin: '#8a3b12'}),
    }
    if name not in P:
        return None
    p = P[name]
    items = p['items']

    def color(tt):
        while tt is not None and tt is not Token:
            if tt in items:
                return items[tt]
            tt = tt.parent
        return p['fg']
    italic = {Comment} if name in ('vscode-dark', 'paper', 'github-light', 'frx') else set()
    out = {'bg': p['bg'], 'fg': p['fg'], 'color': color, 'italic': lambda tt: any(tt in i for i in italic), 'bold': lambda tt: False}
    if name == 'frx':        # управляющие слова — ярче и жирные, как .t-ctl в WebIDE
        out['ctl'] = ({'if', 'elif', 'else', 'for', 'while', 'return', 'await', 'try', 'except', 'finally', 'with', 'yield',
                       'break', 'continue', 'raise', 'import', 'from', 'pass', 'match', 'case'}, '#ff7a59')
    return out


def _palette(name):
    custom = _palette_custom(name)
    if custom:
        return custom
    from pygments.styles import get_style_by_name
    from pygments.token import Token
    st = get_style_by_name(name)
    bg = st.background_color or '#1e1e1e'
    base = st.style_for_token(Token.Text)
    fg = '#' + base['color'] if base.get('color') else ('#222222' if _light(bg) else '#e6e6e6')

    def color(tt):
        c = st.style_for_token(tt).get('color')
        return '#' + c if c else fg
    return {'bg': bg, 'fg': fg, 'color': color, 'italic': lambda tt: st.style_for_token(tt).get('italic'),
            'bold': lambda tt: st.style_for_token(tt).get('bold')}


def _rgb(h):
    h = h.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _light(h):
    r, g, b = _rgb(h)
    return (r * 299 + g * 587 + b * 114) / 1000 > 140


def _mix(a, b, t):
    a, b = _rgb(a) if isinstance(a, str) else a, _rgb(b) if isinstance(b, str) else b
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


THEMES = ['frx', 'dracula', 'one-dark', 'monokai', 'nord', 'github-dark', 'night-owl', 'material', 'gruvbox-dark',
          'gruvbox-light', 'solarized-dark', 'solarized-light', 'xcode', 'vs', 'zenburn', 'native', 'fruity',
          'paraiso-dark', 'lightbulb', 'vscode-dark', 'darcula', 'github-light', 'paper']


# --------------------------------------------------------------------------- подсветка

def _tokens(code, lang, filename):
    """[[ (text, tokentype), … ] по строкам ], имя языка."""
    from pygments.lexers import get_lexer_by_name, guess_lexer, guess_lexer_for_filename
    from pygments.lexers.special import TextLexer
    from pygments.util import ClassNotFound
    lexer = None
    try:
        if lang:
            lexer = get_lexer_by_name(lang)
        elif filename:
            lexer = guess_lexer_for_filename(filename, code)
    except ClassNotFound:
        lexer = None
    if lexer is None:
        try:
            lexer = guess_lexer(code)
            if lexer.analyse_text(code) < 0.05:     # не уверен — пусть будет Python, чаще всего так и есть
                lexer = get_lexer_by_name('python')
        except ClassNotFound:
            lexer = TextLexer()
    lines, cur = [], []
    for tt, val in lexer.get_tokens(code):
        parts = val.split('\n')
        for i, part in enumerate(parts):
            if i:
                lines.append(cur)
                cur = []
            if part:
                cur.append((part, tt))
    if cur:
        lines.append(cur)
    while lines and not lines[-1]:
        lines.pop()
    return lines or [[]], (lexer.aliases[0] if lexer.aliases else 'text')


def _wrap(lines, cols):
    """Переносит длинные строки: [(номер или None, токены)]."""
    out = []
    for n, toks in enumerate(lines, 1):
        cur, width, first = [], 0, True
        for text, tt in toks:
            while text:
                room = cols - width
                if len(text) <= room:
                    cur.append((text, tt))
                    width += len(text)
                    text = ''
                else:
                    cur.append((text[:room], tt))
                    out.append((n if first else None, cur))
                    cur, width, first, text = [], 0, False, text[room:]
        out.append((n if first else None, cur))
    return out


# --------------------------------------------------------------------------- рисование

def _is_emoji(ch):
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF or o in (0x200D, 0xFE0F)


def _cols(text):
    """Ширина в «клетках»: эмодзи занимает две."""
    return sum(0 if ord(ch) in (0x200D, 0xFE0F) else (2 if _is_emoji(ch) else 1) for ch in text)


def _emoji_font(size):
    from PIL import ImageFont
    for c, sz in (('seguiemj.ttf', size), ('Apple Color Emoji.ttc', 160), ('NotoColorEmoji.ttf', 109),
                  ('/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf', 109)):
        try:
            return ImageFont.truetype(c, sz), sz
        except Exception:
            continue
    return None, 0


class Canvas:
    def __init__(self, fonts, size):
        self.s = SCALE
        self.font = _load_font(fonts.get('regular'), size * SCALE)
        self.bold = _load_font(fonts.get('bold') or fonts.get('regular'), size * SCALE, bold=True)
        self.cw = self.font.getlength('M')
        asc, desc = self.font.getmetrics()
        self.lh = int((asc + desc) * 1.45)
        self._emoji = None

    def code_size(self, rows, gutter):
        cols = max([sum(_cols(t) for t, _ in toks) for _, toks in rows] + [20])
        return int(cols * self.cw) + gutter, len(rows) * self.lh

    def _draw_emoji(self, d, x, y, ch):
        """Цветной эмодзи (если в системе есть такой шрифт), иначе пусто — без «квадратиков»."""
        if self._emoji is None:
            self._emoji = _emoji_font(int(self.cw * 1.6))
        font, native = self._emoji
        if font is None:
            return
        from PIL import Image, ImageDraw
        target = int(self.cw * 1.75)
        try:
            box = font.getbbox(ch)
            tile = Image.new('RGBA', (max(1, box[2]) + 4, max(1, box[3]) + 4), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((0, 0), ch, font=font, embedded_color=True)
            bb = tile.getbbox()
            if not bb:
                return
            tile = tile.crop(bb)
            k = target / max(tile.size)
            tile = tile.resize((max(1, int(tile.width * k)), max(1, int(tile.height * k))), Image.LANCZOS)
            asc, _ = self.font.getmetrics()
            d._image.paste(tile, (int(x + (self.cw * 2 - tile.width) / 2), int(y + asc - tile.height + asc * 0.12)), tile)
        except Exception:
            pass

    def text(self, d, x, y, text, font, fill, **kw):
        """Текст моноширинным шрифтом с эмодзи; возвращает x после текста."""
        run = ''
        for ch in text:
            if _is_emoji(ch):
                if run:
                    d.text((x, y), run, font=font, fill=fill, **kw)
                    x += self.cw * len(run)
                    run = ''
                if ord(ch) not in (0x200D, 0xFE0F):
                    self._draw_emoji(d, x, y, ch)
                    x += self.cw * 2
            else:
                run += ch
        if run:
            d.text((x, y), run, font=font, fill=fill, **kw)
            x += self.cw * len(run)
        return x

    def draw_code(self, d, x, y, rows, pal, gutter=0, gutter_color=None, active=None, glow=False):
        asc, desc = self.font.getmetrics()
        pad = (self.lh - asc - desc) // 2
        for i, (n, toks) in enumerate(rows):
            yy = y + i * self.lh + pad
            if gutter and n is not None:
                num = str(n)
                d.text((x + gutter - self.cw * (len(num) + 1.6), yy), num, font=self.font,
                       fill=pal.get('num_active') if n == active else gutter_color)
            cx = x + gutter
            for text, tt in toks:
                f = self.bold if pal['bold'](tt) else self.font
                col = pal['color'](tt)
                ctl = pal.get('ctl')
                if ctl and text.strip() in ctl[0]:
                    col, f = ctl[1], self.bold
                if glow:
                    self.text(d, cx, yy, text, f, _mix(col, pal['bg'], 0.55), stroke_width=2 * self.s,
                              stroke_fill=_mix(col, pal['bg'], 0.85))
                cx = self.text(d, cx, yy, text, f, col)


def _shadow(img, box, radius, blur=36, offset=18, alpha=150):
    from PIL import Image, ImageDraw, ImageFilter
    sh = Image.new('RGBA', img.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(sh).rounded_rectangle([x0, y0 + offset, x1, y1 + offset], radius, fill=(0, 0, 0, alpha))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(sh)


def _gradient(w, h, c1, c2):
    from PIL import Image
    from PIL import ImageChops
    base = Image.new('RGBA', (w, h), c1 + (255,))
    top = Image.new('RGBA', (w, h), c2 + (255,))
    v = Image.linear_gradient('L').resize((w, h))
    hz = Image.linear_gradient('L').rotate(90).transpose(Image.FLIP_LEFT_RIGHT).resize((w, h))
    mask = ImageChops.add(v, hz, scale=2)          # диагональ: левый верх — c1, правый низ — c2
    base.paste(top, (0, 0), mask)
    return base


def _win_controls(d, right, cy, s, color, step=44):
    """Кнопки окна Windows (свернуть, развернуть, закрыть) — нарисованы линиями: в шрифтах их знаков часто нет."""
    k = 5 * s
    for i in range(3):
        cx = right - (2.5 - i) * step * s
        if i == 0:
            d.line([cx - k, cy, cx + k, cy], fill=color, width=max(1, s))
        elif i == 1:
            d.rectangle([cx - k, cy - k, cx + k, cy + k], outline=color, width=max(1, s))
        else:
            d.line([cx - k, cy - k, cx + k, cy + k], fill=color, width=max(1, s))
            d.line([cx - k, cy + k, cx + k, cy - k], fill=color, width=max(1, s))


def _x_icon(d, cx, cy, s, color, k=4):
    d.line([cx - k * s, cy - k * s, cx + k * s, cy + k * s], fill=color, width=max(1, int(1.5 * s)))
    d.line([cx - k * s, cy + k * s, cx + k * s, cy - k * s], fill=color, width=max(1, int(1.5 * s)))


def _traffic(d, x, y, s, r=7):
    for i, c in enumerate([(255, 95, 87), (254, 188, 46), (40, 200, 64)]):
        cx = x + i * 20 * s
        d.ellipse([cx - r * s, y - r * s, cx + r * s, y + r * s], fill=c)


def render(code, style='mac', theme=None, lang=None, filename=None, fonts=None, bg=None):
    from PIL import Image, ImageDraw
    fonts = fonts or {}
    style = style if style in STYLES else 'mac'
    pal = _palette(theme or STYLES[style][2])
    raw, lname = _tokens(code.replace('\t', '    ').rstrip('\n'), lang, filename)
    rows = _wrap(raw, MAX_COLS)
    fname = filename or f"main.{LANG_EXT.get(lname, 'txt')}"
    c = Canvas(fonts, 15 if style not in ('retro',) else 16)
    s = c.s
    dark = not _light(pal['bg'])
    numc = _mix(pal['fg'], pal['bg'], 0.62)
    pal['num_active'] = _mix(pal['fg'], pal['bg'], 0.15)
    ui = _ui_font(12 * s)
    ui_b = _ui_font(12 * s, bold=True)

    if style in ('mac', 'carbon'):
        gutter = 0
        cw, ch = c.code_size(rows, gutter)
        pad, bar, margin = 28 * s, 44 * s, 56 * s
        ww, wh = max(cw + pad * 2, 420 * s), ch + bar + pad
        W, H = ww + margin * 2, wh + margin * 2
        if bg:
            img = Image.new('RGBA', (W, H), _rgb(bg) + (255,))
        elif style == 'carbon':
            img = _gradient(W, H, (171, 184, 195), (171, 184, 195))
        else:
            img = _gradient(W, H, (255, 90, 20), (124, 58, 237))
        box = (margin, margin, margin + ww, margin + wh)
        _shadow(img, box, 12 * s, blur=30 * s // 2, offset=14 * s)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, 12 * s, fill=_rgb(pal['bg']))
        _traffic(d, margin + 22 * s, margin + 22 * s, s, 6.5)
        if style == 'mac':
            tw = ui.getlength(fname)
            d.text((margin + (ww - tw) / 2, margin + 15 * s), fname, font=ui, fill=numc)
        c.draw_code(d, margin + pad, margin + bar, rows, pal)

    elif style == 'vscode':
        gutter = int(c.cw * (len(str(len(raw))) + 4))
        cw, ch = c.code_size(rows, gutter)
        title, tabs, crumbs, status, act = 32 * s, 36 * s, 24 * s, 24 * s, 48 * s
        W = max(act + cw + 60 * s, 640 * s)
        H = title + tabs + crumbs + ch + 24 * s + status
        img = Image.new('RGBA', (W, H), _rgb(pal['bg']) + (255,))
        d = ImageDraw.Draw(img)
        chrome = _mix(pal['bg'], '#000000', 0.25) if dark else _mix(pal['bg'], '#000000', 0.06)
        side = _mix(pal['bg'], '#000000', 0.12) if dark else _mix(pal['bg'], '#000000', 0.04)
        txt = _mix(pal['fg'], pal['bg'], 0.3)
        d.rectangle([0, 0, W, title], fill=chrome)
        t = f"{fname} — FRX — Visual Studio Code"
        d.text(((W - ui.getlength(t)) / 2, 9 * s), t, font=ui, fill=txt)
        _win_controls(d, W, title / 2, s, txt, step=40)
        d.rectangle([0, title, act, H - status], fill=side)
        for i in range(min(5, int((H - status - title - 16 * s) // (44 * s)))):     # значки activity bar — сколько влезет
            yy = title + 16 * s + i * 44 * s
            col = pal['fg'] if i == 0 else _mix(pal['fg'], side, 0.55)
            d.rounded_rectangle([act / 2 - 10 * s, yy, act / 2 + 10 * s, yy + 20 * s], 4 * s, outline=col, width=2 * s)
        d.rectangle([0, title + 12 * s, 2 * s, title + 40 * s], fill=pal['fg'])
        d.rectangle([act, title, W, title + tabs], fill=chrome)
        tab_w = int(ui.getlength(fname) + 70 * s)
        d.rectangle([act, title, act + tab_w, title + tabs], fill=_rgb(pal['bg']))
        d.rectangle([act, title, act + tab_w, title + 2 * s], fill=(0, 122, 204))
        d.ellipse([act + 14 * s, title + 14 * s, act + 22 * s, title + 22 * s], fill=(229, 192, 123))
        d.text((act + 30 * s, title + 10 * s), fname, font=ui, fill=pal['fg'])
        _x_icon(d, act + tab_w - 16 * s, title + tabs / 2, s, txt)
        d.text((act + 16 * s, title + tabs + 5 * s), f"FRX › {fname}", font=ui, fill=numc)
        c.draw_code(d, act + 6 * s, title + tabs + crumbs + 8 * s, rows, pal, gutter=gutter, gutter_color=numc, active=1)
        d.rectangle([0, H - status, W, H], fill=(0, 122, 204))
        right = f"Ln 1, Col 1   Spaces: 4   UTF-8   LF   {lname.capitalize()}"
        wc, by = (255, 255, 255), H - status / 2           # ветка git: две точки и линия, ошибки/предупреждения — кружок и треугольник
        d.ellipse([10 * s, by - 6 * s, 14 * s, by - 2 * s], outline=wc, width=s)
        d.ellipse([10 * s, by + 2 * s, 14 * s, by + 6 * s], outline=wc, width=s)
        d.line([12 * s, by - 2 * s, 12 * s, by + 2 * s], fill=wc, width=s)
        d.text((20 * s, H - status + 5 * s), "main", font=ui, fill=wc)
        x0 = 20 * s + ui.getlength("main") + 16 * s
        _x_icon(d, x0 + 5 * s, by, s, wc, k=3)
        d.ellipse([x0, by - 5 * s, x0 + 10 * s, by + 5 * s], outline=wc, width=s)
        d.text((x0 + 14 * s, H - status + 5 * s), "0", font=ui, fill=wc)
        x1 = x0 + 30 * s
        d.polygon([(x1 + 5 * s, by - 5 * s), (x1, by + 5 * s), (x1 + 10 * s, by + 5 * s)], outline=wc)
        d.text((x1 + 14 * s, H - status + 5 * s), "0", font=ui, fill=wc)
        d.text((W - ui.getlength(right) - 12 * s, H - status + 5 * s), right, font=ui, fill=(255, 255, 255))

    elif style == 'webide':
        # как вкладка «webide» сайта FRX: чёрное пространство с красно-оранжевым светом, меню «frx», заголовок,
        # чипы файла, редактор rgb(21,18,17) с номерами строк, строка состояния и панель справа
        gutter = int(c.cw * (len(str(len(raw))) + 3)) + 14 * s
        cw, ch = c.code_size(rows, gutter)
        pad, nav, head, chips, side_w, gap = 28 * s, 50 * s, 58 * s, 34 * s, 230 * s, 16 * s
        ed_w = max(cw + 40 * s, 520 * s)
        ed_h = max(ch + 32 * s, 290 * s)               # панель справа должна поместиться
        W = pad * 2 + ed_w + gap + side_w
        H = nav + head + chips + ed_h + 46 * s + pad
        ink, ink2, ink3 = (255, 255, 255), (163, 163, 163), (102, 102, 102)
        line_c = (38, 38, 40)
        img = Image.new('RGBA', (W, H), (6, 6, 7, 255))
        glow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        from PIL import ImageFilter
        gd = ImageDraw.Draw(glow)
        gd.ellipse([W * 0.55, -H * 0.45, W * 1.45, H * 0.5], fill=(255, 110, 20, 46))
        gd.ellipse([-W * 0.45, H * 0.55, W * 0.6, H * 1.5], fill=(255, 30, 20, 40))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70 * s)))
        stripes = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(stripes)
        for k in range(-H, W, 14 * s):                     # еле заметные диагональные полосы фона, как на сайте
            sd.line([k, H, k + H, 0], fill=(255, 255, 255, 5), width=1)
        img.alpha_composite(stripes)
        d = ImageDraw.Draw(img)
        gothic = _gothic(22 * s)
        gothic_s = _gothic(13 * s)
        gothic_t = _gothic(30 * s)
        # меню
        d.text((pad, 13 * s), "frx", font=gothic, fill=ink)
        d.text((pad + gothic.getlength("frx") + 8 * s, 22 * s), "1.0.0", font=_gothic(10 * s), fill=ink3)
        x = pad + 120 * s
        for t in ("конфиги", "модули", "files", "neuro", "webide", "аккаунты", "доступ"):
            on = t == 'webide'
            d.text((x, 18 * s), t, font=gothic_s, fill=ink if on else ink2)
            tw = gothic_s.getlength(t)
            if on:
                for k in range(int(tw)):
                    tt = k / max(1, tw)
                    d.line([x + k, 38 * s, x + k, 40 * s], fill=_mix('#ff2a1c', '#ff8a00', tt))
            x += tw + 26 * s
        d.line([0, nav, W, nav], fill=line_c, width=s)
        # заголовок
        d.text((pad, nav + 12 * s), "webide", font=gothic_t, fill=ink)
        sub = "FRX WebIDE · редактор модулей"
        d.text((W - pad - gothic_s.getlength(sub), nav + 24 * s), sub, font=gothic_s, fill=ink3)
        # чипы файлов
        cy = nav + head
        cx = pad
        for t, on in ((fname, True), ("config.json", False)):
            tw = gothic_s.getlength(t) + 24 * s
            d.rectangle([cx, cy, cx + tw, cy + 24 * s], fill=(21, 18, 17), outline=(255, 112, 52) if on else line_c, width=s)
            d.text((cx + 12 * s, cy + 5 * s), t, font=gothic_s, fill=ink if on else ink2)
            cx += tw + 8 * s
        # редактор
        ex, ey = pad, nav + head + chips
        d.rectangle([ex, ey, ex + ed_w, ey + ed_h], fill=(21, 18, 17))
        d.line([ex + gutter - 8 * s, ey, ex + gutter - 8 * s, ey + ed_h], fill=line_c, width=s)
        d.rectangle([ex + gutter - 7 * s, ey + 16 * s, ex + ed_w, ey + 16 * s + c.lh], fill=(33, 30, 29))   # текущая строка
        pal['num_active'] = (220, 220, 220)
        c.draw_code(d, ex, ey + 16 * s, rows, pal, gutter=gutter, gutter_color=ink3, active=1)
        # строка состояния
        sy = ey + ed_h + 14 * s
        d.ellipse([ex, sy + 4 * s, ex + 8 * s, sy + 12 * s], fill=(185, 245, 201))
        d.text((ex + 14 * s, sy), "синтаксис в порядке · сохранено", font=gothic_s, fill=(185, 245, 201))
        pos = "строка 1, столбец 1"
        d.text((ex + ed_w - c.font.getlength(pos) * 0.62, sy + 1 * s), pos, font=_load_font(fonts.get('regular'), 11 * s), fill=ink2)
        # панель справа
        px = ex + ed_w + gap
        d.rectangle([px, ey, px + side_w, ey + ed_h], fill=(17, 17, 20, 230), outline=line_c, width=s)
        d.line([px, ey, px + side_w, ey], fill=(255, 112, 52), width=s)
        d.text((px + 16 * s, ey + 14 * s), "модуль", font=gothic_s, fill=ink)
        info = [("файл", fname), ("строк", str(len(raw))), ("язык", lname), ("версия", "1.0")]
        for i, (k, v) in enumerate(info):
            yy = ey + 46 * s + i * 30 * s
            d.text((px + 16 * s, yy), k, font=gothic_s, fill=ink3)
            d.text((px + 86 * s, yy), v[:18], font=gothic_s, fill=ink)
            d.line([px + 16 * s, yy + 22 * s, px + side_w - 16 * s, yy + 22 * s], fill=line_c, width=1)
        by = ey + 46 * s + len(info) * 30 * s + 12 * s
        for i, (t, primary) in enumerate((("сохранить", True), ("проверить", False))):
            bx = px + 16 * s
            bw = side_w - 32 * s
            yy = by + i * 40 * s
            if yy + 30 * s > ey + ed_h - 8 * s:
                break
            if primary:
                for k in range(int(bw)):
                    d.line([bx + k, yy, bx + k, yy + 30 * s], fill=_mix('#ff2a1c', '#ff8a00', k / bw))
            else:
                d.rectangle([bx, yy, bx + bw, yy + 30 * s], outline=line_c, width=s)
            d.text((bx + (bw - gothic_s.getlength(t)) / 2, yy + 7 * s), t, font=gothic_s, fill=ink)

    elif style == 'sublime':
        gutter = int(c.cw * (len(str(len(raw))) + 3))
        cw, ch = c.code_size(rows, gutter)
        tabs, mini = 36 * s, 90 * s
        W = max(cw + mini + 40 * s, 600 * s)
        H = tabs + ch + 30 * s
        img = Image.new('RGBA', (W, H), _rgb(pal['bg']) + (255,))
        d = ImageDraw.Draw(img)
        bar = _mix(pal['bg'], '#000000', 0.3)
        d.rectangle([0, 0, W, tabs], fill=bar)
        tab_w = int(ui.getlength(fname) + 60 * s)
        d.rounded_rectangle([8 * s, 6 * s, 8 * s + tab_w, tabs + 8 * s], 6 * s, fill=_rgb(pal['bg']))
        d.text((24 * s, 13 * s), fname, font=ui, fill=pal['fg'])
        dx = 8 * s + tab_w - 16 * s
        d.ellipse([dx - 4 * s, tabs / 2 + 4 * s - 4 * s, dx + 4 * s, tabs / 2 + 4 * s + 4 * s], fill=numc)
        c.draw_code(d, 6 * s, tabs + 14 * s, rows, pal, gutter=gutter, gutter_color=numc)
        mx = W - mini - 10 * s       # мини-карта: полоски вместо текста
        for i, (_, toks) in enumerate(rows[:int(ch / (3 * s))]):
            xx = mx
            for text, tt in toks:
                w = len(text) * 1.2 * s
                x_end = min(xx + w, W - 10 * s)
                if text.strip() and x_end > xx:
                    d.rectangle([xx, tabs + 14 * s + i * 3 * s, x_end, tabs + 15.6 * s + i * 3 * s],
                                fill=_mix(pal['color'](tt), pal['bg'], 0.45))
                xx += w
                if xx >= W - 10 * s:
                    break
        d.rectangle([mx - 6 * s, tabs + 10 * s, W - 6 * s, tabs + 10 * s + min(ch, 160 * s)],
                    fill=None, outline=_mix(pal['fg'], pal['bg'], 0.85), width=s)

    elif style == 'terminal':
        cw, ch = c.code_size(rows, 0)
        bar, pad, margin = 36 * s, 22 * s, 40 * s
        prompt_h = c.lh * 2
        ww, wh = max(cw + pad * 2, 560 * s), bar + pad + prompt_h + ch + c.lh + pad
        W, H = ww + margin * 2, wh + margin * 2
        img = _gradient(W, H, (34, 40, 49), (20, 24, 30)) if not bg else Image.new('RGBA', (W, H), _rgb(bg) + (255,))
        box = (margin, margin, margin + ww, margin + wh)
        _shadow(img, box, 10 * s, blur=24 * s // 2, offset=10 * s)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, 10 * s, fill=_rgb(pal['bg']))
        d.rounded_rectangle([margin, margin, margin + ww, margin + bar], 10 * s, fill=(44, 44, 44))
        d.rectangle([margin, margin + bar - 10 * s, margin + ww, margin + bar], fill=(44, 44, 44))
        _traffic(d, margin + 20 * s, margin + bar / 2, s, 6)
        t = "frx@userbot: ~/modules — bash"
        d.text((margin + (ww - ui.getlength(t)) / 2, margin + 11 * s), t, font=ui, fill=(190, 190, 190))
        y = margin + bar + pad
        x = margin + pad
        asc, desc = c.font.getmetrics()
        dy = (c.lh - asc - desc) // 2
        parts = [("frx@userbot", (35, 209, 139)), (":", (204, 204, 204)), ("~/modules", (59, 142, 234)),
                 (f" $ cat {fname}", (204, 204, 204))]
        cx = x
        for text, col in parts:
            d.text((cx, y + dy), text, font=c.bold, fill=col)
            cx += c.cw * len(text)
        c.draw_code(d, x, y + c.lh * 1.5, rows, pal)
        yy = y + c.lh * 1.5 + ch + c.lh * 0.3
        cx = x
        for text, col in parts[:3] + [(" $ ", (204, 204, 204))]:
            d.text((cx, yy + dy), text, font=c.bold, fill=col)
            cx += c.cw * len(text)
        d.rectangle([cx, yy + dy, cx + c.cw, yy + dy + asc + desc], fill=(204, 204, 204))

    elif style == 'github':
        gutter = int(c.cw * (len(str(len(raw))) + 4))
        cw, ch = c.code_size(rows, gutter)
        head, margin = 46 * s, 28 * s
        ww, wh = max(cw + 30 * s, 560 * s), head + ch + 24 * s
        W, H = ww + margin * 2, wh + margin * 2
        page = (246, 248, 250) if not dark else (1, 4, 9)
        img = Image.new('RGBA', (W, H), page + (255,))
        d = ImageDraw.Draw(img)
        border = (208, 215, 222) if not dark else (48, 54, 61)
        box = (margin, margin, margin + ww, margin + wh)
        d.rounded_rectangle(box, 8 * s, fill=_rgb(pal['bg']), outline=border, width=s)
        hb = (246, 248, 250) if not dark else (22, 27, 34)
        d.rounded_rectangle([margin, margin, margin + ww, margin + head], 8 * s, fill=hb, outline=border, width=s)
        d.rectangle([margin + s, margin + head - 8 * s, margin + ww - s, margin + head - s], fill=hb)
        d.line([margin, margin + head, margin + ww, margin + head], fill=border, width=s)
        hc = (31, 35, 40) if not dark else (230, 237, 243)
        d.rounded_rectangle([margin + 16 * s, margin + 14 * s, margin + 27 * s, margin + 30 * s], 2 * s, outline=hc, width=s)
        d.text((margin + 36 * s, margin + 15 * s), fname, font=ui_b, fill=hc)
        meta = f"{len(raw)} lines · {lname}"
        d.text((margin + ww - ui.getlength(meta) - 90 * s, margin + 15 * s), meta, font=ui, fill=(101, 109, 118))
        d.rounded_rectangle([margin + ww - 76 * s, margin + 10 * s, margin + ww - 14 * s, margin + head - 10 * s], 6 * s,
                            outline=border, width=s, fill=_rgb(pal['bg']))
        d.text((margin + ww - 62 * s, margin + 15 * s), "Raw", font=ui_b, fill=hc)
        c.draw_code(d, margin + 4 * s, margin + head + 12 * s, rows, pal, gutter=gutter, gutter_color=(140, 149, 159))

    elif style == 'notepad':
        cw, ch = c.code_size(rows, 0)
        title, menu, status, margin = 40 * s, 32 * s, 26 * s, 34 * s
        ww, wh = max(cw + 40 * s, 560 * s), title + menu + ch + 26 * s + status
        W, H = ww + margin * 2, wh + margin * 2
        img = _gradient(W, H, (120, 170, 230), (180, 140, 220)) if not bg else Image.new('RGBA', (W, H), _rgb(bg) + (255,))
        box = (margin, margin, margin + ww, margin + wh)
        _shadow(img, box, 8 * s, blur=20 * s // 2, offset=8 * s, alpha=110)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, 8 * s, fill=(243, 243, 243))
        d.rounded_rectangle([margin + 16 * s, margin + 12 * s, margin + 30 * s, margin + 28 * s], 2 * s, fill=(255, 255, 255),
                            outline=(30, 120, 210), width=int(1.5 * s))
        d.rounded_rectangle([margin + 40 * s, margin + 6 * s, margin + 40 * s + ui.getlength(fname) + 50 * s, margin + title],
                            6 * s, fill=(251, 251, 251))
        d.text((margin + 54 * s, margin + 14 * s), fname, font=ui, fill=(27, 27, 27))
        _win_controls(d, margin + ww, margin + title / 2, s, (27, 27, 27))
        my = margin + title
        d.rectangle([margin, my, margin + ww, my + menu], fill=(251, 251, 251))
        for i, m in enumerate(['Файл', 'Правка', 'Просмотр']):
            d.text((margin + 16 * s + i * 76 * s, my + 8 * s), m, font=ui, fill=(27, 27, 27))
        d.rectangle([margin, my + menu, margin + ww, margin + wh - status], fill=_rgb(pal['bg']))
        c.draw_code(d, margin + 16 * s, my + menu + 12 * s, rows, pal)
        sy = margin + wh - status
        d.text((margin + 16 * s, sy + 6 * s), f"Стр 1, стлб 1    {len(code)} символов", font=ui, fill=(90, 90, 90))
        d.text((margin + ww - 260 * s, sy + 6 * s), "100%    Windows (CRLF)    UTF-8", font=ui, fill=(90, 90, 90))

    elif style == 'retro':
        cw, ch = c.code_size(rows, 0)
        pad, bezel = 40 * s, 36 * s
        sw, sh = max(cw + pad * 2, 560 * s), ch + pad * 2 + c.lh
        W, H = sw + bezel * 2, sh + bezel * 2 + 30 * s
        img = Image.new('RGBA', (W, H), (40, 38, 34, 255))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([6 * s, 6 * s, W - 6 * s, H - 6 * s], 26 * s, fill=(196, 188, 168))
        d.rounded_rectangle([bezel - 8 * s, bezel - 8 * s, bezel + sw + 8 * s, bezel + sh + 8 * s], 22 * s, fill=(70, 66, 58))
        d.rounded_rectangle([bezel, bezel, bezel + sw, bezel + sh], 18 * s, fill=_rgb(pal['bg']))
        from PIL import ImageFilter
        layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        dl = ImageDraw.Draw(layer)
        c.draw_code(dl, bezel + pad, bezel + pad, rows, pal)
        asc, desc = c.font.getmetrics()
        yy = bezel + pad + ch + (c.lh - asc - desc) // 2
        dl.text((bezel + pad, yy), "READY.", font=c.font, fill=_rgb(pal['fg']))
        dl.rectangle([bezel + pad + c.cw * 7, yy, bezel + pad + c.cw * 8, yy + asc + desc], fill=_rgb(pal['fg']))
        img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(5 * s)))     # свечение люминофора
        img.alpha_composite(layer)
        lines = Image.new('RGBA', img.size, (0, 0, 0, 0))
        dln = ImageDraw.Draw(lines)
        for y in range(bezel, bezel + sh, 4 * s):     # строки развёртки — едва заметные
            dln.line([bezel, y, bezel + sw, y], fill=(0, 0, 0, 55), width=s)
        img.alpha_composite(lines)
        d = ImageDraw.Draw(img)
        d.text((W / 2 - ui_b.getlength("FRX-80") / 2, H - 34 * s), "FRX-80", font=ui_b, fill=(110, 102, 86))
        d.ellipse([W - 60 * s, H - 30 * s, W - 48 * s, H - 18 * s], fill=(90, 220, 120))

    else:  # paper
        gutter = int(c.cw * (len(str(len(raw))) + 4))
        cw, ch = c.code_size(rows, gutter)
        margin = 46 * s
        W, H = max(cw + margin * 2, 560 * s), ch + margin * 2 + 30 * s
        img = Image.new('RGBA', (W, H), _rgb(pal['bg']) + (255,))
        d = ImageDraw.Draw(img)
        d.text((margin, margin - 14 * s), f"Листинг 1. {fname}", font=ui_b, fill=_mix(pal['fg'], pal['bg'], 0.35))
        d.line([margin, margin + 10 * s, W - margin, margin + 10 * s], fill=_mix(pal['fg'], pal['bg'], 0.75), width=s)
        c.draw_code(d, margin - gutter * 0.25, margin + 26 * s, rows, pal, gutter=gutter, gutter_color=numc)
        d.line([margin, H - margin + 10 * s, W - margin, H - margin + 10 * s], fill=_mix(pal['fg'], pal['bg'], 0.75), width=s)

    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'PNG', optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- carbonara

CARBON_THEMES = {'dracula': 'dracula-pro', 'one-dark': 'one-dark', 'monokai': 'monokai', 'nord': 'nord',
                 'night-owl': 'night-owl', 'material': 'material', 'solarized-dark': 'solarized dark',
                 'solarized-light': 'solarized light', 'zenburn': 'zenburn', 'vscode-dark': 'vscode'}


async def carbonara(code, theme, lang, bg):
    payload = {'code': code, 'theme': CARBON_THEMES.get(theme or 'dracula', 'dracula-pro'), 'backgroundColor': bg or '#abb8c3',
               'language': lang or 'auto', 'paddingVertical': '48px', 'paddingHorizontal': '48px', 'dropShadow': True,
               'windowControls': True, 'widthAdjustment': True, 'fontFamily': 'JetBrains Mono', 'fontSize': '15px',
               'exportSize': '2x'}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
        async with s.post('https://carbonara.solopov.dev/api/cook', json=payload) as r:
            if r.status != 200 or not r.headers.get('Content-Type', '').startswith('image'):
                raise RuntimeError(f"Carbonara ответил {r.status}")
            return await r.read()


# --------------------------------------------------------------------------- команды

@decorators.on_start()
async def setup():
    await engine.create_config(key='style', description='Стиль по умолчанию', data_type='choice', default='vscode',
                               options=list(STYLES))
    await engine.create_config(key='theme', description='Тема подсветки по умолчанию (auto — своя у каждого стиля)',
                               data_type='choice', default='auto', options=['auto'] + THEMES)
    await engine.create_config(key='background', description='Цвет фона вокруг окна для mac/carbon/terminal/notepad '
                               '(#ff5a14; пусто — фирменный градиент)', data_type='str', default='')


def _pick(word):
    w = (word or '').strip().lower()
    if w in STYLES:
        return 'style', w
    if w in ALIASES:
        return 'style', ALIASES[w]
    if w in THEMES:
        return 'theme', w
    return None, None


@decorators.command('code', 'codestyle', 'cs')
async def code_cmd(event):
    """картинка из кода: [стиль] [тема] — ответом на код / файл или код со второй строки"""
    args = engine.get_args()
    head, _, rest = args.partition('\n')
    style = theme = None
    words = head.split()
    used = 0
    for w in words[:2]:
        kind, val = _pick(w)
        if kind == 'style' and style is None:
            style, used = val, used + 1
        elif kind == 'theme' and theme is None:
            theme, used = val, used + 1
        else:
            break
    if used:
        code = (' '.join(words[used:]) + ('\n' if words[used:] and rest else '') + rest) if (words[used:] or rest) else ''
    else:
        code = args
    lang = filename = None
    if not code.strip() and event.is_reply:
        r = await event.get_reply_message()
        if r and r.file and (r.file.size or 0) < 300_000 and not r.photo:
            raw = await r.download_media(file=bytes)
            code = raw.decode('utf-8', 'replace')
            filename = r.file.name or None
            lang = EXT_LANG.get(os.path.splitext(filename or '')[1].lower())
        elif r:
            code = r.raw_text or ''
    if not code.strip():
        await engine.answer(event, visual.err("Нет кода") + "\n" + visual.hint(".code [стиль] [тема] — ответом на код или файл · .codestyles — список"))
        return
    if code.count('\n') > 400 or len(code) > 20000:
        await engine.answer(event, visual.err("Слишком длинный код (до 400 строк / 20 000 символов)"))
        return
    style = style or await engine.get_config('style') or 'vscode'
    if theme is None:
        t = await engine.get_config('theme')
        theme = None if t in (None, '', 'auto') else t
    bg = (await engine.get_config('background') or '').strip() or None
    await engine.answer(event, visual.wait(f"Рисую · {STYLES[style][0]}…"))
    try:
        await _deps()
        png = None
        if style == 'carbon':
            try:
                png = await carbonara(code, theme, lang, bg)
            except Exception:
                png = None
        if png is None:
            fonts = await _fonts()
            png = await asyncio.get_running_loop().run_in_executor(
                None, lambda: render(code, style, theme, lang, filename, fonts, bg))
    except Exception as e:
        await engine.answer(event, visual.err("Не получилось") + f"\n<code>{_esc(e)}</code>")
        return
    f = io.BytesIO(png)
    f.name = 'code.png'
    from PIL import Image
    w, h = Image.open(io.BytesIO(png)).size
    as_doc = h > w * 3 or max(w, h) > 4000          # очень длинное Telegram пожмёт до каши — шлём файлом
    await engine.client.send_file(event.chat_id, f, force_document=as_doc, reply_to=event.reply_to_msg_id)
    await event.delete()


@decorators.command('codestyles')
async def styles_cmd(event):
    """стили и темы для .code"""
    lines = [visual.title("CodeStyles", sub=".code [стиль] [тема]")]
    lines += [visual.item(f"<code>{k}</code> — {_esc(v[0])}: {_esc(v[1])}") for k, v in STYLES.items()]
    lines.append("")
    lines.append(visual.section("Темы подсветки"))
    lines.append(", ".join(f"<code>{t}</code>" for t in THEMES))
    lines.append("")
    lines.append(visual.hint("например: .code vscode · .code mac nord · .code terminal monokai"))
    await engine.answer(event, "\n".join(lines))
