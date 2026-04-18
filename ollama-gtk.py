#!/usr/bin/env python3
"""
Olla-GTK  (v6 — 110% Powerhouse Edition)

New in this version:
  ┌─ Architecture ─────────────────────────────────────────────────────────────
  │ • Backend switcher: Ollama (Local) | Google Gemini | Anthropic Claude
  │ • Character-based sliding-window memory — never drops system prompt,
  │   always removes oldest user+assistant pair together
  │ • Clean callback-based streamer interface (_stream_ollama / _gemini / _claude)
  ├─ Vision ────────────────────────────────────────────────────────────────────
  │ • 📎 Attach Image button — file chooser filtered to images
  │ • Image sent as base64 inline_data / images array to whichever backend
  │ • Attachment cleared automatically after each send
  ├─ Image Generation tab ─────────────────────────────────────────────────────
  │ • POST to Automatic1111 / ComfyUI-A1111-compat API (localhost:7860)
  │ • Positive + negative prompt, steps, size, CFG scale
  │ • Result displayed as a native GTK pixbuf image widget
  └─ Settings ──────────────────────────────────────────────────────────────────
    • API key fields for Gemini and Claude (saved to ~/.config/ollama-chat/keys.json)
    • Model selectors for all three backends
    • SD server URL configurable per-session
"""

import gi
gi.require_version("Gtk",      "3.0")
gi.require_version("GdkPixbuf","2.0")
from gi.repository import Gtk, Gdk, GLib, Gio, Pango, GdkPixbuf

import base64
import enum
import io
import json
import os
import re
import time
import threading
import socket
import urllib.request
import urllib.error


# ─────────────────────────────────────────────────────────────────────────────
# Defaults & paths
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_API_URL        = "http://localhost:11434/api/chat"
DEFAULT_SYSTEM         = ""
DEFAULT_TEMPERATURE    = 0.7
DEFAULT_MAX_HISTORY    = 100
DEFAULT_NUM_CTX        = 16384
DEFAULT_NUM_PREDICT    = 4096
DEFAULT_NUM_GPU        = 99
DEFAULT_GEMINI_MODEL   = "gemini-2.5-flash"
DEFAULT_CLAUDE_MODEL   = "claude-sonnet-4-6"
DEFAULT_SD_URL         = "http://localhost:7860"

HISTORY_PATH   = os.path.expanduser("~/.config/ollama-chat/history.json")
KEYS_PATH      = os.path.expanduser("~/.config/ollama-chat/keys.json")
REQUEST_TIMEOUT= 300
TOKEN_BATCH_MS = 0.10


# ─────────────────────────────────────────────────────────────────────────────
# Backend enum
# ─────────────────────────────────────────────────────────────────────────────

class Backend(enum.Enum):
    OLLAMA = "Ollama (Local)"
    GEMINI = "Google Gemini"
    CLAUDE = "Anthropic Claude"


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
    """1 token ≈ 4 chars — good enough for a progress bar."""
    return sum(len(m.get("content", "")) for m in history) // 4


def _ensure_config_dir():
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)


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
# Syntax highlighter  (identical to v5)
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
            foreground=DARK_FG, background=DARK_BG_CODE,
            paragraph_background=DARK_BG_CODE,
            left_margin=18, right_margin=18)
        b.create_tag("code_header",
            family="Monospace", size_points=9,
            foreground=DARK_FG_DIM, background=DARK_BG_HEADER,
            paragraph_background=DARK_BG_HEADER,
            left_margin=18, right_margin=18)
        b.create_tag("inline_code",
            family="Monospace", size_points=10,
            foreground="#D7BA7D", background=DARK_BG_INLINE)
        b.create_tag("bold_text",   weight=Pango.Weight.BOLD)
        b.create_tag("bullet_item", left_margin=28)
        b.create_tag("md_link",     foreground="#3794FF",
                     underline=Pango.Underline.SINGLE)
        b.create_tag("thinking_placeholder",
            foreground=DARK_FG_DIM, style=Pango.Style.ITALIC)
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
            le = ls.copy(); le.forward_to_line_end()
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
# Settings popover  (extended with cloud keys + backend model selectors)
# ─────────────────────────────────────────────────────────────────────────────

