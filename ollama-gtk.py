#!/usr/bin/env python3
"""
Ollama Chat — GTK3 frontend  (v5 — "99% done")

New in this version:
  • Big-Brain payload: num_ctx=16384, num_predict=-1, num_gpu=99,
    repeat_penalty=1.1 — unlocks full VRAM context for long scripts
  • Token batching: UI updates every 100 ms instead of per-character,
    reducing CPU/GPU chatter and coil whine on discrete GPUs
  • Session persistence: history auto-saved to
    ~/.config/ollama-chat/history.json on every response and on quit;
    previous session is offered on startup
  • Context Meter: colour-coded progress bar below the toolbar showing
    estimated context fill (chars ÷ num_ctx×4).  Green → yellow → red.
  • Timeout raised from 120 s → 300 s for large context pre-fills
  • max_history default raised to 100 (let num_ctx do the real limiting)
  • num_ctx exposed as a Settings slider (2k / 4k / 8k / 16k / 32k)
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

import json
import os
import re
import time
import threading
import urllib.request
import urllib.error


# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_API_URL     = "http://localhost:11434/api/chat"
DEFAULT_SYSTEM      = ""
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_HISTORY = 100          # high count; num_ctx is the real limit
DEFAULT_NUM_CTX     = 16384        # tokens; ~16 k fits RX 6800 16 GB easily
DEFAULT_NUM_PREDICT = 4096         # explicit output cap; -1 can be ignored by some models
DEFAULT_NUM_GPU     = 99           # offload all layers to VRAM
HISTORY_PATH        = os.path.expanduser("~/.config/ollama-chat/history.json")
REQUEST_TIMEOUT     = 300          # seconds — large ctx pre-fills can take time
TOKEN_BATCH_MS      = 0.10         # flush token buffer every 100 ms


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\x1B\[[0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)', '', text)
    text = re.sub(r'\x1B[@-_]', '', text)
    return text.replace('\r\n', '\n').replace('\r', '\n').replace('\b', '')


def estimate_tokens(history: list[dict]) -> int:
    """
    Rough token estimate: 1 token ≈ 4 characters of English text.
    Good enough for a progress bar; not a true tokeniser.
    """
    total_chars = sum(len(m.get("content", "")) for m in history)
    return total_chars // 4


# ─────────────────────────────────────────────────────────────────────────────
# Syntax patterns — compiled once at import time
# ─────────────────────────────────────────────────────────────────────────────

_C_STR = r'"(?:[^"\\]|\\.)*"'
_C_COM = r'(?://[^\n]*|/\*[\s\S]*?\*/)'
_C_NUM = r'\b0x[0-9a-fA-F]+\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?[uUlLfF]?\b'

_RAW_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "python": [
        ("number",    r'\b0x[0-9a-fA-F]+\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?\b'),
        ("builtin",   r'\b(?:print|len|range|int|str|float|list|dict|set|tuple|bool|'
                      r'type|input|open|super|self|cls|enumerate|zip|map|filter|sorted|'
                      r'reversed|sum|min|max|abs|round|isinstance|issubclass|hasattr|'
                      r'getattr|setattr|vars|dir|id|hash|repr|eval|exec|compile|object|'
                      r'property|staticmethod|classmethod|next|iter|any|all|chr|ord|'
                      r'bin|hex|oct|pow|divmod|format|bytes|bytearray|memoryview)\b'),
        ("keyword",   r'\b(?:False|None|True|and|as|assert|async|await|break|class|'
                      r'continue|def|del|elif|else|except|finally|for|from|global|if|'
                      r'import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|'
                      r'while|with|yield)\b'),
        ("decorator", r'@\w+(?:\.\w+)*'),
        ("string",    r'(?:f|b|r|rb|br|u)?(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|'
                      r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'),
        ("comment",   r'#[^\n]*'),
    ],
    "javascript": [
        ("number",   r'\b\d+\.?\d*(?:[eE][+-]?\d+)?\b'),
        ("builtin",  r'\b(?:console|window|document|Math|JSON|Array|Object|String|'
                     r'Number|Boolean|RegExp|Date|Error|Promise|Symbol|Map|Set|'
                     r'WeakMap|WeakSet|undefined|null|NaN|Infinity|setTimeout|'
                     r'setInterval|clearTimeout|clearInterval|fetch|require|module|'
                     r'exports|process|Buffer|globalThis)\b'),
        ("keyword",  r'\b(?:break|case|catch|class|const|continue|debugger|default|'
                     r'delete|do|else|export|extends|finally|for|function|if|import|'
                     r'in|instanceof|let|new|of|return|static|super|switch|this|throw|'
                     r'try|typeof|var|void|while|with|yield|async|await)\b'),
        ("string",   r'(?:`[^`]*`|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'),
        ("comment",  _C_COM),
    ],
    "c": [
        ("number",       _C_NUM),
        ("preprocessor", r'^\s*#\w+'),
        ("keyword",      r'\b(?:auto|break|case|char|const|continue|default|do|double|'
                         r'else|enum|extern|float|for|goto|if|inline|int|long|register|'
                         r'return|short|signed|sizeof|static|struct|switch|typedef|union|'
                         r'unsigned|void|volatile|while|_Bool|_Complex|restrict)\b'),
        ("string",       _C_STR),
        ("comment",      _C_COM),
    ],
    "cpp": [
        ("number",       _C_NUM),
        ("preprocessor", r'^\s*#\w+'),
        ("keyword",      r'\b(?:alignas|alignof|and|auto|bool|break|case|catch|char|'
                         r'class|const|constexpr|consteval|constinit|continue|decltype|'
                         r'default|delete|do|double|else|enum|explicit|export|extern|'
                         r'false|float|for|friend|goto|if|inline|int|long|mutable|'
                         r'namespace|new|noexcept|nullptr|operator|override|private|'
                         r'protected|public|return|short|signed|sizeof|static|'
                         r'static_assert|struct|switch|template|this|throw|true|try|'
                         r'typedef|typename|union|unsigned|using|virtual|void|'
                         r'volatile|wchar_t|while)\b'),
        ("string",       _C_STR),
        ("comment",      _C_COM),
    ],
    "bash": [
        ("number",   r'\b\d+\b'),
        ("builtin",  r'\b(?:echo|cd|ls|pwd|mkdir|rm|cp|mv|cat|grep|sed|awk|chmod|'
                     r'chown|sudo|apt|pip|git|curl|wget|source|alias|printf|read|'
                     r'test|true|false|which|find|sort|uniq|cut|head|tail|wc|'
                     r'xargs|env|eval|exec|kill|ps|export|declare|local|unset)\b'),
        ("keyword",  r'\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|'
                     r'function|return|in|select|until|break|continue|exit|'
                     r'shift|set|unset|trap|wait)\b'),
        ("variable", r'\$\{?[A-Za-z_]\w*\}?|\$[0-9#?@*!$-]'),
        ("string",   r'"(?:[^"\\]|\\.)*"'),
        ("comment",  r'#[^\n]*'),
    ],
    "html": [
        ("string",  r'"[^"]*"|\'[^\']*\''),
        ("comment", r'<!--[\s\S]*?-->'),
        ("builtin", r'\b(?:href|src|class|id|style|type|name|value|alt|title|rel|'
                    r'charset|content|lang)\b'),
        ("keyword", r'</?\s*\w[\w.-]*|/?>|<!DOCTYPE'),
    ],
    "css": [
        ("string",    r'"[^"]*"|\'[^\']*\''),
        ("comment",   r'/\*[\s\S]*?\*/'),
        ("number",    r'\b\d+\.?\d*(?:px|em|rem|%|vh|vw|pt|cm|mm|s|ms|deg|fr)?\b'),
        ("builtin",   r'#[0-9a-fA-F]{3,8}\b'),
        ("keyword",   r'\b(?:color|background|font|margin|padding|border|display|'
                      r'position|width|height|flex|grid|align|justify|overflow|'
                      r'transform|transition|animation|content|top|left|right|bottom|'
                      r'opacity|cursor|none|block|inline|relative|absolute|'
                      r'fixed|sticky|auto|center|inherit|important)\b'),
        ("decorator", r'@\w+|:[\w-]+(?:\([^)]*\))?|::[\w-]+'),
    ],
    "rust": [
        ("number",    r'\b\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?'
                      r'(?:u8|u16|u32|u64|u128|usize|i8|i16|i32|i64|i128|isize|f32|f64)?\b'),
        ("builtin",   r'\b(?:println!|print!|eprintln!|eprint!|vec!|format!|assert!|'
                      r'assert_eq!|panic!|todo!|unimplemented!|dbg!|Some|None|Ok|Err|'
                      r'Box|String|Vec|Option|Result|bool|u8|u16|u32|u64|i8|i16|i32|'
                      r'i64|f32|f64|usize|isize|str|char|std)\b'),
        ("keyword",   r'\b(?:as|async|await|break|const|continue|crate|dyn|else|enum|'
                      r'extern|false|fn|for|if|impl|in|let|loop|match|mod|move|mut|pub|'
                      r'ref|return|self|Self|static|struct|super|trait|true|type|unsafe|'
                      r'use|where|while)\b'),
        ("decorator", r'#!?\[[\s\S]*?\]'),
        ("string",    r'r#*"[\s\S]*?"#*|b?"(?:[^"\\]|\\.)*"|b?\'(?:[^\'\\]|\\.)*\''),
        ("comment",   r'(?://[^\n]*|/\*[\s\S]*?\*/)'),
    ],
    "go": [
        ("number",  r'\b\d+\.?\d*\b'),
        ("builtin", r'\b(?:append|cap|close|complex|copy|delete|imag|len|make|new|'
                    r'panic|print|println|real|recover|bool|byte|complex64|complex128|'
                    r'error|float32|float64|int|int8|int16|int32|int64|rune|string|'
                    r'uint|uint8|uint16|uint32|uint64|uintptr|nil|true|false|iota)\b'),
        ("keyword", r'\b(?:break|case|chan|const|continue|default|defer|else|'
                    r'fallthrough|for|func|go|goto|if|import|interface|map|package|'
                    r'range|return|select|struct|switch|type|var)\b'),
        ("string",  r'`[^`]*`|"(?:[^"\\]|\\.)*"'),
        ("comment", _C_COM),
    ],
}

LANG_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    lang: [(name, re.compile(pat, re.MULTILINE)) for name, pat in rules]
    for lang, rules in _RAW_PATTERNS.items()
}

LANG_ALIASES: dict[str, str] = {
    "js": "javascript", "jsx": "javascript",
    "ts": "javascript", "tsx": "javascript", "typescript": "javascript",
    "node": "javascript", "mjs": "javascript", "cjs": "javascript",
    "c++": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "h": "c",
    "sh": "bash", "shell": "bash", "zsh": "bash", "fish": "bash",
    "py": "python", "python3": "python",
    "rs": "rust", "golang": "go",
    "xml": "html", "svg": "html", "htm": "html",
}

LANG_DISPLAY: dict[str, str] = {
    "python": "Python", "javascript": "JavaScript",
    "c": "C", "cpp": "C++", "rust": "Rust", "go": "Go",
    "bash": "Shell", "html": "HTML", "css": "CSS",
}

_RE_FENCE  = re.compile(r'```(\w*)\n?([\s\S]*?)```', re.MULTILINE)
_RE_INLINE = re.compile(r'`([^`\n]+)`')
_RE_BOLD   = re.compile(r'\*\*([^*\n]+)\*\*')
_RE_BULLET = re.compile(r'^[ \t]*[\*\+\-][ \t]+', re.MULTILINE)
_RE_LINK   = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')


# ─────────────────────────────────────────────────────────────────────────────
# VS Code Dark+ palette
# ─────────────────────────────────────────────────────────────────────────────

DARK_BG        = "#1E1E1E"
DARK_BG_PANEL  = "#252526"
DARK_BG_INPUT  = "#2D2D2D"
DARK_BG_CODE   = "#2D2D30"
DARK_BG_HEADER = "#37373D"
DARK_BG_INLINE = "#333337"
DARK_FG        = "#D4D4D4"
DARK_FG_DIM    = "#858585"

NP_THEME = {
    "keyword":      ("#569CD6", True,  False),
    "builtin":      ("#4EC9B0", False, False),
    "string":       ("#CE9178", False, False),
    "comment":      ("#6A9955", False, True),
    "number":       ("#B5CEA8", False, False),
    "decorator":    ("#C586C0", False, False),
    "variable":     ("#9CDCFE", False, False),
    "preprocessor": ("#C586C0", False, False),
    "operator":     ("#D4D4D4", False, False),
    "user_fg":      "#4EC9B0",
    "model_fg":     "#CE9178",
    "system_fg":    "#6A9955",
}


# ─────────────────────────────────────────────────────────────────────────────
# Syntax highlighter
# ─────────────────────────────────────────────────────────────────────────────

class SyntaxHighlighter:
    def __init__(self, buf: Gtk.TextBuffer):
        self.buf = buf
        self._build_tags()

    def _build_tags(self):
        b = self.buf
        for name, (fg, bold, italic) in {
            k: v for k, v in NP_THEME.items() if isinstance(v, tuple)
        }.items():
            props: dict = {"foreground": fg}
            if bold:   props["weight"] = Pango.Weight.BOLD
            if italic: props["style"]  = Pango.Style.ITALIC
            b.create_tag(f"syn_{name}", **props)

        b.create_tag("code_block",
            family="Monospace", size_points=10,
            foreground=DARK_FG,
            background=DARK_BG_CODE,
            paragraph_background=DARK_BG_CODE,
            left_margin=18, right_margin=18)

        b.create_tag("code_header",
            family="Monospace", size_points=9,
            foreground=DARK_FG_DIM,
            background=DARK_BG_HEADER,
            paragraph_background=DARK_BG_HEADER,
            left_margin=18, right_margin=18)

        b.create_tag("inline_code",
            family="Monospace", size_points=10,
            foreground="#D7BA7D",
            background=DARK_BG_INLINE)

        b.create_tag("bold_text",   weight=Pango.Weight.BOLD)
        b.create_tag("bullet_item", left_margin=28)
        b.create_tag("md_link",
            foreground="#3794FF",
            underline=Pango.Underline.SINGLE)
        b.create_tag("thinking_placeholder",
            foreground=DARK_FG_DIM,
            style=Pango.Style.ITALIC)

        b.create_tag("user_label",
            foreground=NP_THEME["user_fg"],
            weight=Pango.Weight.BOLD, size_points=11)
        b.create_tag("model_label",
            foreground=NP_THEME["model_fg"],
            weight=Pango.Weight.BOLD, size_points=11)
        b.create_tag("system_msg",
            foreground=NP_THEME["system_fg"],
            style=Pango.Style.ITALIC)

    def highlight_code_range(self, start_off: int, end_off: int, lang: str):
        lang     = LANG_ALIASES.get(lang, lang)
        patterns = LANG_PATTERNS.get(lang)
        if not patterns:
            return
        si    = self.buf.get_iter_at_offset(start_off)
        ei    = self.buf.get_iter_at_offset(end_off)
        text  = self.buf.get_text(si, ei, False)
        table = self.buf.get_tag_table()
        for tag_name, compiled in patterns:
            tag = table.lookup(f"syn_{tag_name}")
            if tag is None:
                continue
            for m in compiled.finditer(text):
                s = self.buf.get_iter_at_offset(start_off + m.start())
                e = self.buf.get_iter_at_offset(start_off + m.end())
                self.buf.apply_tag(tag, s, e)

    def highlight_message(self, base_off: int, text: str):
        buf         = self.buf
        fence_spans = [(m.start(), m.end()) for m in _RE_FENCE.finditer(text)]

        def in_fence(pos):
            return any(s <= pos <= e for s, e in fence_spans)

        for m in _RE_FENCE.finditer(text):
            lang  = LANG_ALIASES.get(m.group(1).strip().lower(),
                                     m.group(1).strip().lower())
            blk_s = buf.get_iter_at_offset(base_off + m.start())
            blk_e = buf.get_iter_at_offset(base_off + m.end())
            buf.apply_tag_by_name("code_block", blk_s, blk_e)
            if lang:
                self.highlight_code_range(
                    base_off + m.start(2), base_off + m.end(2), lang)

        for m in _RE_INLINE.finditer(text):
            if in_fence(m.start()):
                continue
            s = buf.get_iter_at_offset(base_off + m.start())
            e = buf.get_iter_at_offset(base_off + m.end())
            buf.apply_tag_by_name("inline_code", s, e)

        for m in _RE_BOLD.finditer(text):
            s = buf.get_iter_at_offset(base_off + m.start())
            e = buf.get_iter_at_offset(base_off + m.end())
            buf.apply_tag_by_name("bold_text", s, e)

        for m in _RE_BULLET.finditer(text):
            ls = buf.get_iter_at_offset(base_off + m.start())
            le = ls.copy()
            le.forward_to_line_end()
            buf.apply_tag_by_name("bullet_item", ls, le)

        for m in _RE_LINK.finditer(text):
            s = buf.get_iter_at_offset(base_off + m.start())
            e = buf.get_iter_at_offset(base_off + m.end())
            buf.apply_tag_by_name("md_link", s, e)

    def insert_code_headers(self, base_off: int, text: str) -> int:
        inserted = 0
        buf = self.buf
        for m in _RE_FENCE.finditer(text):
            raw_lang = m.group(1).strip().lower()
            lang     = LANG_ALIASES.get(raw_lang, raw_lang)
            display  = LANG_DISPLAY.get(lang, lang.capitalize() if lang else "")
            if not display:
                continue
            header_text = f"  {display}\n"
            pos = buf.get_iter_at_offset(base_off + m.start() + inserted)
            buf.insert_with_tags_by_name(pos, header_text, "code_header")
            inserted += len(header_text)
        return inserted


# ─────────────────────────────────────────────────────────────────────────────
# Settings popover
# ─────────────────────────────────────────────────────────────────────────────

class SettingsPopover(Gtk.Popover):
    CTX_OPTIONS = [2048, 4096, 8192, 16384, 32768]

    def __init__(self, parent_btn, app):
        super().__init__()
        self.set_relative_to(parent_btn)
        self.app = app

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(10)
        grid.set_border_width(14)
        self.add(grid)

        def lbl(t):
            l = Gtk.Label(label=t)
            l.set_halign(Gtk.Align.END)
            return l

        # API URL
        grid.attach(lbl("API URL"), 0, 0, 1, 1)
        self.url_entry = Gtk.Entry()
        self.url_entry.set_width_chars(36)
        self.url_entry.set_text(app.api_url)
        grid.attach(self.url_entry, 1, 0, 2, 1)

        # System prompt
        grid.attach(lbl("System\nPrompt"), 0, 1, 1, 1)
        sys_scroll = Gtk.ScrolledWindow()
        sys_scroll.set_min_content_height(70)
        sys_scroll.set_max_content_height(120)
        sys_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sys_view = Gtk.TextView()
        self.sys_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.sys_view.set_left_margin(4)
        self.sys_view.set_right_margin(4)
        self.sys_buf = self.sys_view.get_buffer()
        self.sys_buf.set_text(app.system_prompt)
        sys_scroll.add(self.sys_view)
        grid.attach(sys_scroll, 1, 1, 2, 1)

        # Temperature
        grid.attach(lbl("Temperature"), 0, 2, 1, 1)
        self.temp_spin = Gtk.SpinButton()
        self.temp_spin.set_adjustment(
            Gtk.Adjustment(value=app.temperature, lower=0.0, upper=2.0,
                           step_increment=0.05, page_increment=0.1))
        self.temp_spin.set_digits(2)
        grid.attach(self.temp_spin, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="(0 = deterministic, 2 = creative)"), 2, 2, 1, 1)

        # Context window size
        grid.attach(lbl("Context Size\n(num_ctx)"), 0, 3, 1, 1)
        self.ctx_combo = Gtk.ComboBoxText()
        current_idx = 0
        for i, v in enumerate(self.CTX_OPTIONS):
            self.ctx_combo.append_text(f"{v:,} tokens")
            if v == app.num_ctx:
                current_idx = i
        self.ctx_combo.set_active(current_idx)
        grid.attach(self.ctx_combo, 1, 3, 1, 1)
        grid.attach(Gtk.Label(label="↑ higher = more memory used"), 2, 3, 1, 1)

        # Max history messages
        grid.attach(lbl("Max History\nMessages"), 0, 4, 1, 1)
        self.hist_spin = Gtk.SpinButton()
        self.hist_spin.set_adjustment(
            Gtk.Adjustment(value=app.max_history, lower=2, upper=500,
                           step_increment=2, page_increment=10))
        self.hist_spin.set_digits(0)
        grid.attach(self.hist_spin, 1, 4, 1, 1)
        grid.attach(Gtk.Label(label="messages kept (num_ctx is the real cap)"), 2, 4, 1, 1)

        # Max output tokens
        grid.attach(lbl("Max Output\n(num_predict)"), 0, 5, 1, 1)
        self.predict_spin = Gtk.SpinButton()
        self.predict_spin.set_adjustment(
            Gtk.Adjustment(value=app.num_predict, lower=256, upper=32768,
                           step_increment=256, page_increment=1024))
        self.predict_spin.set_digits(0)
        grid.attach(self.predict_spin, 1, 5, 1, 1)
        grid.attach(Gtk.Label(label="tokens generated per response (4096 = ~3k words)"), 2, 5, 1, 1)

        apply_btn = Gtk.Button(label="Apply  (history cleared)")
        apply_btn.get_style_context().add_class("send-btn")
        apply_btn.connect("clicked", self._apply)
        grid.attach(apply_btn, 2, 6, 1, 1)

        self.show_all()

    def _apply(self, *_):
        self.app.api_url       = self.url_entry.get_text().strip()
        s, e = self.sys_buf.get_start_iter(), self.sys_buf.get_end_iter()
        self.app.system_prompt = self.sys_buf.get_text(s, e, False).strip()
        self.app.temperature   = self.temp_spin.get_value()
        self.app.num_ctx       = self.CTX_OPTIONS[self.ctx_combo.get_active()]
        self.app.num_predict   = int(self.predict_spin.get_value())
        self.app.max_history   = int(self.hist_spin.get_value())
        self.app.history.clear()
        self.app._update_context_meter()
        self.app._insert_sys("Settings applied — history cleared")
        self.popdown()


# ─────────────────────────────────────────────────────────────────────────────
# Main application window
# ─────────────────────────────────────────────────────────────────────────────

class OllamaChat(Gtk.Window):

    MODELS = [
        "llama3", "llama3.1", "llama3.2",
        "mistral", "mistral-nemo",
        "qwen2.5", "qwen2.5-coder",
        "codellama", "deepseek-coder",
        "phi3", "phi4",
        "gemma2", "gemma3",
    ]

    _DOTS = ["●∙∙", "∙●∙", "∙∙●"]

    def __init__(self):
        super().__init__(title="Ollama Chat")
        self.set_default_size(980, 760)

        self.api_url       = DEFAULT_API_URL
        self.system_prompt = DEFAULT_SYSTEM
        self.temperature   = DEFAULT_TEMPERATURE
        self.max_history   = DEFAULT_MAX_HISTORY
        self.num_ctx       = DEFAULT_NUM_CTX
        self.num_predict   = DEFAULT_NUM_PREDICT
        self.num_gpu       = DEFAULT_NUM_GPU

        self.model        = "llama3"
        self.busy         = False
        self._cancel_flag = False
        self._resp_start: int | None = None
        self._placeholder_mark  = None
        self._thinking_timer_id = None
        self._thinking_dot_idx  = 0
        self.history: list[dict] = []

        self._build_ui()
        self.highlighter = SyntaxHighlighter(self.chat_buf)
        self._apply_css()
        self._set_status("ready")
        self._register_shortcuts()
        self.connect("destroy", self._on_quit)

        # Offer to restore the last session
        GLib.idle_add(self._maybe_restore_session)

    # ── Persistence ───────────────────────────────────────────────────────

    def _get_history_path(self) -> str:
        folder = os.path.dirname(HISTORY_PATH)
        os.makedirs(folder, exist_ok=True)
        return HISTORY_PATH

    def _save_history(self):
        try:
            with open(self._get_history_path(), "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except OSError:
            pass    # non-fatal; don't crash if disk is full or read-only

    def _load_history(self) -> list[dict]:
        path = self._get_history_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _maybe_restore_session(self):
        saved = self._load_history()
        if not saved:
            return False    # GLib.idle_add won't re-schedule

        n_turns = sum(1 for m in saved if m.get("role") == "user")
        dialog  = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Restore previous session?",
        )
        dialog.format_secondary_text(
            f"Found {n_turns} previous turn(s) "
            f"({estimate_tokens(saved):,} ≈ tokens).  "
            f"Restore them into context?")
        dialog.add_buttons(
            "Start Fresh", Gtk.ResponseType.NO,
            "Restore Session", Gtk.ResponseType.YES)
        dialog.set_default_response(Gtk.ResponseType.YES)

        resp = dialog.run()
        dialog.destroy()

        if resp == Gtk.ResponseType.YES:
            self.history = saved
            self._insert_sys(
                f"Session restored — {n_turns} turn(s), "
                f"≈{estimate_tokens(self.history):,} tokens")
            self._update_context_meter()

        return False    # don't reschedule

    def _on_quit(self, *_):
        self._save_history()
        Gtk.main_quit()

    # ── Context meter helpers ─────────────────────────────────────────────

    def _update_context_meter(self):
        """Refresh the context-fill progress bar and its label."""
        used   = estimate_tokens(self.history)
        cap    = self.num_ctx
        frac   = min(used / cap, 1.0) if cap > 0 else 0.0

        self._ctx_bar.set_fraction(frac)
        pct = int(frac * 100)
        self._ctx_label.set_markup(
            f"<span foreground='{DARK_FG_DIM}' size='small'>"
            f"Context: {used:,} / {cap:,} tokens  ({pct}%)</span>")

        # Colour: green → yellow → red
        if frac < 0.6:
            colour = "#4EC9B0"   # teal / green
        elif frac < 0.85:
            colour = "#DCDCAA"   # yellow
        else:
            colour = "#F44747"   # red

        css = f"""
        progressbar trough {{ background-color: #333333; border-radius: 3px; }}
        progressbar progress {{ background-color: {colour}; border-radius: 3px; }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        self._ctx_bar.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── CSS ───────────────────────────────────────────────────────────────

    def _apply_css(self):
        css = f"""
        window, .main-box {{ background-color: {DARK_BG_PANEL}; }}
        .chat-view {{
            font-family: 'Segoe UI', 'Ubuntu', 'Cantarell', 'Noto Sans', sans-serif;
            font-size: 11pt;
            background-color: {DARK_BG};
            color: {DARK_FG};
            caret-color: {DARK_FG};
        }}
        .input-view {{
            font-family: 'Segoe UI', 'Ubuntu', 'Cantarell', 'Noto Sans', sans-serif;
            font-size: 11pt;
            background-color: {DARK_BG_INPUT};
            color: {DARK_FG};
            caret-color: {DARK_FG};
        }}
        textview text {{ background-color: transparent; color: {DARK_FG}; }}
        .chat-view  text {{ background-color: {DARK_BG}; }}
        .input-view text {{ background-color: {DARK_BG_INPUT}; }}
        scrolledwindow, viewport {{ background-color: {DARK_BG}; }}
        frame > border {{ border: 1px solid #444444; border-radius: 4px; }}
        .toolbar-box {{ background-color: {DARK_BG_PANEL}; }}
        .meter-box   {{ background-color: {DARK_BG_PANEL}; padding: 2px 8px 4px 8px; }}
        label {{ color: {DARK_FG}; }}
        .hint-label {{ color: {DARK_FG_DIM}; }}
        combobox button {{
            background-color: #3C3C3C; color: {DARK_FG};
            border: 1px solid #555555; border-radius: 4px;
        }}
        .send-btn {{
            background: #0E639C; color: #FFFFFF;
            border: none; border-radius: 5px;
            padding: 5px 16px; font-weight: bold;
        }}
        .send-btn:hover   {{ background: #1177BB; }}
        .send-btn:active  {{ background: #0A4D7A; }}
        .send-btn:disabled {{ background: #3C3C3C; color: #6A6A6A; }}
        .stop-btn {{
            background: #8B1A1A; color: #FFFFFF;
            border: none; border-radius: 5px;
            padding: 5px 16px; font-weight: bold;
        }}
        .stop-btn:hover  {{ background: #A52020; }}
        .stop-btn:active {{ background: #6A1010; }}
        .icon-btn {{
            background: transparent; color: {DARK_FG};
            border: 1px solid #555555; border-radius: 5px;
            padding: 4px 8px;
        }}
        .icon-btn:hover {{ background: #3C3C3C; }}
        .clear-btn {{
            background: #3C3C3C; color: {DARK_FG};
            border: 1px solid #555555;
            border-radius: 5px; padding: 5px 10px;
        }}
        .clear-btn:hover {{ background: #4C4C4C; }}
        separator {{ background-color: #333333; }}
        popover {{ background-color: {DARK_BG_PANEL}; }}
        popover entry, popover textview {{
            background-color: {DARK_BG_INPUT}; color: {DARK_FG};
        }}
        progressbar trough  {{ background-color: #333333; border-radius: 3px; min-height: 5px; }}
        progressbar progress {{ background-color: #4EC9B0; border-radius: 3px; min-height: 5px; }}
        """.encode()

        p = Gtk.CssProvider()
        p.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), p,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.get_style_context().add_class("main-box")
        self.add(root)

        # ── toolbar ──────────────────────────────────────────────────────
        toolbar = Gtk.Box(spacing=8)
        toolbar.set_border_width(8)
        toolbar.get_style_context().add_class("toolbar-box")
        root.pack_start(toolbar, False, False, 0)

        toolbar.pack_start(Gtk.Label(label="Model:"), False, False, 0)
        self.model_combo = Gtk.ComboBoxText()
        for m in self.MODELS:
            self.model_combo.append_text(m)
        self.model_combo.set_active(0)
        self.model_combo.connect("changed", self._on_model_changed)
        toolbar.pack_start(self.model_combo, False, False, 0)

        self.status_lbl = Gtk.Label()
        self.status_lbl.set_halign(Gtk.Align.END)
        toolbar.pack_end(self.status_lbl, False, False, 0)

        self._gear_btn = Gtk.Button(label="⚙")
        self._gear_btn.get_style_context().add_class("icon-btn")
        self._gear_btn.set_tooltip_text("Settings  (Ctrl+,)")
        self._gear_btn.connect("clicked", self._open_settings)
        toolbar.pack_end(self._gear_btn, False, False, 0)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.get_style_context().add_class("clear-btn")
        clear_btn.set_tooltip_text("Clear conversation  (Ctrl+L)")
        clear_btn.connect("clicked", self._clear)
        toolbar.pack_end(clear_btn, False, False, 0)

        # ── context meter ─────────────────────────────────────────────────
        meter_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meter_box.get_style_context().add_class("meter-box")
        root.pack_start(meter_box, False, False, 0)

        self._ctx_label = Gtk.Label()
        self._ctx_label.set_halign(Gtk.Align.START)
        meter_box.pack_start(self._ctx_label, False, False, 0)

        self._ctx_bar = Gtk.ProgressBar()
        self._ctx_bar.set_fraction(0.0)
        meter_box.pack_start(self._ctx_bar, False, False, 0)

        root.pack_start(Gtk.Separator(), False, False, 0)

        # ── chat display ──────────────────────────────────────────────────
        self.chat_view = Gtk.TextView()
        self.chat_view.set_editable(False)
        self.chat_view.set_cursor_visible(False)
        self.chat_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.chat_view.set_left_margin(14)
        self.chat_view.set_right_margin(14)
        self.chat_view.set_top_margin(10)
        self.chat_view.set_bottom_margin(10)
        self.chat_view.get_style_context().add_class("chat-view")
        self.chat_buf = self.chat_view.get_buffer()
        self.chat_view.connect("button-press-event", self._on_chat_click)

        self.chat_scroll = Gtk.ScrolledWindow()
        self.chat_scroll.set_vexpand(True)
        self.chat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.chat_scroll.add(self.chat_view)
        root.pack_start(self.chat_scroll, True, True, 0)

        root.pack_start(Gtk.Separator(), False, False, 0)

        # ── input area ────────────────────────────────────────────────────
        input_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        input_outer.set_border_width(8)
        input_outer.get_style_context().add_class("toolbar-box")
        root.pack_start(input_outer, False, False, 0)

        frame = Gtk.Frame()
        input_outer.pack_start(frame, False, False, 0)

        inp_scroll = Gtk.ScrolledWindow()
        inp_scroll.set_min_content_height(70)
        inp_scroll.set_max_content_height(220)
        inp_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame.add(inp_scroll)

        self.input_view = Gtk.TextView()
        self.input_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.input_view.set_left_margin(8)
        self.input_view.set_right_margin(8)
        self.input_view.set_top_margin(6)
        self.input_view.set_bottom_margin(6)
        self.input_view.get_style_context().add_class("input-view")
        self.input_buf = self.input_view.get_buffer()
        self.input_view.connect("key-press-event", self._on_key_press)
        inp_scroll.add(self.input_view)

        btn_row = Gtk.Box(spacing=8)
        input_outer.pack_start(btn_row, False, False, 0)

        hint = Gtk.Label(
            label="Ctrl+Enter = Send  ·  Ctrl+L = Clear  ·  Ctrl+, = Settings")
        hint.set_halign(Gtk.Align.START)
        hint.get_style_context().add_class("hint-label")
        btn_row.pack_start(hint, True, True, 0)

        self.stop_btn = Gtk.Button(label="■ Stop")
        self.stop_btn.get_style_context().add_class("stop-btn")
        self.stop_btn.connect("clicked", self._stop_generation)
        self.stop_btn.set_no_show_all(True)
        btn_row.pack_end(self.stop_btn, False, False, 0)

        self.send_btn = Gtk.Button(label="Send  (Ctrl+↵)")
        self.send_btn.get_style_context().add_class("send-btn")
        self.send_btn.connect("clicked", self._send)
        btn_row.pack_end(self.send_btn, False, False, 0)

        # Initialise the meter display with zeroed state
        self._update_context_meter()

    # ── Global shortcuts ──────────────────────────────────────────────────

    def _register_shortcuts(self):
        self.connect("key-press-event", self._on_window_key)

    def _on_window_key(self, _win, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if not ctrl:
            return False
        if event.keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self._clear()
            return True
        if event.keyval == Gdk.KEY_comma:
            self._open_settings(self._gear_btn)
            return True
        return False

    # ── Status ────────────────────────────────────────────────────────────

    def _set_status(self, state: str):
        table = {
            "ready":     (NP_THEME["model_fg"], "Ready"),
            "thinking":  ("#DCDCAA",            "Thinking…"),
            "streaming": (NP_THEME["user_fg"],  "Receiving…"),
            "stopped":   ("#F44747",            "Stopped"),
        }
        fg, label = table.get(state, ("#888888", state))
        self.status_lbl.set_markup(f"<span foreground='{fg}'>● {label}</span>")

    # ── Buffer / scroll helpers ───────────────────────────────────────────

    def _is_near_bottom(self) -> bool:
        adj = self.chat_scroll.get_vadjustment()
        return adj.get_value() + adj.get_page_size() >= adj.get_upper() - 20

    def _scroll_end(self):
        end  = self.chat_buf.get_end_iter()
        mark = self.chat_buf.create_mark(None, end, False)
        self.chat_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def _scroll_if_at_bottom(self):
        if self._is_near_bottom():
            self._scroll_end()

    def _insert_sys(self, text: str):
        e = self.chat_buf.get_end_iter()
        self.chat_buf.insert_with_tags_by_name(e, f"── {text} ──\n\n", "system_msg")
        self._scroll_end()

    def _clear(self, *_):
        self.chat_buf.set_text("")
        self.history.clear()
        self._save_history()
        self._update_context_meter()
        self._insert_sys("Conversation cleared")

    # ── Settings ──────────────────────────────────────────────────────────

    def _open_settings(self, btn, *_):
        SettingsPopover(btn, self).popup()

    # ── Right-click context menu ──────────────────────────────────────────

    def _on_chat_click(self, widget, event):
        if event.button != 3:
            return False
        coords = self.chat_view.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET, int(event.x), int(event.y))
        result = self.chat_view.get_iter_at_location(*coords)
        it     = result[1] if isinstance(result, tuple) else result

        table  = self.chat_buf.get_tag_table()
        cb_tag = table.lookup("code_block")
        in_cb  = bool(cb_tag and it.has_tag(cb_tag))

        menu = Gtk.Menu()
        if in_cb:
            start, end = it.copy(), it.copy()
            start.backward_to_tag_toggle(cb_tag)
            end.forward_to_tag_toggle(cb_tag)
            code_text = self.chat_buf.get_text(start, end, False)
            copy_code = Gtk.MenuItem(label="Copy Code Block")
            copy_code.connect(
                "activate",
                lambda *_: Gtk.Clipboard
                    .get(Gdk.SELECTION_CLIPBOARD)
                    .set_text(code_text, -1))
            menu.append(copy_code)
            menu.append(Gtk.SeparatorMenuItem())

        copy_sel = Gtk.MenuItem(label="Copy Selection")
        copy_sel.connect("activate",
                         lambda *_: self.chat_view.emit("copy-clipboard"))
        menu.append(copy_sel)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_model_changed(self, w):
        model_name = w.get_active_text()
        if not model_name:
            return

        # If Ollama is reachable, verify the model is actually downloaded.
        # If the server is down entirely we let it pass — the chat worker
        # will surface a clean error when the user tries to send.
        if not self._is_model_installed(model_name):
            self._prompt_install_model(model_name)
            return  # don't clear history or switch until install completes

        self.model = model_name
        self.history.clear()
        self._save_history()
        self._update_context_meter()
        self._insert_sys(f"Switched to {self.model}  (history cleared)")

    # ── Model management ──────────────────────────────────────────────────

    def _base_url(self) -> str:
        """Derive the Ollama root URL from the configured api_url."""
        # api_url is something like http://localhost:11434/api/chat
        # Strip everything from /api onwards to get http://localhost:11434
        idx = self.api_url.find("/api/")
        return self.api_url[:idx] if idx != -1 else self.api_url.rstrip("/")

    def _is_model_installed(self, model_name: str) -> bool:
        """
        Query /api/tags to see whether *model_name* is physically present.
        Returns True on any network error (let the chat worker handle that).
        """
        try:
            req = urllib.request.Request(
                f"{self._base_url()}/api/tags",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data      = json.loads(resp.read().decode())
                installed = [m.get("name", "") for m in data.get("models", [])]
                # Ollama tags models as "llama3:latest"; match on base name too
                for inst in installed:
                    if inst == model_name or inst.startswith(model_name + ":"):
                        return True
                return False
        except Exception:
            return True     # server unreachable — let chat attempt and fail cleanly

    def _prompt_install_model(self, model_name: str):
        """Ask the user if they want to pull the missing model."""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Model Not Installed",
        )
        dialog.format_secondary_text(
            f"'{model_name}' doesn't appear to be downloaded on this machine.\n\n"
            "Would you like to pull (download) it from Ollama now?\n"
            "Large models can be several GB — the download runs in the background.")
        resp = dialog.run()
        dialog.destroy()

        if resp == Gtk.ResponseType.YES:
            threading.Thread(
                target=self._pull_worker, args=(model_name,), daemon=True
            ).start()
        else:
            # Revert the combo box to whatever model was active before
            for i, m in enumerate(self.MODELS):
                if m == self.model:
                    self.model_combo.set_active(i)
                    break

    def _pull_worker(self, model_name: str):
        """
        Background thread: stream /api/pull progress into the status label.
        On success, switch the active model.  On failure, revert the combo.
        """
        GLib.idle_add(self.send_btn.set_sensitive, False)
        self.busy = True
        GLib.idle_add(self._insert_sys,
                      f"Downloading {model_name}… this may take several minutes.")

        payload = json.dumps({"name": model_name, "stream": True}).encode()
        try:
            req = urllib.request.Request(
                f"{self._base_url()}/api/pull",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                line_buf = ""
                for raw in resp:
                    line_buf += raw.decode(errors="replace")
                    if not line_buf.endswith("\n"):
                        continue
                    for line in line_buf.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj    = json.loads(line)
                            status = obj.get("status", "")
                            # Show download percentage when Ollama reports it
                            total     = obj.get("total", 0)
                            completed = obj.get("completed", 0)
                            if total and completed:
                                pct    = int(completed / total * 100)
                                status = f"{status}  {pct}%"
                            if status:
                                GLib.idle_add(self._set_status, status)
                        except json.JSONDecodeError:
                            pass
                    line_buf = ""

            # ── Success ───────────────────────────────────────────────────
            GLib.idle_add(self._insert_sys,
                          f"✓ {model_name} installed successfully")
            self.model = model_name
            self.history.clear()
            self._save_history()
            GLib.idle_add(self._update_context_meter)
            GLib.idle_add(self._insert_sys,
                          f"Switched to {model_name}  (history cleared)")

        except Exception as ex:
            GLib.idle_add(self._insert_sys,
                          f"✗ Failed to pull {model_name}: {ex}")
            # Revert combo box to the previously active model
            for i, m in enumerate(self.MODELS):
                if m == self.model:
                    GLib.idle_add(self.model_combo.set_active, i)
                    break
        finally:
            self.busy = False
            GLib.idle_add(self.send_btn.set_sensitive, True)
            GLib.idle_add(self._set_status, "ready")

    def _on_key_press(self, _widget, event):
        if (event.keyval == Gdk.KEY_Return
                and event.state & Gdk.ModifierType.CONTROL_MASK):
            self._send()
            return True
        return False

    def _stop_generation(self, *_):
        self._cancel_flag = True

    # ── Thinking animation ────────────────────────────────────────────────

    def _start_thinking_animation(self):
        buf = self.chat_buf
        self._thinking_dot_idx = 0
        placeholder = self._DOTS[0] + "\n"
        buf.insert_with_tags_by_name(
            buf.get_end_iter(), placeholder, "thinking_placeholder")
        ph_start = buf.get_end_iter()
        ph_start.backward_chars(len(placeholder))
        self._placeholder_mark = buf.create_mark(
            "thinking_ph", ph_start, left_gravity=True)
        self._thinking_timer_id = GLib.timeout_add(400, self._pulse_thinking)

    def _pulse_thinking(self) -> bool:
        if not self._placeholder_mark:
            return False
        self._thinking_dot_idx = (self._thinking_dot_idx + 1) % len(self._DOTS)
        self._replace_placeholder(self._DOTS[self._thinking_dot_idx] + "\n")
        return True

    def _replace_placeholder(self, new_text: str):
        buf = self.chat_buf
        if not self._placeholder_mark or self._placeholder_mark.get_deleted():
            return
        ph_s = buf.get_iter_at_mark(self._placeholder_mark)
        ph_e = buf.get_end_iter()
        buf.delete(ph_s, ph_e)
        buf.insert_with_tags_by_name(
            buf.get_end_iter(), new_text, "thinking_placeholder")

    def _stop_thinking_animation(self):
        if self._thinking_timer_id is not None:
            GLib.source_remove(self._thinking_timer_id)
            self._thinking_timer_id = None
        if self._placeholder_mark and not self._placeholder_mark.get_deleted():
            buf  = self.chat_buf
            ph_s = buf.get_iter_at_mark(self._placeholder_mark)
            ph_e = buf.get_end_iter()
            buf.delete(ph_s, ph_e)
            buf.delete_mark(self._placeholder_mark)
        self._placeholder_mark = None

    # ── Error recovery ────────────────────────────────────────────────────

    def _restore_prompt_on_fail(self):
        """
        Pop the last user turn off history and put the text back into the
        input box so the user can retry immediately without retyping.
        Called from the worker thread via GLib.idle_add.
        """
        if self.history and self.history[-1]["role"] == "user":
            failed_prompt = self.history.pop()["content"]
            self.input_buf.set_text(failed_prompt)
            self._update_context_meter()

    # ── Send / receive pipeline ───────────────────────────────────────────

    def _send(self, *_):
        if self.busy:
            return
        s, e   = self.input_buf.get_start_iter(), self.input_buf.get_end_iter()
        prompt = self.input_buf.get_text(s, e, False).strip()
        if not prompt:
            return

        self.input_buf.set_text("")
        self.busy         = True
        self._cancel_flag = False
        self.send_btn.set_sensitive(False)
        self.stop_btn.show()
        self._set_status("thinking")

        self.history.append({"role": "user", "content": prompt})
        self._update_context_meter()

        buf = self.chat_buf
        buf.insert_with_tags_by_name(buf.get_end_iter(), "You\n", "user_label")
        buf.insert(buf.get_end_iter(), prompt + "\n\n")
        self._scroll_end()

        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        # ── Sliding-window truncation (even-aligned to preserve pairs) ────
        if len(self.history) > self.max_history:
            overage = len(self.history) - self.max_history
            overage += overage % 2
            self.history = self.history[overage:]

        GLib.idle_add(self._begin_response)

        messages: list[dict] = []

        # Fallback system prompt — prevents CodeLlama and other instruction-
        # tuned models from hallucinating when they see empty <<SYS>> tags.
        sys_prompt = self.system_prompt.strip()
        if not sys_prompt:
            sys_prompt = "You are a helpful and concise AI assistant."
        messages.append({"role": "system", "content": sys_prompt})
        messages.extend(self.history)

        payload = json.dumps({
            "model":    self.model,
            "messages": messages,
            "stream":   True,
            "options":  {
                "temperature":    self.temperature,
                "num_ctx":        self.num_ctx,      # large context window
                "num_predict":    self.num_predict,  # explicit cap; avoids model ignoring -1
                "num_gpu":        self.num_gpu,      # maximise VRAM offloading
                "repeat_penalty": 1.1,               # prevent looping
                "top_k":          40,                # long-form generation stability
                "top_p":          0.9,               # long-form generation stability
            },
        }).encode()

        full_response = ""
        first_token   = True
        request_failed = False  # set True in except; skips polluting history

        # Token batching state — flush every TOKEN_BATCH_MS seconds
        token_buffer   = ""
        last_ui_update = time.time()

        try:
            req = urllib.request.Request(
                self.api_url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                line_buffer = ""
                for raw_chunk in resp:
                    if self._cancel_flag:
                        resp.close()
                        token_buffer  += " [stopped]"
                        full_response += " [stopped]"
                        GLib.idle_add(self._append_token,
                                      token_buffer, first_token)
                        first_token  = False
                        token_buffer = ""
                        break

                    line_buffer += raw_chunk.decode(errors="replace")
                    if not line_buffer.endswith("\n"):
                        continue

                    done_signal = False
                    for line in line_buffer.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        token = obj.get("message", {}).get("content", "")
                        if token:
                            token_buffer  += token
                            full_response += token

                        if obj.get("done"):
                            done_signal = True

                    line_buffer = ""

                    # ── Flush buffer: either 100 ms have passed or we're done
                    now = time.time()
                    if token_buffer and (
                            now - last_ui_update > TOKEN_BATCH_MS
                            or first_token
                            or done_signal):
                        GLib.idle_add(self._append_token,
                                      token_buffer, first_token)
                        token_buffer   = ""
                        last_ui_update = now
                        first_token    = False

                    if done_signal:
                        break

                # Flush any remaining buffered text after the loop ends
                if token_buffer:
                    GLib.idle_add(self._append_token,
                                  token_buffer, first_token)

        except urllib.error.URLError as ex:
            msg = (f"[Connection error: {ex.reason}\n"
                   "Make sure Ollama is running:  ollama serve]")
            full_response = msg
            GLib.idle_add(self._append_token, msg, first_token)
            GLib.idle_add(self._restore_prompt_on_fail)
            request_failed = True
        except Exception as ex:
            msg = f"[Error: {ex}]"
            full_response = msg
            GLib.idle_add(self._append_token, msg, first_token)
            GLib.idle_add(self._restore_prompt_on_fail)
            request_failed = True

        # Only persist a clean assistant reply; never write error messages into
        # history — that would confuse the model on subsequent turns.
        if not request_failed:
            self.history.append({"role": "assistant", "content": full_response})
            self._save_history()           # persist after every completed response
        GLib.idle_add(self._finish_response,
                      "stopped" if self._cancel_flag else "ready")

    # ── GTK-thread callbacks ──────────────────────────────────────────────

    def _begin_response(self):
        buf = self.chat_buf
        buf.insert_with_tags_by_name(
            buf.get_end_iter(), f"{self.model}\n", "model_label")
        self._resp_start = buf.get_end_iter().get_offset()
        self._set_status("streaming")
        self._start_thinking_animation()
        self._scroll_end()

    def _append_token(self, token: str, is_first: bool):
        if is_first:
            self._stop_thinking_animation()
        self.chat_buf.insert(self.chat_buf.get_end_iter(), token)
        self._scroll_if_at_bottom()

    def _finish_response(self, status: str = "ready"):
        self._stop_thinking_animation()

        buf = self.chat_buf
        buf.insert(buf.get_end_iter(), "\n\n")

        if self._resp_start is not None:
            # Pass 1: language header bars
            si       = buf.get_iter_at_offset(self._resp_start)
            raw_text = buf.get_text(si, buf.get_end_iter(), False)
            self.highlighter.insert_code_headers(self._resp_start, raw_text)

            # Pass 2: syntax + markdown highlighting
            si2       = buf.get_iter_at_offset(self._resp_start)
            full_text = buf.get_text(si2, buf.get_end_iter(), False)
            self.highlighter.highlight_message(self._resp_start, full_text)

            self._resp_start = None

        self.busy = False
        self.send_btn.set_sensitive(True)
        self.stop_btn.hide()
        self._set_status(status)
        self._update_context_meter()   # refresh meter after response lands
        self._scroll_if_at_bottom()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    win = OllamaChat()
    win.show_all()
    Gtk.main()
