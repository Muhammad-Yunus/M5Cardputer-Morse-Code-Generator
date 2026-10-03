# Cardputer v1.1 (K132) - Morse Code Generator
# UIFlow2 / MicroPython
#
# Keyboard matrix input, convert ke morse, putar tone via Speaker
# (prioritas PCM sine 440Hz, fallback square wave). Tampilkan kode
# morse di layar, progress indikator, dan status engine.

import math
import time
import M5
from M5 import *

# ---------------- konfigurasi audio ----------------
MORSE_FREQ = 800        # Hz
MORSE_VOL = 90          # 0..100
SR = 16000              # sample rate PCM
CH_SIN = 0
CH_SQR = 1

# durasi morse (ms) — unit base = 90
UNIT = 90
DOT_D = UNIT
DASH_D = UNIT * 3
GAP_INTRA = UNIT        # antar simbol dalam satu huruf
GAP_INTER = UNIT * 3    # antar huruf
GAP_WORD = UNIT * 7     # antar kata (setelah spasi input)

# ---------------- warna ----------------
C_BG      = 0x0B0F14
C_HEAD    = 0x121A24
C_PANEL   = 0x05090D
C_GRID    = 0x16232E
C_TEXT    = 0xE6F1FF
C_DIM     = 0x6B7C8C
C_OK      = 0x39D98A
C_BAD     = 0xFF5C5C
C_WAVE    = 0x35D6FF
C_ACCENT  = 0xFFC24B
C_MORSE   = 0x35D6FF
C_CURSOR  = 0xFFC24B

MORSE_TABLE = {
    'A': '.-',   'B': '-...', 'C': '-.-.', 'D': '-..',
    'E': '.',    'F': '..-.', 'G': '--.',  'H': '....',
    'I': '..',   'J': '.---', 'K': '-.-',  'L': '.-..',
    'M': '--',   'N': '-.',   'O': '---',  'P': '.--.',
    'Q': '--.-', 'R': '.-.',  'S': '...',  'T': '-',
    'U': '..-',  'V': '...-', 'W': '.--',  'X': '-..-',
    'Y': '-.--', 'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--',
    '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..', '9': '----.',
}

S = {
    "run": False,
    "idle": False,
    "audio_ok": False,
    "engine": "SIN",
    "input": "",
    "morse_full": "",
    "seq_events": [],
    "seq_i": 0,
    "seq_active": False,
    "cursor_on": True,
    "blink": 0,
    "fonts": {},
    "kb": None,
    "kb_code": None,
    "btn": None,
    "last_key": "--",
    "canvas": None,
}

# =====================================================================
# AUDIO
# =====================================================================

def _call(fn_name, *args):
    fn = getattr(Speaker, fn_name, None)
    if fn is None:
        return False, None
    try:
        return True, fn(*args)
    except Exception:
        return False, None