class SettingsPopover(Gtk.Popover):
    CTX_OPTIONS = [2048, 4096, 8192, 16384, 32768]

    OLLAMA_MODELS = [
        "llama3", "llama3.1", "llama3.2",
        "mistral", "mistral-nemo",
        "qwen2.5", "qwen2.5-coder",
        "codellama", "deepseek-coder",
        "phi3", "phi4", "gemma2", "gemma3",
        # multimodal
        "llava", "llava:13b", "moondream",
    ]
    GEMINI_MODELS = [
        "gemini-2.5-flash",          # current free-tier workhorse (2026)
        "gemini-2.5-pro",            # highest quality, larger quota cost
        "gemini-3.1-flash-preview",  # next-gen preview (may require billing)
    ]
    CLAUDE_MODELS = [
        "claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001",
    ]

    def __init__(self, parent_btn, app: "OllamaChat"):
        super().__init__()
        self.set_relative_to(parent_btn)
        self.app = app

        nb = Gtk.Notebook()
        nb.set_border_width(8)
        self.add(nb)

        nb.append_page(self._build_general_tab(), Gtk.Label(label="General"))
        nb.append_page(self._build_cloud_tab(),   Gtk.Label(label="Cloud APIs"))
        nb.append_page(self._build_advanced_tab(),Gtk.Label(label="Advanced"))

        self.show_all()

    # ── tab builders ─────────────────────────────────────────────────────

    def _build_general_tab(self):
        app = self.app
        grid = Gtk.Grid(row_spacing=8, column_spacing=10, border_width=12)

        def lbl(t):
            l = Gtk.Label(label=t); l.set_halign(Gtk.Align.END); return l

        # API URL (Ollama)
        grid.attach(lbl("Ollama URL"), 0, 0, 1, 1)
        self.url_entry = Gtk.Entry()
        self.url_entry.set_width_chars(34)
        self.url_entry.set_text(app.api_url)
        grid.attach(self.url_entry, 1, 0, 2, 1)

        # Ollama model
        grid.attach(lbl("Ollama Model"), 0, 1, 1, 1)
        self.ollama_model_combo = Gtk.ComboBoxText()
        for m in self.OLLAMA_MODELS:
            self.ollama_model_combo.append_text(m)
        _set_combo(self.ollama_model_combo, self.OLLAMA_MODELS, app.model)
        grid.attach(self.ollama_model_combo, 1, 1, 2, 1)

        # System prompt
        grid.attach(lbl("System\nPrompt"), 0, 2, 1, 1)
        sys_scroll = Gtk.ScrolledWindow()
        sys_scroll.set_min_content_height(70)
        sys_scroll.set_max_content_height(120)
        sys_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sys_view = Gtk.TextView()
        self.sys_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.sys_view.set_left_margin(4); self.sys_view.set_right_margin(4)
        self.sys_buf = self.sys_view.get_buffer()
        self.sys_buf.set_text(app.system_prompt)
        sys_scroll.add(self.sys_view)
        grid.attach(sys_scroll, 1, 2, 2, 1)

        # Temperature
        grid.attach(lbl("Temperature"), 0, 3, 1, 1)
        self.temp_spin = Gtk.SpinButton()
        self.temp_spin.set_adjustment(
            Gtk.Adjustment(value=app.temperature, lower=0.0, upper=2.0,
                           step_increment=0.05, page_increment=0.1))
        self.temp_spin.set_digits(2)
        grid.attach(self.temp_spin, 1, 3, 1, 1)
        grid.attach(Gtk.Label(label="(0 = deterministic, 2 = creative)"), 2, 3, 1, 1)

        apply_btn = Gtk.Button(label="Apply  (clears history)")
        apply_btn.get_style_context().add_class("send-btn")
        apply_btn.connect("clicked", self._apply)
        grid.attach(apply_btn, 2, 4, 1, 1)
        return grid

    def _build_cloud_tab(self):
        app = self.app
        grid = Gtk.Grid(row_spacing=8, column_spacing=10, border_width=12)

        def lbl(t):
            l = Gtk.Label(label=t); l.set_halign(Gtk.Align.END); return l

        # Gemini
        grid.attach(Gtk.Label(label="── Google Gemini ──────────────────────────"),
                    0, 0, 3, 1)
        grid.attach(lbl("API Key"), 0, 1, 1, 1)
        self.gemini_key_entry = Gtk.Entry()
        self.gemini_key_entry.set_visibility(False)
        self.gemini_key_entry.set_width_chars(36)
        self.gemini_key_entry.set_text(app.gemini_key)
        self.gemini_key_entry.set_placeholder_text("AIza…")
        grid.attach(self.gemini_key_entry, 1, 1, 2, 1)

        grid.attach(lbl("Model"), 0, 2, 1, 1)
        self.gemini_model_combo = Gtk.ComboBoxText()
        for m in self.GEMINI_MODELS:
            self.gemini_model_combo.append_text(m)
        _set_combo(self.gemini_model_combo, self.GEMINI_MODELS, app.gemini_model)
        grid.attach(self.gemini_model_combo, 1, 2, 2, 1)

        # Claude
        grid.attach(Gtk.Label(label="── Anthropic Claude ──────────────────────"),
                    0, 3, 3, 1)
        grid.attach(lbl("API Key"), 0, 4, 1, 1)
        self.claude_key_entry = Gtk.Entry()
        self.claude_key_entry.set_visibility(False)
        self.claude_key_entry.set_width_chars(36)
        self.claude_key_entry.set_text(app.claude_key)
        self.claude_key_entry.set_placeholder_text("sk-ant-…")
        grid.attach(self.claude_key_entry, 1, 4, 2, 1)

        grid.attach(lbl("Model"), 0, 5, 1, 1)
        self.claude_model_combo = Gtk.ComboBoxText()
        for m in self.CLAUDE_MODELS:
            self.claude_model_combo.append_text(m)
        _set_combo(self.claude_model_combo, self.CLAUDE_MODELS, app.claude_model)
        grid.attach(self.claude_model_combo, 1, 5, 2, 1)

        note = Gtk.Label()
        note.set_markup(
            "<span foreground='#858585' size='small'>"
            "Keys are stored locally in ~/.config/ollama-chat/keys.json</span>")
        note.set_halign(Gtk.Align.START)
        grid.attach(note, 0, 6, 3, 1)

        apply_btn = Gtk.Button(label="Save Keys & Apply")
        apply_btn.get_style_context().add_class("send-btn")
        apply_btn.connect("clicked", self._apply)
        grid.attach(apply_btn, 2, 7, 1, 1)
        return grid

    def _build_advanced_tab(self):
        app = self.app
        grid = Gtk.Grid(row_spacing=8, column_spacing=10, border_width=12)

        def lbl(t):
            l = Gtk.Label(label=t); l.set_halign(Gtk.Align.END); return l

        # Context size
        grid.attach(lbl("Context Size\n(num_ctx)"), 0, 0, 1, 1)
        self.ctx_combo = Gtk.ComboBoxText()
        current_idx = 0
        for i, v in enumerate(self.CTX_OPTIONS):
            self.ctx_combo.append_text(f"{v:,} tokens")
            if v == app.num_ctx:
                current_idx = i
        self.ctx_combo.set_active(current_idx)
        grid.attach(self.ctx_combo, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="↑ higher = more VRAM used"), 2, 0, 1, 1)

        # num_predict
        grid.attach(lbl("Max Output\n(num_predict)"), 0, 1, 1, 1)
        self.predict_spin = Gtk.SpinButton()
        self.predict_spin.set_adjustment(
            Gtk.Adjustment(value=app.num_predict, lower=256, upper=32768,
                           step_increment=256, page_increment=1024))
        self.predict_spin.set_digits(0)
        grid.attach(self.predict_spin, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="tokens per response  (4096 ≈ 3k words)"), 2, 1, 1, 1)

        # Max history
        grid.attach(lbl("Max History\nMessages"), 0, 2, 1, 1)
        self.hist_spin = Gtk.SpinButton()
        self.hist_spin.set_adjustment(
            Gtk.Adjustment(value=app.max_history, lower=2, upper=500,
                           step_increment=2, page_increment=10))
        self.hist_spin.set_digits(0)
        grid.attach(self.hist_spin, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="character-budget trimming is the real cap"), 2, 2, 1, 1)

        # SD URL
        grid.attach(lbl("SD API URL"), 0, 3, 1, 1)
        self.sd_url_entry = Gtk.Entry()
        self.sd_url_entry.set_width_chars(28)
        self.sd_url_entry.set_text(app.sd_url)
        grid.attach(self.sd_url_entry, 1, 3, 2, 1)

        apply_btn = Gtk.Button(label="Apply  (clears history)")
        apply_btn.get_style_context().add_class("send-btn")
        apply_btn.connect("clicked", self._apply)
        grid.attach(apply_btn, 2, 4, 1, 1)
        return grid

    # ── apply ─────────────────────────────────────────────────────────────

    def _apply(self, *_):
        app = self.app
        # General
        app.api_url       = self.url_entry.get_text().strip()
        s, e = self.sys_buf.get_start_iter(), self.sys_buf.get_end_iter()
        app.system_prompt = self.sys_buf.get_text(s, e, False).strip()
        app.temperature   = self.temp_spin.get_value()
        new_model = self.ollama_model_combo.get_active_text()
        if new_model:
            app.model = new_model

        # Cloud
        app.gemini_key   = self.gemini_key_entry.get_text().strip()
        app.claude_key   = self.claude_key_entry.get_text().strip()
        gm = self.gemini_model_combo.get_active_text()
        cm = self.claude_model_combo.get_active_text()
        if gm: app.gemini_model = gm
        if cm: app.claude_model = cm
        app._save_keys()

        # Advanced
        app.num_ctx     = self.CTX_OPTIONS[self.ctx_combo.get_active()]
        app.num_predict = int(self.predict_spin.get_value())
        app.max_history = int(self.hist_spin.get_value())
        app.sd_url      = self.sd_url_entry.get_text().strip()

        app.history.clear()
        app._update_context_meter()
        app._insert_sys("Settings applied — history cleared")
        self.popdown()


def _set_combo(combo: Gtk.ComboBoxText, items: list, value: str):
    """Set a ComboBoxText to the given value, or 0 if not found."""
    for i, m in enumerate(items):
        if m == value:
            combo.set_active(i)
            return
    combo.set_active(0)


# ─────────────────────────────────────────────────────────────────────────────
# Image Generation Panel  (Stable Diffusion / A1111 compatible)
# ─────────────────────────────────────────────────────────────────────────────

