"""
Eoffice Learning Assistant - System Tray Popup
=================================================
- Hasil jawaban ditampilkan sebagai popup dekat tray icon
- Kontrol via klik kanan tray icon (menu)
- Konfigurasi: config.json (dibuat otomatis)
- Log: error.log

Menu Tray:
  Capture Now  - capture sekali sekarang
  Start Auto   - mulai auto-capture
  Stop Auto    - hentikan auto-capture
  Status       - lihat status
  Exit         - keluar
"""

import os
import sys
import json
import time
import base64
import logging
import threading
from io import BytesIO
from pathlib import Path

import ctypes
import tkinter as tk
from tkinter import ttk

import mss
from PIL import Image

import pystray
from pystray import MenuItem as item
from PIL import Image as PILImage, ImageDraw
import keyboard

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR    = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH    = BASE_DIR / "error.log"

APP_TITLE    = "ExAI"
APP_VERSION  = "3.0"
CAPTURE_HOTKEY = "ctrl+alt+s"   # shortcut global untuk capture
HIDE_HOTKEY    = "ctrl+alt+h"   # shortcut untuk hide/show popup

# ---------------------------------------------------------------------------
# System Prompt — dibaca dari file eksternal (user buat sendiri)
# ---------------------------------------------------------------------------
_PROMPT_FILE = BASE_DIR / "promp_AI.txt"

def _load_prompt_from_file() -> str:
    """Baca prompt dari file promp_AI.txt. Error jika file tidak ada."""
    try:
        if _PROMPT_FILE.exists():
            return _PROMPT_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:
        log.warning(f"Gagal baca prompt file: {e}")
    return None


DEFAULT_CONFIG = {
    "interval_seconds": 10,
    "ai_provider": "",          # "gemini" or "openai" — diisi via setup dialog
    "api_key": "",              # API key — diisi via setup dialog
    "model": "",                # kosong = auto (pakai default per provider)
    "user_prompt": (
        "Perhatikan screenshot ini. "
        "Identifikasi soal pilihan ganda CEH v13 yang ada, lalu tentukan jawaban paling tepat."
    ),
    "auto_start": False
}

# Default model per provider
_DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception as e:
            log.warning(f"Gagal baca config, pakai default: {e}")
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Gagal simpan config: {e}")


# ---------------------------------------------------------------------------
# Tk Thread Manager — satu root Tk, semua UI pakai Toplevel
# ---------------------------------------------------------------------------
_tk_root  = None
_tk_ready = threading.Event()

def _start_tk_loop():
    global _tk_root
    _tk_root = tk.Tk()
    _tk_root.withdraw()
    _tk_ready.set()
    _tk_root.mainloop()

threading.Thread(target=_start_tk_loop, daemon=True).start()
_tk_ready.wait(timeout=5)


# ---------------------------------------------------------------------------
# Anti-Capture: buat window invisible dari screen capture/recording
# ---------------------------------------------------------------------------
def _hide_from_capture(tk_win):
    """
    Windows 10 2004+: SetWindowDisplayAffinity dengan WDA_EXCLUDEFROMCAPTURE.
    Window tetap terlihat di monitor fisik, tapi INVISIBLE di:
    - Screen recording
    - Screen sharing / screen mirroring
    - PrintScreen oleh app lain
    - Proctoring software screen capture
    """
    try:
        hwnd = tk_win.winfo_id()
        WDA_EXCLUDEFROMCAPTURE = 0x00000011
        result = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        if not result:
            # Fallback: WDA_MONITOR — tampil hitam di capture
            WDA_MONITOR = 0x00000001
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Popup custom (Toplevel) — auto-hide, muncul di pojok kanan bawah
# ---------------------------------------------------------------------------
POPUP_DURATION_MS = 15000
POPUP_FADE_STEPS  = 20
POPUP_FADE_MS     = 15

# Track active popups supaya bisa ditutup otomatis saat popup baru muncul
_active_popups = []

def _close_all_popups():
    """Tutup semua popup yang sedang aktif."""
    for win_ref in _active_popups[:]:
        try:
            win_ref.destroy()
        except (tk.TclError, Exception):
            pass
    _active_popups.clear()