def audio_init():
    ok = False
    r = _call("begin")
    ok = bool(r[1]) if r[0] else False
    if not ok:
        _call("config")
        r = _call("begin")
        ok = bool(r[1]) if r[0] else False
    S["audio_ok"] = ok
    if ok:
        _call("setVolume", 255)
        _call("setAllChannelVolume", 255)
        _call("setChannelVolume", CH_SQR, MORSE_VOL * 255 // 100)
    return ok


def build_pcm(dur_ms):
    # PCM buffer untuk durasi tertentu; panjang loop = ceil(dur/periode)
    cycles = int(round(MORSE_FREQ * dur_ms / 1000.0))
    if cycles < 1:
        cycles = 1
    n = int(round(cycles * SR / MORSE_FREQ))
    if n < 16:
        n = 16
    step = 2.0 * math.pi * MORSE_FREQ / SR
    peak = int(32767 * (MORSE_VOL / 100.0) * 0.95)
    buf = bytearray(n * 2)
    for i in range(n):
        v = int(peak * math.sin(step * i))
        buf[2 * i] = v & 0xFF
        buf[2 * i + 1] = (v >> 8) & 0xFF
    return buf


_pcm_dot = None
_pcm_dash = None


def pcm_for(dur_ms):
    global _pcm_dot, _pcm_dash
    # dot=0, dash=1 — dash 3x lebih lama dari dot, loop terpisah
    if dur_ms >= DASH_D:
        if _pcm_dash is None:
            _pcm_dash = build_pcm(DASH_D)
        return _pcm_dash
    if _pcm_dot is None:
        _pcm_dot = build_pcm(DOT_D)
    return _pcm_dot


def stop_morse_audio():
    _call("stop", CH_SIN)
    _call("stop", CH_SQR)


def stop_morse_seq():
    S["seq_active"] = False
    S["seq_i"] = 0
    stop_morse_audio()


def play_morse_beep(duration_ms):
    buf = pcm_for(duration_ms)
    if S["engine"] == "SIN" and S["audio_ok"] and buf is not None:
        _call("stop", CH_SIN)
        fn = getattr(Speaker, "playRaw", None)
        if fn is not None:
            for args in (
                (buf, SR, True, 1, CH_SIN, False),
                (buf, SR, True, 1, CH_SIN),
                (buf, SR, True),
            ):
                try:
                    fn(*args)
                    return
                except Exception:
                    continue
    if S["audio_ok"]:
        _call("setChannelVolume", CH_SQR, MORSE_VOL * 255 // 100)
        fn = getattr(Speaker, "tone", None)
        if fn is not None:
            try:
                fn(MORSE_FREQ, duration_ms, CH_SQR, False)
                return
            except Exception:
                pass
            try:
                fn(MORSE_FREQ, duration_ms)
            except Exception:
                pass


# =====================================================================
# MORSE LOGIC
# =====================================================================

def text_to_morse(text):
    out = []
    for ch in text:
        if ch == " ":
            out.append("//")
        else:
            m = MORSE_TABLE.get(ch.upper())
            if m:
                out.append(m + "/")
    return "".join(out).rstrip("/")


def build_sequence(morse_str):
    """
    Bangun list (durasi_ms, is_on) dari string morse.
    '/' = antar huruf, '//' = antar kata, '.' = dot, '-' = dash
    """
    events = []
    # parse karakter: titik, dash, slash
    i = 0
    while i < len(morse_str):
        ch = morse_str[i]
        if ch == ".":
            events.append((DOT_D, True))
        elif ch == "-":
            events.append((DASH_D, True))
        elif ch == "/":
            # cek // untuk word gap
            if i + 1 < len(morse_str) and morse_str[i + 1] == "/":
                events.append((GAP_WORD, False))
                i += 1
            else:
                events.append((GAP_INTER, False))
        i += 1
    return events


def start_morse():
    morse_str = text_to_morse(S["input"])
    S["morse_full"] = morse_str
    S["seq_events"] = build_sequence(morse_str)
    S["seq_i"] = 0
    S["seq_active"] = True
    S["run"] = False


# =====================================================================
# KEYBOARD
# =====================================================================

def bind_special_keys():
    table = {}
    try:
        from unit import KeyCode
    except Exception:
        return table
    for name in ("KEYCODE_BACKSPACE", "KEYCODE_DEL", "KEYCODE_DELETE",
                 "KEYCODE_BS", "KEYCODE_ENTER", "KEYCODE_ESC"):
        v = getattr(KeyCode, name, None)
        if isinstance(v, int) and not isinstance(v, bool) and not (0x20 <= v <= 0x7E):
            table[name] = v
    return table


def on_key(code):
    if not isinstance(code, int) or isinstance(code, bool):
        return
    S["last_key"] = "%02X" % (code & 0xFF)

    sp = S["fonts"].get("special", {})

    # 1) Enter
    enter = sp.get("KEYCODE_ENTER")
    if code in (0x0D, 0x0A) or (enter is not None and code == enter):
        if S["seq_active"]:
            stop_morse_seq()
        else:
            start_morse()
        return

    # 2) Backspace — kumpulkan kandidat, buang None
    bs_candidates = [sp.get("KEYCODE_BACKSPACE"), sp.get("KEYCODE_DEL"),
                     sp.get("KEYCODE_DELETE"), sp.get("KEYCODE_BS")]
    if code in (0x08, 0x7F) or any(code == c for c in bs_candidates if c is not None):
        if S["seq_active"]:
            stop_morse_seq()
        if S["input"]:
            S["input"] = S["input"][:-1]
        return

    # 3) Escape
    esc = sp.get("KEYCODE_ESC")
    if code == 0x1B or (esc is not None and code == esc):
        if S["seq_active"]:
            stop_morse_seq()
        S["input"] = ""
        S["morse_full"] = ""
        return

    # 4) Karakter cetak
    if 0x20 <= code <= 0x7E:
        c = chr(code)
        if S["seq_active"]:
            return
        if c == " ":
            if len(S["input"]) < 25:
                S["input"] += " "
        elif c.isalpha() or c.isdigit():
            uc = c.upper()
            if len(S["input"]) < 25:
                S["input"] += uc
        return


def kb_callback(kb_obj):
    try:
        S["kb_code"] = kb_obj.get_key()
    except Exception:
        S["kb_code"] = None


def kb_init():
    try:
        from hardware import MatrixKeyboard
    except Exception:
        return False
    try:
        kb = MatrixKeyboard()
        kb.set_callback(kb_callback)
        S["kb"] = kb
        return True
    except Exception:
        return False


def kb_poll():
    kb = S["kb"]
    if kb is None:
        return
    tick = getattr(kb, "tick", None)
    if tick is not None:
        try:
            r = tick()
            if isinstance(r, int) and not isinstance(r, bool):
                S["kb_code"] = r
        except Exception:
            pass
    code = S["kb_code"]
    if code is None:
        return
    S["kb_code"] = None
    on_key(code)


# =====================================================================
# MORSE SEQUENCE SERVICE (jalankan di loop)
# =====================================================================

_morse_t_end = 0
_morse_on = False


def service_morse():
    global _morse_t_end, _morse_on
    if not S["seq_active"]:
        if _morse_on:
            stop_morse_seq()
            _morse_on = False
        return

    now = time.ticks_ms()
    events = S["seq_events"]
    i = S["seq_i"]

    if i >= len(events):
        S["seq_active"] = False
        S["seq_i"] = 0
        if _morse_on:
            stop_morse_seq()
            _morse_on = False
        return

    if _morse_t_end == 0:
        dur, on = events[i]
        _morse_on = on
        if on:
            play_morse_beep(dur)
        _morse_t_end = time.ticks_add(now, dur)

    if time.ticks_diff(now, _morse_t_end) >= 0:
        S["seq_i"] += 1
        _morse_t_end = 0
        S["seq_active"] = S["seq_i"] < len(events)
        if not S["seq_active"]:
            stop_morse_seq()
            _morse_on = False


# =====================================================================
# UI
# =====================================================================

def font_of(*names):
    fonts = getattr(M5.Lcd, "FONTS", None)
    if fonts is None:
        return None
    for n in names:
        h = getattr(fonts, n, None)
        if h is not None:
            return h
    return None


def put(s, x, y, fg, fk, bg=C_BG):
    f = S["fonts"].get(fk)
    if f is not None:
        M5.Lcd.setFont(f)
    M5.Lcd.setTextColor(fg, bg)
    M5.Lcd.drawString(s, x, y)


def draw_header():
    M5.Lcd.fillRect(0, 0, 240, 18, C_HEAD)
    M5.Lcd.drawLine(0, 20, 240, 20, C_HEAD)
    put("MORSE GENERATOR", 6, 2, C_TEXT, "title", C_HEAD)
    # status
    if S["seq_active"]:
        st_col = C_WAVE
        st_txt = "PLAY"
    elif S["audio_ok"]:
        st_col = C_OK
        st_txt = "READY"
    else:
        st_col = C_BAD
        st_txt = "NOAUD"
    put(st_txt, 188, 2, st_col, "title", C_HEAD)
    M5.Lcd.fillRect(180, 6, 4, 4, st_col)


def draw_input():
    M5.Lcd.fillRect(0, 22, 240, 18, C_BG)
    inp = S["input"][:25]
    if S["cursor_on"]:
        disp = inp + "_"
    else:
        disp = inp
    put("> " + disp, 6, 24, C_TEXT, "small")
    if len(S["input"]) >= 25:
        put("+", 232, 24, C_DIM, "small")


def draw_morse_view():
    M5.Lcd.fillRect(0, 42, 240, 26, C_PANEL)
    M5.Lcd.drawRect(2, 44, 236, 22, C_GRID)

    morse_str = S["morse_full"]
    if S["seq_active"] and morse_str:
        # tampilkan progres di morse view
        played = 0
        for i in range(S["seq_i"] + 1):
            dur, on = S["seq_events"][i]
            played += dur
        total = sum(d for d, _ in S["seq_events"])
        ratio = played / total if total else 0

        # bar progres
        M5.Lcd.fillRect(0, 69, 240, 18, C_BG)
        bar_w = int(202 * ratio)
        M5.Lcd.fillRect(2, 74, 202, 4, C_GRID)
        M5.Lcd.fillRect(2, 74, bar_w, 4, C_WAVE)

        # text morse (scrollable ke kiri, tampilkan dari karakter 1)
        mshow = morse_str[:54]
        put(mshow, 6, 48, C_MORSE, "small")
        put("%d%%" % (100 * ratio), 208, 71, C_DIM, "small")
    elif morse_str:
        mshow = morse_str[:54]
        put(mshow, 6, 48, C_MORSE, "small")
        if len(morse_str) > 54:
            put("+", 234, 48, C_DIM, "small")
        M5.Lcd.fillRect(0, 69, 240, 18, C_BG)
        put("ENTER:play  ESC:clr", 6, 71, C_DIM, "small")
    else:
        M5.Lcd.fillRect(0, 69, 240, 18, C_BG)
        put("Type text, then press ENTER", 6, 71, C_DIM, "small")


def draw_morse_preview():
    """Pratinjau morse dari input saat idle."""
    if S["seq_active"]:
        return
    morse_str = text_to_morse(S["input"])
    S["morse_full"] = morse_str
    mshow = morse_str[:54] if morse_str else ""
    put(mshow, 6, 48, C_MORSE, "small")


def draw_hint():
    M5.Lcd.fillRect(0, 69, 240, 18, C_BG)
    if S["seq_active"]:
        put("ENTER:stop  BS:del", 6, 71, C_DIM, "small")
    else:
        put("A-Z0-9 input  ENTER:play  ESC:clear", 6, 71, C_DIM, "small")


def draw_engine_status():
    M5.Lcd.drawLine(0, 117, 240, 117, C_HEAD)
    eng_col = C_OK if S["audio_ok"] else C_BAD
    put("SPK: " + ("OK" if S["audio_ok"] else "OFF"), 6, 119, eng_col, "small")
    put("ENG: " + S["engine"], 68, 119, C_DIM, "small")
    put(S["last_key"], 212, 119, C_DIM, "small")


def draw_all():
    draw_header()
    draw_input()
    draw_morse_view()
    draw_hint()
    draw_engine_status()


# =====================================================================
# SETUP / LOOP
# =====================================================================

def setup():
    M5.begin()
    try:
        M5.Display.setRotation(1)
    except Exception:
        try:
            Widgets.setRotation(1)
        except Exception:
            pass
    try:
        if M5.Lcd.width() < M5.Lcd.height():
            M5.Lcd.setRotation(1)
    except Exception:
        pass

    S["fonts"]["special"] = bind_special_keys()
    S["fonts"]["title"] = font_of("Montserrat14", "Montserrat12", "DejaVu12")
    S["fonts"]["mid"] = font_of("Montserrat18", "Montserrat14", "DejaVu12")
    S["fonts"]["small"] = font_of("Montserrat12", "Montserrat14")

    M5.Lcd.fillScreen(C_BG)
    try:
        S["btn"] = getattr(M5, "BtnA", None)
    except Exception:
        S["btn"] = None
    kb_init()
    audio_init()

    # pre-build buffer dot & dash, tahan referensi agar tidak di-GC
    pcm_for(DOT_D)
    pcm_for(DASH_D)

    draw_all()


def loop():
    M5.update()
    kb_poll()
    service_morse()

    now = time.ticks_ms()
    if time.ticks_diff(now, S["blink"]) >= 500:
        S["blink"] = now
        S["cursor_on"] = not S["cursor_on"]
        if not S["seq_active"]:
            draw_input()
            draw_morse_view()

    if S["seq_active"]:
        draw_morse_view()

    btn = S["btn"]
    if btn is not None:
        try:
            if btn.wasClicked():
                if S["seq_active"]:
                    S["seq_active"] = False
                    stop_morse_seq()
                elif S["input"]:
                    start_morse()
        except Exception:
            pass

    time.sleep_ms(10)


if __name__ == "__main__":
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            from utility import print_error_msg
            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")