class ImageGenPanel(Gtk.Box):
    """
    Self-contained panel that lives in the Image Gen notebook tab.

    Supports two backends selectable via a radio-button row:
      • Stable Diffusion (localhost A1111-compatible API, default)
      • Google Gemini  (gemini-2.5-flash-image or gemini-3.1-flash-image-preview)

    The Gemini backend uses the same API key stored in app.gemini_key and
    the v1beta/generateContent endpoint with response_modalities=["IMAGE"].
    SD-specific controls (steps, CFG, negative prompt) are shown/hidden
    automatically based on which backend is active.
    """

    SIZES      = ["512×512", "768×512", "512×768", "768×768", "1024×1024"]
    GEMINI_IMG = ["gemini-2.5-flash-image", "gemini-3.1-flash-image-preview"]

    def __init__(self, app: "OllamaChat"):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.app             = app
        self._use_gemini     = False   # False = SD, True = Gemini
        self._current_pixbuf: GdkPixbuf.Pixbuf | None = None
        self._build()

    def _build(self):
        # ── Left controls ────────────────────────────────────────────────
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_border_width(10)
        left.set_size_request(360, -1)
        self.pack_start(left, False, False, 0)

        # ── Backend selector ─────────────────────────────────────────────
        backend_row = Gtk.Box(spacing=10)
        left.pack_start(backend_row, False, False, 0)

        self._rb_sd = Gtk.RadioButton.new_with_label(None, "🖥  Stable Diffusion")
        self._rb_sd.set_active(True)
        backend_row.pack_start(self._rb_sd, False, False, 0)

        self._rb_gemini = Gtk.RadioButton.new_with_label_from_widget(
            self._rb_sd, "✨  Gemini Image")
        backend_row.pack_start(self._rb_gemini, False, False, 0)

        self._rb_sd.connect("toggled", self._on_backend_toggled)
        self._rb_gemini.connect("toggled", self._on_backend_toggled)

        # ── Gemini model selector (hidden by default) ─────────────────────
        self._gemini_row = Gtk.Box(spacing=6)
        self._gemini_row.set_no_show_all(True)
        self._gemini_model_combo = Gtk.ComboBoxText()
        for m in self.GEMINI_IMG:
            self._gemini_model_combo.append_text(m)
        self._gemini_model_combo.set_active(0)
        self._gemini_row.pack_start(
            Gtk.Label(label="Model:", xalign=1), False, False, 0)
        self._gemini_row.pack_start(self._gemini_model_combo, True, True, 0)
        left.pack_start(self._gemini_row, False, False, 0)

        # ── Prompt ───────────────────────────────────────────────────────
        left.pack_start(Gtk.Label(label="Prompt", xalign=0), False, False, 0)
        pos_scroll = Gtk.ScrolledWindow()
        pos_scroll.set_min_content_height(80)
        pos_scroll.set_max_content_height(160)
        pos_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.pos_view = Gtk.TextView()
        self.pos_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.pos_view.set_left_margin(6); self.pos_view.set_right_margin(6)
        self.pos_buf = self.pos_view.get_buffer()
        pos_scroll.add(self.pos_view)
        left.pack_start(pos_scroll, False, False, 0)

        # ── Negative prompt (SD-only, hidden for Gemini) ─────────────────
        self._neg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._neg_box.pack_start(
            Gtk.Label(label="Negative prompt", xalign=0), False, False, 0)
        neg_scroll = Gtk.ScrolledWindow()
        neg_scroll.set_min_content_height(50)
        neg_scroll.set_max_content_height(100)
        neg_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.neg_view = Gtk.TextView()
        self.neg_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.neg_view.set_left_margin(6); self.neg_view.set_right_margin(6)
        self.neg_buf = self.neg_view.get_buffer()
        self.neg_buf.set_text(
            "blurry, low quality, ugly, deformed, watermark, text, signature")
        neg_scroll.add(self.neg_view)
        self._neg_box.pack_start(neg_scroll, False, False, 0)
        left.pack_start(self._neg_box, False, False, 0)

        # ── SD settings grid (hidden for Gemini) ─────────────────────────
        self._sd_grid = Gtk.Grid(row_spacing=6, column_spacing=8)
        left.pack_start(self._sd_grid, False, False, 0)

        self._sd_grid.attach(Gtk.Label(label="Size", xalign=1), 0, 0, 1, 1)
        self.size_combo = Gtk.ComboBoxText()
        for s in self.SIZES:
            self.size_combo.append_text(s)
        self.size_combo.set_active(0)
        self._sd_grid.attach(self.size_combo, 1, 0, 1, 1)

        self._sd_grid.attach(Gtk.Label(label="Steps", xalign=1), 0, 1, 1, 1)
        self.steps_spin = Gtk.SpinButton()
        self.steps_spin.set_adjustment(
            Gtk.Adjustment(value=20, lower=1, upper=150,
                           step_increment=1, page_increment=5))
        self.steps_spin.set_digits(0)
        self._sd_grid.attach(self.steps_spin, 1, 1, 1, 1)

        self._sd_grid.attach(Gtk.Label(label="CFG scale", xalign=1), 0, 2, 1, 1)
        self.cfg_spin = Gtk.SpinButton()
        self.cfg_spin.set_adjustment(
            Gtk.Adjustment(value=7.0, lower=1.0, upper=30.0,
                           step_increment=0.5, page_increment=1.0))
        self.cfg_spin.set_digits(1)
        self._sd_grid.attach(self.cfg_spin, 1, 2, 1, 1)

        # ── Status + buttons ─────────────────────────────────────────────
        self.sd_status = Gtk.Label(label="")
        self.sd_status.set_halign(Gtk.Align.START)
        left.pack_start(self.sd_status, False, False, 0)

        self.gen_btn = Gtk.Button(label="🎨  Generate Image")
        self.gen_btn.get_style_context().add_class("send-btn")
        self.gen_btn.connect("clicked", self._generate)
        left.pack_start(self.gen_btn, False, False, 0)

        save_btn = Gtk.Button(label="Save Image…")
        save_btn.get_style_context().add_class("clear-btn")
        save_btn.connect("clicked", self._save_image)
        left.pack_start(save_btn, False, False, 0)

        # ── Right: image display ─────────────────────────────────────────
        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.pack_start(right_scroll, True, True, 0)

        self._img_widget = Gtk.Image()
        self._img_widget.set_halign(Gtk.Align.CENTER)
        self._img_widget.set_valign(Gtk.Align.CENTER)
        placeholder = Gtk.Label()
        placeholder.set_markup(
            f"<span foreground='{DARK_FG_DIM}'>Generated image will appear here</span>")
        self._img_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._img_box.set_halign(Gtk.Align.CENTER)
        self._img_box.set_valign(Gtk.Align.CENTER)
        self._img_box.pack_start(placeholder, True, True, 0)
        self._img_box.pack_start(self._img_widget, True, True, 0)
        right_scroll.add(self._img_box)

    # ── Backend toggle ────────────────────────────────────────────────────

    def _on_backend_toggled(self, btn):
        if not btn.get_active():
            return
        self._use_gemini = self._rb_gemini.get_active()
        # Show/hide SD-specific controls
        if self._use_gemini:
            self._neg_box.hide()
            self._sd_grid.hide()
            self._gemini_row.show()
            self.sd_status.set_text(
                "Gemini image — uses your Gemini API key from Settings")
        else:
            self._neg_box.show()
            self._sd_grid.show()
            self._gemini_row.hide()
            self.sd_status.set_text("")

    # ── Generation dispatcher ─────────────────────────────────────────────

    def _generate(self, *_):
        s, e   = self.pos_buf.get_start_iter(), self.pos_buf.get_end_iter()
        prompt = self.pos_buf.get_text(s, e, False).strip()
        if not prompt:
            self.sd_status.set_text("Enter a prompt first.")
            return

        self.gen_btn.set_sensitive(False)

        if self._use_gemini:
            self.sd_status.set_text("Sending to Gemini…")
            threading.Thread(
                target=self._gen_gemini_worker, args=(prompt,),
                daemon=True).start()
        else:
            s, e = self.neg_buf.get_start_iter(), self.neg_buf.get_end_iter()
            neg  = self.neg_buf.get_text(s, e, False).strip()
            size = self.size_combo.get_active_text() or "512×512"
            w, h = (int(x) for x in size.replace("×", "x").split("x"))
            self.sd_status.set_text("Generating…")
            threading.Thread(
                target=self._gen_sd_worker,
                args=(prompt, neg, w, h,
                      int(self.steps_spin.get_value()),
                      self.cfg_spin.get_value()),
                daemon=True).start()

    # ── SD worker ─────────────────────────────────────────────────────────

    def _gen_sd_worker(self, prompt, neg, w, h, steps, cfg):
        try:
            payload = json.dumps({
                "prompt":          prompt,
                "negative_prompt": neg,
                "width":  w, "height": h,
                "steps":  steps, "cfg_scale": cfg,
                "sampler_name": "Euler a",
            }).encode()
            url = self.app.sd_url.rstrip("/") + "/sdapi/v1/txt2img"
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=300) as resp:
                data   = json.loads(resp.read().decode())
                images = data.get("images", [])
                if not images:
                    raise ValueError("No images returned from SD API")
                GLib.idle_add(self._show_image, base64.b64decode(images[0]))
        except Exception as ex:
            GLib.idle_add(self.sd_status.set_text,
                          f"SD error: {ex}  (Is Automatic1111 running?)")
        finally:
            GLib.idle_add(self.gen_btn.set_sensitive, True)

    # ── Gemini image worker ───────────────────────────────────────────────

    def _gen_gemini_worker(self, prompt: str):
        """
        Call the Gemini generateContent endpoint with response_modalities=["IMAGE"].
        The model returns inline_data (base64 PNG) in the first candidate's parts.
        Uses v1beta so both stable and preview image model IDs are accepted.

        429 handling: parses Google's retryDelay field and auto-retries once
        after the specified cool-down so transient quota bursts self-heal.
        """
        if not self.app.gemini_key:
            GLib.idle_add(self.sd_status.set_text,
                          "No Gemini API key — open ⚙ Settings → Cloud APIs")
            GLib.idle_add(self.gen_btn.set_sensitive, True)
            return

        model = self._gemini_model_combo.get_active_text() or "gemini-2.5-flash-image"
        clean = model.split("/")[-1]
        url   = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                 f"{clean}:generateContent?key={self.app.gemini_key}")

        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }).encode()

        max_attempts = 2   # one automatic retry after a 429 cool-down
        for attempt in range(1, max_attempts + 1):
            try:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                try:
                    resp_obj = urllib.request.urlopen(req, timeout=120)
                except urllib.error.HTTPError as http_err:
                    body = http_err.read().decode(errors="replace")
                    try:
                        err_json   = json.loads(body)
                        err_obj    = (err_json[0]["error"]
                                      if isinstance(err_json, list)
                                      else err_json.get("error", {}))
                        google_msg = err_obj.get("message", body)

                        # ── "Limit 0" quota bug diagnosis ─────────────────
                        # Google aliases some model IDs to internal preview
                        # variants that have a hardcoded free-tier quota of 0.
                        # Surface a human-readable hint rather than a raw 429.
                        if http_err.code == 429:
                            # Parse the retryDelay e.g. "14.48s" → 15 seconds
                            retry_info = (err_obj.get("details") or [{}])
                            retry_secs = 0
                            for detail in retry_info:
                                delay_str = detail.get("retryDelay", "")
                                if delay_str:
                                    try:
                                        retry_secs = int(
                                            float(delay_str.rstrip("s")) + 1)
                                    except ValueError:
                                        retry_secs = 15

                            if "limit: 0" in google_msg or "RESOURCE_EXHAUSTED" in google_msg:
                                # Permanent "Limit 0" — retrying won't help
                                hint = (
                                    f"Quota limit 0 on {clean} — try switching "
                                    "to gemini-3.1-flash-image-preview, or enable "
                                    "Image Generation in AI Studio key settings")
                                print(f"[Gemini 429 Limit-0] {google_msg}", flush=True)
                                GLib.idle_add(self.sd_status.set_text, hint)
                                return  # no point retrying a hard zero quota

                            if attempt < max_attempts and retry_secs > 0:
                                # Transient rate-limit — wait and retry once
                                print(f"[Gemini 429] retrying in {retry_secs}s…",
                                      flush=True)
                                for remaining in range(retry_secs, 0, -1):
                                    GLib.idle_add(
                                        self.sd_status.set_text,
                                        f"Rate limited — retrying in {remaining}s…")
                                    time.sleep(1)
                                continue   # jump to next attempt

                    except Exception:
                        google_msg = http_err.reason

                    print(f"[Gemini Image HTTPError {http_err.code}] {google_msg}",
                          flush=True)
                    GLib.idle_add(self.sd_status.set_text,
                                  f"Gemini {http_err.code}: {google_msg}")
                    return

                with resp_obj as resp:
                    data = json.loads(resp.read().decode())

                # Walk parts looking for inline_data (the image bytes)
                img_bytes = None
                for part in (data.get("candidates", [{}])[0]
                                 .get("content", {})
                                 .get("parts", [])):
                    if "inlineData" in part:
                        img_bytes = base64.b64decode(part["inlineData"]["data"])
                        break
                    if "inline_data" in part:
                        img_bytes = base64.b64decode(part["inline_data"]["data"])
                        break

                if img_bytes:
                    GLib.idle_add(self._show_image, img_bytes)
                else:
                    text_parts = [p.get("text", "") for p in
                                  (data.get("candidates", [{}])[0]
                                       .get("content", {})
                                       .get("parts", []))]
                    GLib.idle_add(self.sd_status.set_text,
                                  "No image in response: "
                                  + " ".join(text_parts)[:120])
                return   # success — exit the retry loop

            except Exception as ex:
                GLib.idle_add(self.sd_status.set_text, f"Error: {ex}")
                return

        GLib.idle_add(self.sd_status.set_text,
                      "Still rate-limited after retry — wait a minute and try again")
        GLib.idle_add(self.gen_btn.set_sensitive, True)

    def _show_image(self, img_bytes: bytes):
        """
        Display decoded image bytes in the Gtk.Image widget.
        Uses Gio.MemoryInputStream + Pixbuf.new_from_stream — the GTK-native
        path that avoids the write/close dance of PixbufLoader.
        Called on the GTK main thread via GLib.idle_add.
        """
        try:
            stream  = Gio.MemoryInputStream.new_from_data(img_bytes, None)
            pixbuf  = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
            self._current_pixbuf = pixbuf
            self._img_widget.set_from_pixbuf(pixbuf)
            self.sd_status.set_text(
                f"Done — {pixbuf.get_width()}×{pixbuf.get_height()} px")
        except Exception as ex:
            self.sd_status.set_text(f"Display error: {ex}")

    def _save_image(self, *_):
        if not self._current_pixbuf:
            self.sd_status.set_text("Nothing to save yet.")
            return
        dialog = Gtk.FileChooserDialog(
            title="Save Image",
            parent=self.app,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE,   Gtk.ResponseType.OK)
        dialog.set_current_name("generated.png")
        ff = Gtk.FileFilter(); ff.set_name("PNG images"); ff.add_pattern("*.png")
        dialog.add_filter(ff)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            if not path.endswith(".png"):
                path += ".png"
            self._current_pixbuf.savev(path, "png", [], [])
            self.sd_status.set_text(f"Saved to {path}")
        dialog.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# Main application window