def show_popup(_tray_icon, title: str, message: str, duration_ms: int = POPUP_DURATION_MS):
    def _create():
        try:
            # Tutup popup sebelumnya
            _close_all_popups()

            win = tk.Toplevel(_tk_root)
            _active_popups.append(win)

            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.85)
            win.configure(bg="#1a1a2e")

            # Anti-capture: invisible dari recording
            win.update_idletasks()
            _hide_from_capture(win)

            pad = 10
            frame = tk.Frame(win, bg="#1a1a2e", padx=pad, pady=pad)
            frame.pack(fill="both", expand=True)

            header = tk.Frame(frame, bg="#1a1a2e")
            header.pack(fill="x")

            tk.Label(
                header, text=title,
                bg="#1a1a2e", fg="#7aa2f7",
                font=("Segoe UI", 9, "bold"),
                anchor="w", justify="left"
            ).pack(side="left", fill="x", expand=True)

            close_btn = tk.Label(
                header, text=" × ",
                bg="#1a1a2e", fg="#565f89",
                font=("Segoe UI", 8),
                cursor="hand2",
            )
            close_btn.pack(side="right")

            msg = message if len(message) <= 350 else message[:347] + "..."
            tk.Label(
                frame, text=msg,
                bg="#1a1a2e", fg="#a9b1d6",
                font=("Segoe UI", 8),
                anchor="w", justify="left",
                wraplength=280
            ).pack(fill="x", pady=(4, 0))

            win.update_idletasks()
            w = max(win.winfo_reqwidth(), 280)
            h = win.winfo_reqheight()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            margin = 8
            x = sw - w - margin
            y = sh - h - margin - 44
            win.geometry(f"{w}x{h}+{x}+{y}")

            def close(_event=None):
                try:
                    if win in _active_popups:
                        _active_popups.remove(win)
                    win.destroy()
                except tk.TclError:
                    pass

            close_btn.bind("<Button-1>", close)

            def fade_out(step=0):
                if step >= POPUP_FADE_STEPS:
                    close()
                    return
                alpha = 0.85 * (1 - step / POPUP_FADE_STEPS)
                try:
                    win.attributes("-alpha", alpha)
                    win.after(POPUP_FADE_MS, fade_out, step + 1)
                except tk.TclError:
                    pass

            win.after(duration_ms, fade_out)

        except Exception as e:
            log.warning(f"Popup gagal: {e}")

    _tk_root.after(0, _create)


# ---------------------------------------------------------------------------
# AI Engine (Gemini + OpenAI)
# ---------------------------------------------------------------------------
import re as _re

class AIEngine:
    def __init__(self, config: dict):
        self.config  = config
        self._model  = None       # untuk Gemini
        self._client = None       # untuk OpenAI

    @property
    def provider(self) -> str:
        return self.config.get("ai_provider", "gemini").lower()

    @property
    def model_name(self) -> str:
        m = self.config.get("model", "").strip()
        return m or _DEFAULT_MODELS.get(self.provider, "gemini-2.5-flash")

    def _get_gemini_model(self):
        if self._model is None:
            import google.generativeai as genai
            api_key = self.config.get("api_key", "").strip()
            if not api_key:
                raise EnvironmentError("API Key belum diset! Buka Settings di tray.")
            genai.configure(api_key=api_key)
            system_prompt = _load_prompt_from_file()
            if not system_prompt:
                raise FileNotFoundError(
                    f"File 'promp_AI.txt' tidak ditemukan di {BASE_DIR}!\n"
                    "Buat file tersebut berisi system prompt untuk AI."
                )
            self._model = genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=system_prompt,
            )
        return self._model

    def _get_openai_client(self):
        if self._client is None:
            from openai import OpenAI
            api_key = self.config.get("api_key", "").strip()
            if not api_key:
                raise EnvironmentError("API Key belum diset! Buka Settings di tray.")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def capture_area_base64(self) -> str | None:
        """
        Buka overlay fullscreen, user drag pilih area, capture area tsb.
        Return base64 PNG atau None jika batal.
        """
        result = {"b64": None}

        # Stop _tk_root mainloop sementara agar tidak ada 2 Tk sekaligus
        global _tk_root, _tk_ready
        try:
            _tk_root.quit()
        except Exception:
            pass
        time.sleep(0.15)

        def _snip():
            try:
                snip_root = tk.Tk()
                snip_root.attributes("-fullscreen", True)
                snip_root.attributes("-topmost", True)
                snip_root.attributes("-alpha", 0.15)
                snip_root.configure(bg="black")
                snip_root.config(cursor="crosshair")

                # Anti-capture: overlay invisible dari recording
                snip_root.update_idletasks()
                _hide_from_capture(snip_root)

                canvas = tk.Canvas(snip_root, bg="black", highlightthickness=0)
                canvas.pack(fill="both", expand=True)

                start   = {"x": 0, "y": 0}
                rect_id = [None]

                def on_press(event):
                    start["x"] = event.x_root
                    start["y"] = event.y_root
                    if rect_id[0]:
                        canvas.delete(rect_id[0])
                    rect_id[0] = canvas.create_rectangle(
                        event.x, event.y, event.x, event.y,
                        outline="#89b4fa", width=2
                    )

                def on_drag(event):
                    if rect_id[0]:
                        canvas.coords(
                            rect_id[0],
                            start["x"] - snip_root.winfo_rootx(),
                            start["y"] - snip_root.winfo_rooty(),
                            event.x, event.y
                        )

                def on_release(event):
                    x1 = min(start["x"], event.x_root)
                    y1 = min(start["y"], event.y_root)
                    x2 = max(start["x"], event.x_root)
                    y2 = max(start["y"], event.y_root)
                    snip_root.destroy()

                    if (x2 - x1) < 20 or (y2 - y1) < 20:
                        log.info("Area terlalu kecil, batal capture.")
                        return

                    # Tunggu overlay benar-benar hilang dari layar
                    time.sleep(0.5)

                    with mss.MSS() as sct:
                        region = {"left": x1, "top": y1,
                                  "width": x2 - x1, "height": y2 - y1}
                        shot = sct.grab(region)
                        img  = Image.frombytes("RGB", shot.size, shot.rgb)
                        buf  = BytesIO()
                        img.save(buf, format="PNG")
                        result["b64"] = base64.b64encode(
                            buf.getvalue()
                        ).decode("utf-8")

                def on_cancel(event=None):
                    try:
                        keyboard.unhook_key('delete')
                    except Exception:
                        pass
                    try:
                        snip_root.destroy()
                    except Exception:
                        pass

                canvas.bind("<ButtonPress-1>", on_press)
                canvas.bind("<B1-Motion>", on_drag)
                canvas.bind("<ButtonRelease-1>", on_release)

                snip_root.bind("<Delete>", on_cancel)
                canvas.bind("<Delete>", on_cancel)
                keyboard.on_press_key('delete', lambda _: snip_root.after(0, on_cancel))

                snip_root.focus_force()
                canvas.focus_set()

                snip_root.mainloop()
            except Exception as e:
                log.error(f"Snip gagal: {e}")

        t = threading.Thread(target=_snip, daemon=True)
        t.start()
        t.join()

        # Restart _tk_root mainloop untuk popup
        _tk_root  = None
        _tk_ready = threading.Event()
        threading.Thread(target=_start_tk_loop, daemon=True).start()
        _tk_ready.wait(timeout=5)

        time.sleep(0.1)
        return result["b64"]

    def ask(self, image_b64: str) -> dict:
        if self.provider == "openai":
            return self._ask_openai(image_b64)
        return self._ask_gemini(image_b64)

    def _ask_gemini(self, image_b64: str) -> dict:
        model       = self._get_gemini_model()
        user_prompt = self.config.get("user_prompt", DEFAULT_CONFIG["user_prompt"])
        image_data  = {"mime_type": "image/png", "data": image_b64}
        response    = model.generate_content([user_prompt, image_data])
        return self._parse(response.text.strip())

    def _ask_openai(self, image_b64: str) -> dict:
        client      = self._get_openai_client()
        user_prompt = self.config.get("user_prompt", DEFAULT_CONFIG["user_prompt"])
        system_prompt = _load_prompt_from_file()
        if not system_prompt:
            raise FileNotFoundError(
                f"File 'promp_AI.txt' tidak ditemukan di {BASE_DIR}!\n"
                "Buat file tersebut berisi system prompt untuk AI."
            )
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_b64}"
                    }}
                ]}
            ],
            max_tokens=1024,
        )
        return self._parse(response.choices[0].message.content.strip())

    @staticmethod
    def _parse(raw: str) -> dict:
        """
        Parse output format dari prompt CEH v13.
        Mencari:
        - '✅ [LETTER]' untuk jawaban
        - '### Confidence Level' untuk confidence
        - Teks opsi jawaban yang benar dari '### Option Analysis'
        - '### Why This Answer Is Correct' untuk penjelasan
        """
        try:
            # Cari jawaban: baris yang mengandung ✅ diikuti huruf A-D
            answer = "?"
            match = _re.search(r"✅\s*([A-Da-d])", raw)
            if match:
                answer = match.group(1).upper()

            # Cari confidence level
            confidence = ""
            conf_match = _re.search(
                r"###\s*Confidence Level\s*\n\s*(High|Medium|Low)",
                raw, _re.IGNORECASE
            )
            if conf_match:
                confidence = conf_match.group(1).strip()

            # Cari teks opsi jawaban dari Option Analysis section
            option_text = ""
            if answer in ("A", "B", "C", "D"):
                # Cari di dalam section Option Analysis
                opt_section = _re.search(
                    r"###\s*Option Analysis\s*\n(.+?)(?:\n###|\n====|$)",
                    raw, _re.DOTALL
                )
                search_text = opt_section.group(1) if opt_section else raw

                # Pattern: huruf jawaban diikuti titik/paren lalu teks
                opt_match = _re.search(
                    rf"^\s*\**{answer}[.):]\**\s*(.+?)$",
                    search_text, _re.MULTILINE
                )
                if opt_match:
                    text = opt_match.group(1).strip()
                    # Bersihkan emoji dan markdown bold
                    text = _re.sub(r"[✔✅❌]", "", text).strip()
                    text = _re.sub(r"\*+", "", text).strip()
                    if text:
                        option_text = text

            # Cari penjelasan setelah "Why This Answer Is Correct"
            explanation = ""
            exp_match = _re.search(
                r"###\s*Why This Answer Is Correct\s*\n(.+?)(?:\n###|\n====|$)",
                raw, _re.DOTALL
            )
            if exp_match:
                explanation = exp_match.group(1).strip()
                if len(explanation) > 250:
                    explanation = explanation[:247] + "..."

            return {
                "answer":      answer,
                "option_text": option_text,
                "confidence":  confidence,
                "explanation": explanation,
            }
        except Exception:
            return {"answer": "?", "option_text": "", "confidence": "", "explanation": ""}