# ─────────────────────────────────────────────────────────────────────────────

class OllamaChat(Gtk.Window):

    OLLAMA_MODELS = [
        "llama3", "llama3.1", "llama3.2",
        "mistral", "mistral-nemo",
        "qwen2.5", "qwen2.5-coder",
        "codellama", "deepseek-coder",
        "phi3", "phi4", "gemma2", "gemma3",
        "llava", "llava:13b", "moondream",
    ]

    _DOTS = ["●∙∙", "∙●∙", "∙∙●"]

    def __init__(self):
        super().__init__(title="Olla-GTK")
        self.set_default_size(1020, 780)

        # ── Runtime settings ─────────────────────────────────────────────
        self.api_url       = DEFAULT_API_URL
        self.system_prompt = DEFAULT_SYSTEM
        self.temperature   = DEFAULT_TEMPERATURE
        self.max_history   = DEFAULT_MAX_HISTORY
        self.num_ctx       = DEFAULT_NUM_CTX
        self.num_predict   = DEFAULT_NUM_PREDICT
        self.num_gpu       = DEFAULT_NUM_GPU
        self.sd_url        = DEFAULT_SD_URL

        # Cloud backends
        self.backend      = Backend.OLLAMA
        self.model        = "llama3"
        self.gemini_model = DEFAULT_GEMINI_MODEL
        self.claude_model = DEFAULT_CLAUDE_MODEL
        self.gemini_key   = ""
        self.claude_key   = ""

        # State
        self.busy         = False
        self._cancel_flag = False
        self._resp_start: int | None = None
        self._placeholder_mark  = None
        self._thinking_timer_id = None
        self._thinking_dot_idx  = 0
        self.history: list[dict] = []

        # Image attachment (vision)
        self.attached_image_b64:  str | None = None
        self.attached_image_name: str        = ""

        # Auto-Editor / Novel Mode
        self.novel_mode: bool = False

        self._build_ui()
        self.highlighter = SyntaxHighlighter(self.chat_buf)
        self._apply_css()
        self._set_status("ready")
        self._register_shortcuts()
        self._load_keys()
        self.connect("destroy", self._on_quit)
        GLib.idle_add(self._maybe_restore_session)
        GLib.idle_add(self.input_view.grab_focus)

    # ── Persistence ───────────────────────────────────────────────────────

    def _save_history(self):
        try:
            _ensure_config_dir()
            with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_history(self) -> list[dict]:
        if not os.path.exists(HISTORY_PATH):
            return []
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_keys(self):
        try:
            _ensure_config_dir()
            with open(KEYS_PATH, "w", encoding="utf-8") as f:
                json.dump({"gemini": self.gemini_key,
                           "claude": self.claude_key}, f)
            os.chmod(KEYS_PATH, 0o600)   # owner read/write only — no group/world access
        except OSError:
            pass

    def _load_keys(self):
        if not os.path.exists(KEYS_PATH):
            return
        try:
            with open(KEYS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.gemini_key = data.get("gemini", "")
            self.claude_key = data.get("claude", "")
        except (OSError, json.JSONDecodeError):
            pass

    def _maybe_restore_session(self):
        saved = self._load_history()
        if not saved:
            return False
        n_turns = sum(1 for m in saved if m.get("role") == "user")
        dialog  = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Restore previous session?")
        dialog.format_secondary_text(
            f"{n_turns} turn(s) found  (≈{estimate_tokens(saved):,} tokens).  "
            "Restore into context?")
        dialog.add_buttons("Start Fresh", Gtk.ResponseType.NO,
                           "Restore Session", Gtk.ResponseType.YES)
        dialog.set_default_response(Gtk.ResponseType.YES)
        resp = dialog.run(); dialog.destroy()
        if resp == Gtk.ResponseType.YES:
            self.history = saved
            self._insert_sys(f"Session restored — {n_turns} turn(s)")
            self._update_context_meter()
        return False

    def _on_quit(self, *_):
        # Signal any running worker/streamer to abort so daemon threads
        # close their urllib connections before the interpreter exits.
        self._cancel_flag = True
        self._save_history()
        Gtk.main_quit()

    # ── Memory: character-budget sliding window ───────────────────────────

    def _trim_history_to_budget(self):
        """
        Remove the oldest user+assistant pairs until the total character count
        (system prompt + history) fits within 85% of num_ctx × 4 chars.
        The system prompt index is never stored in self.history, so it is
        inherently protected from trimming.
        """
        budget = int(self.num_ctx * 4 * 0.85)
        sys_chars = len(self.system_prompt or "You are a helpful AI assistant.")

        while True:
            hist_chars = sum(len(m.get("content", "")) for m in self.history)
            if sys_chars + hist_chars <= budget or len(self.history) < 2:
                break
            # Always remove a complete pair aligned to user role at index 0
            if self.history[0]["role"] == "user":
                self.history = self.history[2:]
            else:
                # Orphaned assistant at head — remove single entry to re-align
                self.history = self.history[1:]

    # ── Context meter ─────────────────────────────────────────────────────

    def _update_context_meter(self):
        used  = estimate_tokens(self.history)
        cap   = self.num_ctx
        frac  = min(used / cap, 1.0) if cap > 0 else 0.0
        self._ctx_bar.set_fraction(frac)
        pct   = int(frac * 100)
        self._ctx_label.set_markup(
            f"<span foreground='{DARK_FG_DIM}' size='small'>"
            f"Context: {used:,} / {cap:,} tokens  ({pct}%)</span>")
        colour = ("#4EC9B0" if frac < 0.6
                  else "#DCDCAA" if frac < 0.85
                  else "#F44747")
        css = (f"progressbar trough {{ background-color: #333333; border-radius:3px; }}"
               f"progressbar progress {{ background-color: {colour}; border-radius:3px; }}")
        p = Gtk.CssProvider()
        p.load_from_data(css.encode())
        self._ctx_bar.get_style_context().add_provider(
            p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── CSS ───────────────────────────────────────────────────────────────

    def _apply_css(self):
        css = f"""
        window, .main-box {{ background-color: {DARK_BG_PANEL}; }}
        .chat-view {{
            font-family: 'Segoe UI','Ubuntu','Cantarell','Noto Sans',sans-serif;
            font-size: 11pt; background-color: {DARK_BG};
            color: {DARK_FG}; caret-color: {DARK_FG};
        }}
        .input-view {{
            font-family: 'Segoe UI','Ubuntu','Cantarell','Noto Sans',sans-serif;
            font-size: 11pt; background-color: {DARK_BG_INPUT};
            color: {DARK_FG}; caret-color: {DARK_FG};
        }}
        textview text {{ background-color: transparent; color: {DARK_FG}; }}
        .chat-view  text {{ background-color: {DARK_BG}; }}
        .input-view text {{ background-color: {DARK_BG_INPUT}; }}
        scrolledwindow, viewport {{ background-color: {DARK_BG}; }}
        notebook {{ background-color: {DARK_BG_PANEL}; }}
        notebook tab {{ background-color: {DARK_BG_PANEL}; padding: 4px 10px; }}
        notebook tab:checked {{ background-color: {DARK_BG}; }}
        frame > border {{ border: 1px solid #444444; border-radius: 4px; }}
        .toolbar-box, .meter-box {{ background-color: {DARK_BG_PANEL}; }}
        .meter-box {{ padding: 2px 8px 4px 8px; }}
        label {{ color: {DARK_FG}; }}
        .hint-label {{ color: {DARK_FG_DIM}; }}
        .attach-label {{ color: #4EC9B0; font-size: 9pt; }}
        combobox button {{
            background-color: #3C3C3C; color: {DARK_FG};
            border: 1px solid #555555; border-radius: 4px;
        }}
        .send-btn {{
            background: #0E639C; color: #FFFFFF;
            border: none; border-radius: 5px; padding: 5px 16px; font-weight: bold;
        }}
        .send-btn:hover   {{ background: #1177BB; }}
        .send-btn:active  {{ background: #0A4D7A; }}
        .send-btn:disabled {{ background: #3C3C3C; color: #6A6A6A; }}
        .stop-btn {{
            background: #8B1A1A; color: #FFFFFF;
            border: none; border-radius: 5px; padding: 5px 16px; font-weight: bold;
        }}
        .stop-btn:hover  {{ background: #A52020; }}
        .stop-btn:active {{ background: #6A1010; }}
        .icon-btn {{
            background: transparent; color: {DARK_FG};
            border: 1px solid #555555; border-radius: 5px; padding: 4px 8px;
        }}
        .icon-btn:hover {{ background: #3C3C3C; }}
        .clear-btn {{
            background: #3C3C3C; color: {DARK_FG};
            border: 1px solid #555555; border-radius: 5px; padding: 5px 10px;
        }}
        .clear-btn:hover {{ background: #4C4C4C; }}
        separator {{ background-color: #333333; }}
        popover {{ background-color: {DARK_BG_PANEL}; }}
        popover entry, popover textview {{
            background-color: {DARK_BG_INPUT}; color: {DARK_FG};
        }}
        progressbar trough  {{ background-color:#333333; border-radius:3px; min-height:5px; }}
        progressbar progress {{ background-color:#4EC9B0; border-radius:3px; min-height:5px; }}
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

        # ── Toolbar ───────────────────────────────────────────────────────
        toolbar = Gtk.Box(spacing=8)
        toolbar.set_border_width(8)
        toolbar.get_style_context().add_class("toolbar-box")
        root.pack_start(toolbar, False, False, 0)

        # Backend switcher
        toolbar.pack_start(Gtk.Label(label="Backend:"), False, False, 0)
        self.backend_combo = Gtk.ComboBoxText()
        for b in Backend:
            self.backend_combo.append_text(b.value)
        self.backend_combo.set_active(0)
        self.backend_combo.connect("changed", self._on_backend_changed)
        toolbar.pack_start(self.backend_combo, False, False, 0)

        # Model selector (Ollama)
        toolbar.pack_start(Gtk.Label(label="Model:"), False, False, 0)
        self.model_combo = Gtk.ComboBoxText()
        for m in self.OLLAMA_MODELS:
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

        # ── Context meter ─────────────────────────────────────────────────
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

        # ── Notebook: Chat | Image Gen ─────────────────────────────────────
        self._notebook = Gtk.Notebook()
        root.pack_start(self._notebook, True, True, 0)

        self._notebook.append_page(self._build_chat_page(),
                                   Gtk.Label(label="💬  Chat"))
        self._img_gen_panel = ImageGenPanel(self)
        self._notebook.append_page(self._img_gen_panel,
                                   Gtk.Label(label="🎨  Image Gen"))

        self._update_context_meter()

    def _build_chat_page(self) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Chat display
        self.chat_view = Gtk.TextView()
        self.chat_view.set_editable(False)
        self.chat_view.set_cursor_visible(False)
        self.chat_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.chat_view.set_left_margin(14); self.chat_view.set_right_margin(14)
        self.chat_view.set_top_margin(10);  self.chat_view.set_bottom_margin(10)
        self.chat_view.get_style_context().add_class("chat-view")
        self.chat_buf = self.chat_view.get_buffer()
        self.chat_view.connect("button-press-event", self._on_chat_click)

        self.chat_scroll = Gtk.ScrolledWindow()
        self.chat_scroll.set_vexpand(True)
        self.chat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.chat_scroll.add(self.chat_view)
        page.pack_start(self.chat_scroll, True, True, 0)

        page.pack_start(Gtk.Separator(), False, False, 0)

        # ── Input area ────────────────────────────────────────────────────
        input_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        input_outer.set_border_width(8)
        input_outer.get_style_context().add_class("toolbar-box")
        page.pack_start(input_outer, False, False, 0)

        # Attachment banner (hidden by default)
        self._attach_banner = Gtk.Box(spacing=6)
        self._attach_lbl    = Gtk.Label(label="")
        self._attach_lbl.get_style_context().add_class("attach-label")
        self._attach_banner.pack_start(self._attach_lbl, False, False, 0)
        clr = Gtk.Button(label="✕  Remove")
        clr.get_style_context().add_class("icon-btn")
        clr.connect("clicked", self._clear_attachment)
        self._attach_banner.pack_start(clr, False, False, 0)
        self._attach_banner.set_no_show_all(True)
        input_outer.pack_start(self._attach_banner, False, False, 0)

        # Text input frame
        frame = Gtk.Frame()
        input_outer.pack_start(frame, False, False, 0)
        inp_scroll = Gtk.ScrolledWindow()
        inp_scroll.set_min_content_height(70)
        inp_scroll.set_max_content_height(220)
        inp_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame.add(inp_scroll)

        self.input_view = Gtk.TextView()
        self.input_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.input_view.set_left_margin(8); self.input_view.set_right_margin(8)
        self.input_view.set_top_margin(6);  self.input_view.set_bottom_margin(6)
        self.input_view.get_style_context().add_class("input-view")
        self.input_buf = self.input_view.get_buffer()
        self.input_view.connect("key-press-event", self._on_key_press)
        inp_scroll.add(self.input_view)

        # Button row
        btn_row = Gtk.Box(spacing=8)
        input_outer.pack_start(btn_row, False, False, 0)

        hint = Gtk.Label(
            label="Enter = Send  ·  Shift+Enter = newline  ·  Ctrl+L = Clear  ·  Ctrl+, = Settings")
        hint.set_halign(Gtk.Align.START)
        hint.get_style_context().add_class("hint-label")
        btn_row.pack_start(hint, True, True, 0)

        # Novel / Auto-Editor mode toggle
        self.novel_btn = Gtk.ToggleButton(label="✨ Editor")
        self.novel_btn.get_style_context().add_class("icon-btn")
        self.novel_btn.set_tooltip_text(
            "Auto-Editor mode: rewrites your text instead of chatting")
        self.novel_btn.connect("toggled", self._on_novel_toggled)
        btn_row.pack_end(self.novel_btn, False, False, 0)

        attach_btn = Gtk.Button(label="📎")
        attach_btn.get_style_context().add_class("icon-btn")
        attach_btn.set_tooltip_text("Attach image  (vision models)")
        attach_btn.connect("clicked", self._attach_image)
        btn_row.pack_end(attach_btn, False, False, 0)

        self.stop_btn = Gtk.Button(label="■ Stop")
        self.stop_btn.get_style_context().add_class("stop-btn")
        self.stop_btn.connect("clicked", self._stop_generation)
        self.stop_btn.set_no_show_all(True)
        btn_row.pack_end(self.stop_btn, False, False, 0)

        self.send_btn = Gtk.Button(label="Send  (Ctrl+↵)")
        self.send_btn.get_style_context().add_class("send-btn")
        self.send_btn.connect("clicked", self._send)
        btn_row.pack_end(self.send_btn, False, False, 0)

        return page

    # ── Global shortcuts ──────────────────────────────────────────────────

    def _register_shortcuts(self):
        self.connect("key-press-event", self._on_window_key)

    def _on_window_key(self, _win, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if not ctrl:
            return False
        if event.keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self._clear(); return True
        if event.keyval == Gdk.KEY_comma:
            self._open_settings(self._gear_btn); return True
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

    # ── Image attachment ──────────────────────────────────────────────────

    def _attach_image(self, *_):
        dialog = Gtk.FileChooserDialog(
            title="Attach Image", parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           "Attach",         Gtk.ResponseType.OK)
        ff = Gtk.FileFilter()
        ff.set_name("Images")
        for pat in ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.bmp"):
            ff.add_pattern(pat)
        dialog.add_filter(ff)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                self.attached_image_b64  = base64.b64encode(raw).decode()
                self.attached_image_name = os.path.basename(path)
                self._attach_lbl.set_text(f"📷  {self.attached_image_name}")
                self._attach_banner.show()
            except OSError as ex:
                self._insert_sys(f"Could not read image: {ex}")
        dialog.destroy()

    def _clear_attachment(self, *_):
        self.attached_image_b64  = None
        self.attached_image_name = ""
        self._attach_banner.hide()

    def _clear_attachment_ui(self):
        """Called from the GTK thread after a send completes."""
        self._clear_attachment()

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

    def _on_backend_changed(self, w):
        val = w.get_active_text()
        for b in Backend:
            if b.value == val:
                self.backend = b
                break
        self._insert_sys(f"Backend: {self.backend.value}")
        # Show/hide Ollama model combo (irrelevant for cloud backends)
        self.model_combo.set_sensitive(self.backend == Backend.OLLAMA)

    def _on_model_changed(self, w):
        model_name = w.get_active_text()
        if not model_name:
            return
        if not self._is_model_installed(model_name):
            self._prompt_install_model(model_name)
            return
        self.model = model_name
        self.history.clear()
        self._save_history()
        self._update_context_meter()
        self._insert_sys(f"Switched to {self.model}  (history cleared)")

    def _on_key_press(self, _widget, event):
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        ctrl  = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if event.keyval == Gdk.KEY_Return:
            if shift or ctrl:
                # Shift+Enter or Ctrl+Enter → insert literal newline
                self.input_buf.insert_at_cursor("\n")
                return True
            else:
                # Plain Enter → send
                self._send()
                return True
        return False

    def _on_novel_toggled(self, btn):
        self.novel_mode = btn.get_active()
        if self.novel_mode:
            self._insert_sys(
                "✨ Auto-Editor mode ON — paste text and I'll polish it silently")
        else:
            self._insert_sys("Auto-Editor mode OFF — back to normal chat")

    def _stop_generation(self, *_):
        self._cancel_flag = True

    # ── Model management (Ollama) ─────────────────────────────────────────

    def _base_url(self) -> str:
        idx = self.api_url.find("/api/")
        return self.api_url[:idx] if idx != -1 else self.api_url.rstrip("/")

    def _is_model_installed(self, model_name: str) -> bool:
        try:
            req = urllib.request.Request(f"{self._base_url()}/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                for m in data.get("models", []):
                    inst = m.get("name", "")
                    if inst == model_name or inst.startswith(model_name + ":"):
                        return True
                return False
        except Exception:
            return True

    def _prompt_install_model(self, model_name: str):
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Model Not Installed")
        dialog.format_secondary_text(
            f"'{model_name}' is not downloaded.  Pull it from Ollama now?")
        resp = dialog.run(); dialog.destroy()
        if resp == Gtk.ResponseType.YES:
            threading.Thread(
                target=self._pull_worker, args=(model_name,), daemon=True).start()
        else:
            for i, m in enumerate(self.OLLAMA_MODELS):
                if m == self.model:
                    self.model_combo.set_active(i); break

    def _pull_worker(self, model_name: str):
        GLib.idle_add(self.send_btn.set_sensitive, False)
        self.busy = True
        GLib.idle_add(self._insert_sys,
                      f"Downloading {model_name}… this may take several minutes.")
        payload = json.dumps({"name": model_name, "stream": True}).encode()
        try:
            req = urllib.request.Request(
                f"{self._base_url()}/api/pull", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                lbuf = ""
                for raw in resp:
                    lbuf += raw.decode(errors="replace")
                    if not lbuf.endswith("\n"):
                        continue
                    for line in lbuf.splitlines():
                        line = line.strip()
                        if not line: continue
                        try:
                            obj    = json.loads(line)
                            status = obj.get("status", "")
                            total  = obj.get("total", 0)
                            compl  = obj.get("completed", 0)
                            if total and compl:
                                status = f"{status}  {int(compl/total*100)}%"
                            if status:
                                GLib.idle_add(self._set_status, status)
                        except json.JSONDecodeError:
                            pass
                    lbuf = ""
            self.model = model_name
            self.history.clear()
            self._save_history()
            GLib.idle_add(self._update_context_meter)
            GLib.idle_add(self._insert_sys, f"✓ {model_name} installed — history cleared")
        except Exception as ex:
            GLib.idle_add(self._insert_sys, f"✗ Pull failed: {ex}")
            for i, m in enumerate(self.OLLAMA_MODELS):
                if m == self.model:
                    GLib.idle_add(self.model_combo.set_active, i); break
        finally:
            self.busy = False
            GLib.idle_add(self.send_btn.set_sensitive, True)
            GLib.idle_add(self._set_status, "ready")

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
        if not self._placeholder_mark: return False
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
        if self.history and self.history[-1]["role"] == "user":
            failed = self.history.pop()["content"]
            self.input_buf.set_text(failed)
            self._update_context_meter()

    # ── Send pipeline ─────────────────────────────────────────────────────

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
        if self.attached_image_name:
            buf.insert(buf.get_end_iter(),
                       f"[📷 {self.attached_image_name}] {prompt}\n\n")
        else:
            buf.insert(buf.get_end_iter(), prompt + "\n\n")
        self._scroll_end()

        threading.Thread(target=self._worker, daemon=True).start()

    # ── Worker: routes to the correct streaming backend ───────────────────

    def _worker(self):
        self._trim_history_to_budget()
        GLib.idle_add(self._begin_response)

        # Resolved model label for the speaker badge
        if self.backend == Backend.OLLAMA:
            resp_label = self.model
        elif self.backend == Backend.GEMINI:
            resp_label = f"Gemini ({self.gemini_model})"
        else:
            resp_label = f"Claude ({self.claude_model})"

        full_response  = ""
        first_token    = True
        request_failed = False
        token_buffer   = ""
        last_ui_update = time.time()

        def on_token(text: str):
            nonlocal full_response, token_buffer, first_token, last_ui_update
            full_response  += text
            token_buffer   += text
            now = time.time()
            if token_buffer and (now - last_ui_update > TOKEN_BATCH_MS or first_token):
                GLib.idle_add(self._append_token, token_buffer, first_token)
                token_buffer   = ""
                last_ui_update = now
                first_token    = False

        def is_cancelled() -> bool:
            return self._cancel_flag

        try:
            if self.backend == Backend.OLLAMA:
                self._stream_ollama(on_token, is_cancelled)
            elif self.backend == Backend.GEMINI:
                self._stream_gemini(on_token, is_cancelled)
            elif self.backend == Backend.CLAUDE:
                self._stream_claude(on_token, is_cancelled)

            # Flush any remainder
            if token_buffer:
                GLib.idle_add(self._append_token, token_buffer, first_token)

        except urllib.error.URLError as ex:
            msg = f"[Connection error: {ex.reason}]"
            full_response = msg
            GLib.idle_add(self._append_token, msg, first_token)
            GLib.idle_add(self._restore_prompt_on_fail)
            request_failed = True
        except (socket.timeout, ConnectionResetError) as ex:
            msg = f"[Network timeout / reset: {ex}]"
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

        # Clear image attachment after each send (win or fail)
        GLib.idle_add(self._clear_attachment_ui)

        if not request_failed:
            self.history.append({"role": "assistant", "content": full_response})
            self._save_history()

        GLib.idle_add(self._finish_response,
                      "stopped" if self._cancel_flag else "ready")

    # ── Backend streamers ─────────────────────────────────────────────────

    def _stream_ollama(self, on_token, is_cancelled):
        """Stream /api/chat on localhost Ollama."""
        sys_prompt = (self.system_prompt.strip()
                      or "You are a helpful and concise AI assistant.")
        if self.novel_mode:
            sys_prompt = (
                "You are a silent copyeditor. The user will send you raw draft text. "
                "Your ONLY job is to return the polished, corrected version of that "
                "text with improved grammar, flow, and clarity. "
                "Output ONLY the rewritten text — no preamble, no explanation, "
                "no commentary, no quotation marks around the result. "
                "Preserve the author's voice and intent."
            )

        # Build messages — inject image into last user message if attached
        messages = [{"role": "system", "content": sys_prompt}]
        for i, msg in enumerate(self.history):
            if (i == len(self.history) - 1
                    and msg["role"] == "user"
                    and self.attached_image_b64):
                messages.append({
                    "role":    "user",
                    "content": msg["content"],
                    "images":  [self.attached_image_b64],
                })
            else:
                messages.append(msg)

        payload = json.dumps({
            "model":    self.model,
            "messages": messages,
            "stream":   True,
            "options":  {
                "temperature":    self.temperature,
                "num_ctx":        self.num_ctx,
                "num_predict":    self.num_predict,
                "num_gpu":        self.num_gpu,
                "repeat_penalty": 1.1,
                "top_k":          40,
                "top_p":          0.9,
            },
        }).encode()

        req = urllib.request.Request(
            self.api_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            lbuf = ""
            for raw in resp:
                if is_cancelled():
                    resp.close(); on_token(" [stopped]"); return
                lbuf += raw.decode(errors="replace")
                if not lbuf.endswith("\n"):
                    continue
                done = False
                for line in lbuf.splitlines():
                    line = line.strip()
                    if not line: continue
                    try:
                        obj   = json.loads(line)
                        token = obj.get("message", {}).get("content", "")
                        if token:
                            on_token(token)
                        if obj.get("done"):
                            done = True
                    except json.JSONDecodeError:
                        pass
                lbuf = ""
                if done:
                    break

    def _stream_gemini(self, on_token, is_cancelled):
        """Stream Gemini via REST SSE (?alt=sse)."""
        if not self.gemini_key:
            on_token("[Error: No Gemini API key — open ⚙ Settings → Cloud APIs]")
            return

        sys_prompt = (self.system_prompt.strip()
                      or "You are a helpful and concise AI assistant.")
        if self.novel_mode:
            sys_prompt = (
                "You are a silent copyeditor. The user will send you raw draft text. "
                "Your ONLY job is to return the polished, corrected version of that "
                "text with improved grammar, flow, and clarity. "
                "Output ONLY the rewritten text — no preamble, no explanation, "
                "no commentary, no quotation marks around the result. "
                "Preserve the author's voice and intent."
            )

        # Convert history to Gemini format
        contents = []
        for i, msg in enumerate(self.history):
            role = "model" if msg["role"] == "assistant" else "user"
            parts = []
            if (i == len(self.history) - 1
                    and msg["role"] == "user"
                    and self.attached_image_b64):
                parts.append({"inline_data": {
                    "mime_type": "image/jpeg",
                    "data":      self.attached_image_b64,
                }})
            parts.append({"text": msg["content"]})
            contents.append({"role": role, "parts": parts})

        payload = json.dumps({
            "system_instruction": {"parts": [{"text": sys_prompt}]},
            "contents":           contents,
            "generationConfig":   {
                "temperature":   self.temperature,
                "maxOutputTokens": self.num_predict,
            },
        }).encode()

        # v1beta: accepts preview models AND flexible JSON payloads.
        # v1 (production) is stricter and rejects preview model IDs with 400.
        clean_model = self.gemini_model.split("/")[-1]
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{clean_model}:streamGenerateContent"
               f"?alt=sse&key={self.gemini_key}")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp_cm = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as http_err:
            # Read the full error body so we can surface Google's actual message
            # rather than just the numeric status code.
            try:
                body = http_err.read().decode(errors="replace")
                err_json = json.loads(body)
                google_msg = (err_json[0]["error"]["message"]
                              if isinstance(err_json, list)
                              else err_json.get("error", {}).get("message", body))
            except Exception:
                google_msg = http_err.reason
            print(f"[Gemini HTTPError {http_err.code}] {google_msg}", flush=True)
            on_token(f"[Gemini {http_err.code}: {google_msg}]")
            return

        with resp_cm as resp:
            lbuf = ""
            for raw in resp:
                if is_cancelled():
                    resp.close(); on_token(" [stopped]"); return
                lbuf += raw.decode(errors="replace")
                if not lbuf.endswith("\n"):
                    continue
                for line in lbuf.splitlines():
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    json_str = line[6:]
                    if json_str == "[DONE]":
                        return
                    try:
                        obj  = json.loads(json_str)
                        text = (obj.get("candidates", [{}])[0]
                                   .get("content", {})
                                   .get("parts", [{}])[0]
                                   .get("text", ""))
                        if text:
                            on_token(text)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        pass
                lbuf = ""

    def _stream_claude(self, on_token, is_cancelled):
        """Stream Anthropic Claude via SSE messages API."""
        if not self.claude_key:
            on_token("[Error: No Claude API key — open ⚙ Settings → Cloud APIs]")
            return

        sys_prompt = (self.system_prompt.strip()
                      or "You are a helpful and concise AI assistant.")
        if self.novel_mode:
            sys_prompt = (
                "You are a silent copyeditor. The user will send you raw draft text. "
                "Your ONLY job is to return the polished, corrected version of that "
                "text with improved grammar, flow, and clarity. "
                "Output ONLY the rewritten text — no preamble, no explanation, "
                "no commentary, no quotation marks around the result. "
                "Preserve the author's voice and intent."
            )

        # Convert history to Claude format (supports image content blocks)
        messages = []
        for i, msg in enumerate(self.history):
            if (i == len(self.history) - 1
                    and msg["role"] == "user"
                    and self.attached_image_b64):
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type":       "base64",
                            "media_type": "image/jpeg",
                            "data":       self.attached_image_b64,
                        }},
                        {"type": "text", "text": msg["content"]},
                    ],
                })
            else:
                messages.append({"role": msg["role"], "content": msg["content"]})

        payload = json.dumps({
            "model":      self.claude_model,
            "system":     sys_prompt,
            "messages":   messages,
            "max_tokens": self.num_predict,
            "stream":     True,
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         self.claude_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            lbuf = ""
            for raw in resp:
                if is_cancelled():
                    resp.close(); on_token(" [stopped]"); return
                lbuf += raw.decode(errors="replace")
                if not lbuf.endswith("\n"):
                    continue
                for line in lbuf.splitlines():
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    json_str = line[6:]
                    try:
                        obj = json.loads(json_str)
                        if obj.get("type") == "content_block_delta":
                            text = obj.get("delta", {}).get("text", "")
                            if text:
                                on_token(text)
                    except json.JSONDecodeError:
                        pass
                lbuf = ""

    # ── GTK-thread callbacks ──────────────────────────────────────────────

    def _begin_response(self):
        if self.backend == Backend.OLLAMA:
            label = self.model
        elif self.backend == Backend.GEMINI:
            label = f"Gemini ({self.gemini_model})"
        else:
            label = f"Claude ({self.claude_model})"

        buf = self.chat_buf
        buf.insert_with_tags_by_name(
            buf.get_end_iter(), f"{label}\n", "model_label")
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
            si       = buf.get_iter_at_offset(self._resp_start)
            raw_text = buf.get_text(si, buf.get_end_iter(), False)
            self.highlighter.insert_code_headers(self._resp_start, raw_text)
            si2       = buf.get_iter_at_offset(self._resp_start)
            full_text = buf.get_text(si2, buf.get_end_iter(), False)
            self.highlighter.highlight_message(self._resp_start, full_text)
            self._resp_start = None

        self.busy = False
        self.send_btn.set_sensitive(True)
        self.stop_btn.hide()
        self._set_status(status)
        self._update_context_meter()
        self._scroll_if_at_bottom()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    win = OllamaChat()
    win.show_all()
    Gtk.main()