# ---------------------------------------------------------------------------
# Tray Icon Builder
# ---------------------------------------------------------------------------
def make_tray_image() -> PILImage.Image:
    """Load ikon tray dari file 15.ico di folder yang sama dengan script."""
    TRAY_SIZE = 64
    ico_path = BASE_DIR / "15.ico"

    if ico_path.exists():
        try:
            img = PILImage.open(ico_path)

            # ICO bisa punya multiple sizes — pilih yang paling dekat dengan TRAY_SIZE
            if hasattr(img, "ico") and img.ico.sizes():
                sizes = img.ico.sizes()
                best = min(sizes, key=lambda s: abs(s[0] - TRAY_SIZE))
                img.size = best  # hint ke PIL untuk load ukuran ini
                img.seek(0)

            img = img.convert("RGBA")

            # Resize hanya jika perlu, pakai LANCZOS agar halus
            if img.size != (TRAY_SIZE, TRAY_SIZE):
                img = img.resize((TRAY_SIZE, TRAY_SIZE), PILImage.LANCZOS)

            return img

        except Exception as e:
            log.warning(f"Gagal load 15.ico, pakai ikon default: {e}")

    # Fallback: lingkaran biru dengan antialiasing (render 2× lalu downscale)
    scale = 2
    big = TRAY_SIZE * scale
    img = PILImage.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 4 * scale
    draw.ellipse(
        [pad, pad, big - pad, big - pad],
        fill=(30, 120, 220),
    )
    return img.resize((TRAY_SIZE, TRAY_SIZE), PILImage.LANCZOS)


# ---------------------------------------------------------------------------
# Capture Service (Manual Only)
# ---------------------------------------------------------------------------
RATE_LIMIT_MAX   = 2        # max request per window
RATE_LIMIT_WINDOW = 60      # window dalam detik

class CaptureService:
    def __init__(self, engine: AIEngine, config: dict, tray_ref):
        self.engine    = engine
        self.config    = config
        self.tray_ref  = tray_ref
        self._busy     = False
        self._lock     = threading.Lock()
        self._requests = []   # timestamps of recent requests

    def reload_config(self) -> dict:
        fresh = load_config()
        self.config.update(fresh)
        self.engine.config.update(fresh)
        self.engine._model  = None   # reset Gemini model
        self.engine._client = None   # reset OpenAI client
        log.info("Config berhasil di-reload.")
        return fresh

    def _notify(self, title: str, message: str, duration_ms: int = POPUP_DURATION_MS):
        icon = self.tray_ref()
        if icon:
            show_popup(icon, title, message, duration_ms=duration_ms)

    def _check_rate_limit(self) -> int | None:
        """Cek rate limit. Return detik tunggu jika kena limit, None jika ok."""
        now = time.time()
        # Buang timestamps yang sudah lewat window
        self._requests = [t for t in self._requests if now - t < RATE_LIMIT_WINDOW]
        if len(self._requests) >= RATE_LIMIT_MAX:
            oldest = self._requests[0]
            wait = int(RATE_LIMIT_WINDOW - (now - oldest)) + 1
            return max(wait, 1)
        return None

    def capture_now(self):
        """User klik Capture → cek busy → cek rate limit → buka snip → Gemini."""
        with self._lock:
            if self._busy:
                self._notify(
                    "⏳ Harap Tunggu",
                    "Proses sebelumnya masih berjalan.\nTunggu hingga selesai sebelum capture lagi.",
                    duration_ms=3000
                )
                log.info("Capture ditolak: masih busy.")
                return

            # Cek rate limit
            wait = self._check_rate_limit()
            if wait:
                self._notify(
                    "😏 Sabar Bro...",
                    f"Nanti ketahuan kalo jawabnya terlalu cepat! 😂\nSantai dulu {wait} detik ya.",
                    duration_ms=4000
                )
                log.info(f"Capture ditolak: rate limit, tunggu {wait}s.")
                return

            self._busy = True

        threading.Thread(target=self._process, daemon=True).start()

    def _process(self):
        try:
            # Tutup popup yang sedang aktif sebelum buka snip tool
            # (mencegah Tcl crash dari 2 Tk instance bersamaan)
            _tk_root.after(0, _close_all_popups)
            time.sleep(0.2)

            log.info("Membuka snipping tool...")
            img_b64 = self.engine.capture_area_base64()

            if img_b64 is None:
                log.info("Capture dibatalkan oleh user.")
                return

            # Tampilkan popup loading
            self._notify(
                "⏳ Memproses...",
                "Mengirim screenshot ke Gemini AI.\nHarap tunggu beberapa detik...",
                duration_ms=30000  # akan diganti oleh popup hasil
            )
            log.info("Mengirim ke Gemini...")
            self._requests.append(time.time())  # catat timestamp request
            result = self.engine.ask(img_b64)

            answer      = result.get("answer", "?")
            option_text = result.get("option_text", "")
            confidence  = result.get("confidence", "")
            explanation = result.get("explanation", "")

            log.info(f"Hasil → answer={answer} | confidence={confidence} | option={option_text!r}")

            if answer in ("A", "B", "C", "D"):
                # Emoji confidence
                conf_emoji = {
                    "High":   "\U0001f60e High",
                    "Medium": "\U0001f914 Medium",
                    "Low":    "\U0001f628 Low",
                }.get(confidence, confidence)
                conf_tag = f" | {conf_emoji}" if confidence else ""
                title = f"✅ Jawaban: {answer}{conf_tag}"

                # Body: teks opsi + penjelasan
                parts = []
                if option_text:
                    parts.append(f"{answer}. {option_text}")
                if explanation:
                    parts.append(f"\n{explanation}")
                body = "\n".join(parts) if parts else "Jawaban ditemukan."
                self._notify(title, body)

            elif answer == "?":
                log.info("Soal tidak terdeteksi / tidak lengkap.")
                self._notify(
                    "❌ Tidak Ada Soal",
                    "Tidak ada soal pilihan ganda yang terdeteksi pada area yang di-capture.\n"
                    "Pastikan area yang dipilih mencakup soal lengkap beserta pilihan jawabannya.",
                    duration_ms=8000
                )
            else:
                self._notify(
                    "⚠ Soal Tidak Lengkap",
                    explanation or "Soal tidak terbaca lengkap. Coba capture ulang dengan area yang lebih besar.",
                    duration_ms=8000
                )

        except Exception as e:
            log.error(f"Proses gagal: {e}")
            self._notify(
                "❌ Error",
                f"Terjadi kesalahan:\n{e}",
                duration_ms=10000
            )

        finally:
            with self._lock:
                self._busy = False


# ---------------------------------------------------------------------------
# Setup Dialog — muncul saat pertama kali / belum ada API key
# ---------------------------------------------------------------------------
def _show_setup_dialog(config: dict) -> dict:
    """Modal dialog untuk setup AI provider + API key. Return updated config."""
    result = {"ok": False}

    def _create():
        try:
            win = tk.Toplevel(_tk_root)
            win.title("Setup - ExAI")
            win.attributes("-topmost", True)
            win.configure(bg="#1a1a2e")
            win.resizable(False, False)

            _hide_from_capture(win)

            # --- Main frame ---
            frm = tk.Frame(win, bg="#1a1a2e", padx=24, pady=20)
            frm.pack(fill="both", expand=True)

            tk.Label(
                frm, text="\U0001f527 ExAI Setup",
                bg="#1a1a2e", fg="#7aa2f7",
                font=("Segoe UI", 12, "bold")
            ).pack(pady=(0, 14))

            # --- Provider ---
            tk.Label(
                frm, text="AI Provider:",
                bg="#1a1a2e", fg="#a9b1d6",
                font=("Segoe UI", 9)
            ).pack(anchor="w")

            provider_var = tk.StringVar(value=config.get("ai_provider", "gemini") or "gemini")
            prov_frame = tk.Frame(frm, bg="#1a1a2e")
            prov_frame.pack(fill="x", pady=(2, 10))

            style = ttk.Style()
            style.configure("Dark.TRadiobutton",
                background="#1a1a2e", foreground="#a9b1d6",
                font=("Segoe UI", 9))

            tk.Radiobutton(
                prov_frame, text="\U0001f48e Gemini (Google)",
                variable=provider_var, value="gemini",
                bg="#1a1a2e", fg="#a9b1d6", selectcolor="#313244",
                activebackground="#1a1a2e", activeforeground="#7aa2f7",
                font=("Segoe UI", 9),
            ).pack(side="left", padx=(0, 16))

            tk.Radiobutton(
                prov_frame, text="\U0001f916 OpenAI",
                variable=provider_var, value="openai",
                bg="#1a1a2e", fg="#a9b1d6", selectcolor="#313244",
                activebackground="#1a1a2e", activeforeground="#7aa2f7",
                font=("Segoe UI", 9),
            ).pack(side="left")

            # --- Model (optional) ---
            tk.Label(
                frm, text="Model (kosongkan = default):",
                bg="#1a1a2e", fg="#a9b1d6",
                font=("Segoe UI", 9)
            ).pack(anchor="w")

            model_var = tk.StringVar(value=config.get("model", ""))
            model_entry = tk.Entry(
                frm, textvariable=model_var,
                bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                font=("Segoe UI", 9), relief="flat", bd=4
            )
            model_entry.pack(fill="x", pady=(2, 10))

            # --- API Key ---
            tk.Label(
                frm, text="API Key:",
                bg="#1a1a2e", fg="#a9b1d6",
                font=("Segoe UI", 9)
            ).pack(anchor="w")

            key_var = tk.StringVar(value=config.get("api_key", ""))
            key_entry = tk.Entry(
                frm, textvariable=key_var, show="\u2022",
                bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                font=("Segoe UI", 9), relief="flat", bd=4
            )
            key_entry.pack(fill="x", pady=(2, 4))

            # Show/hide toggle
            show_var = tk.BooleanVar(value=False)
            def toggle_show():
                key_entry.config(show="" if show_var.get() else "\u2022")
            tk.Checkbutton(
                frm, text="Show key", variable=show_var, command=toggle_show,
                bg="#1a1a2e", fg="#565f89", selectcolor="#313244",
                activebackground="#1a1a2e", activeforeground="#7aa2f7",
                font=("Segoe UI", 8)
            ).pack(anchor="w", pady=(0, 14))

            # --- Status label ---
            status_lbl = tk.Label(
                frm, text="",
                bg="#1a1a2e", fg="#f38ba8",
                font=("Segoe UI", 8)
            )
            status_lbl.pack(fill="x")

            # --- Buttons ---
            btn_frame = tk.Frame(frm, bg="#1a1a2e")
            btn_frame.pack(fill="x", pady=(4, 0))

            def on_save():
                provider = provider_var.get().strip()
                api_key  = key_var.get().strip()
                model    = model_var.get().strip()

                if not api_key:
                    status_lbl.config(text="API Key wajib diisi!")
                    return

                config["ai_provider"] = provider
                config["api_key"]     = api_key
                config["model"]       = model
                save_config(config)
                result["ok"] = True
                try:
                    win.destroy()
                except tk.TclError:
                    pass

            def on_cancel():
                try:
                    win.destroy()
                except tk.TclError:
                    pass

            tk.Button(
                btn_frame, text="Save",
                bg="#7aa2f7", fg="#1a1a2e",
                font=("Segoe UI", 9, "bold"),
                relief="flat", padx=20, pady=4,
                cursor="hand2", command=on_save
            ).pack(side="right", padx=(8, 0))

            tk.Button(
                btn_frame, text="Cancel",
                bg="#313244", fg="#a9b1d6",
                font=("Segoe UI", 9),
                relief="flat", padx=14, pady=4,
                cursor="hand2", command=on_cancel
            ).pack(side="right")

            # Center on screen
            win.update_idletasks()
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            x = (win.winfo_screenwidth()  - w) // 2
            y = (win.winfo_screenheight() - h) // 2
            win.geometry(f"+{x}+{y}")

            win.protocol("WM_DELETE_WINDOW", on_cancel)
            key_entry.focus_set()

        except Exception as e:
            log.error(f"Setup dialog gagal: {e}")

    done = threading.Event()

    def _create_and_wait():
        _create()
        # Tunggu window ditutup
        def _check():
            # Cek apakah masih ada Toplevel
            children = [w for w in _tk_root.winfo_children() if w.winfo_exists()]
            if children:
                _tk_root.after(200, _check)
            else:
                done.set()
        _tk_root.after(200, _check)

    _tk_root.after(0, _create_and_wait)
    done.wait()
    return config if result["ok"] else None


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main():
    config = load_config()

    # --- Cek apakah API key sudah diset ---
    if not config.get("api_key", "").strip():
        log.info("API key belum diset, membuka setup dialog...")
        updated = _show_setup_dialog(config)
        if updated is None:
            log.info("Setup dibatalkan. Keluar.")
            return
        config = updated

    engine = AIEngine(config)

    _icon_holder = [None]
    def get_icon():
        return _icon_holder[0]

    service = CaptureService(engine, config, get_icon)

    # --- Menu actions ---
    def on_capture(icon=None, _item=None):
        service.reload_config()
        service.capture_now()

    def on_settings(icon=None, _item=None):
        def _run():
            fresh = load_config()
            updated = _show_setup_dialog(fresh)
            if updated:
                config.update(updated)
                engine.config.update(updated)
                engine._model  = None
                engine._client = None
                log.info(f"Settings diperbarui: provider={updated.get('ai_provider')}")
        threading.Thread(target=_run, daemon=True).start()

    def on_exit(icon, _item):
        keyboard.unhook_all()
        icon.stop()

    # --- Register global hotkeys ---
    keyboard.add_hotkey(CAPTURE_HOTKEY, on_capture)
    keyboard.add_hotkey(HIDE_HOTKEY, lambda: _tk_root.after(0, _close_all_popups))
    log.info(f"Hotkey capture : {CAPTURE_HOTKEY.upper()}")
    log.info(f"Hotkey hide    : {HIDE_HOTKEY.upper()}")

    provider = config.get("ai_provider", "gemini")
    model    = config.get("model", "") or _DEFAULT_MODELS.get(provider, "")
    log.info(f"AI Provider  : {provider.upper()}")
    log.info(f"Model        : {model}")

    # --- Build tray icon ---
    tray_image = make_tray_image()
    menu = pystray.Menu(
        item(f"Capture  ({CAPTURE_HOTKEY.upper()})", on_capture, default=True),
        pystray.Menu.SEPARATOR,
        item("Settings",   on_settings),
        item("Exit",       on_exit),
    )

    icon = pystray.Icon(APP_TITLE, tray_image, APP_TITLE, menu)
    _icon_holder[0] = icon

    log.info(f"{APP_TITLE} {APP_VERSION} berjalan (tray mode).")
    log.info(f"Config : {CONFIG_PATH}")
    log.info(f"Log    : {LOG_PATH}")

    icon.run()


if __name__ == "__main__":
    main()