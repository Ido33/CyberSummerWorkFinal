# -*- coding: utf-8 -*-
"""
client/gui_app.py
==================
Local-CyberComm GUI client, built with CustomTkinter.
Dark, WhatsApp-inspired look: chat bubbles, avatar initials, contact rows.

Structure:
  - LoginFrame   : sign in / sign up screen + auto server discovery
  - MainWindow   : contacts (search + paging) and groups list
  - ChatWindow   : private or group chat window (bubbles, files, calls, voice notes)
  - CallWindow   : video call display window

Thread-safety note:
  network_client runs a background thread listening for server messages.
  Tkinter/CTk widgets may only be touched from the main thread. So every
  network callback only *pushes an event*, and the GUI processes it via
  .after(0, ...) on the main thread.
"""

import os
import base64
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import sys

import customtkinter as ctk
from PIL import Image, ImageTk

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.protocol import MsgType, DEFAULT_PORT
from shared.discovery import discover_server
from client.network_client import NetworkClient
from client import file_transfer

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

APP_TITLE = "Local-CyberComm"

_HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
_TRAILING_PUNCT_RE = re.compile(r"^(.*[\u0590-\u05FF])([?!.,:;]+)$")


def is_rtl_text(text: str) -> bool:
    return bool(_HEBREW_RE.search(text or ""))


def fix_rtl(text: str) -> str:
    """
    Tk on this system already shapes each Hebrew word's internal character
    order correctly (via the OS font engine) - the only two things it gets
    wrong are:
      1. The left-to-right *order of the words themselves* in an RTL
         sentence - fixed here by reversing word order (not character
         order within each word).
      2. Trailing punctuation (?!.,:;) attached to a Hebrew word ends up
         displayed on the wrong side - fixed here by moving it to the
         front of the word in the stored string, which compensates for
         Tk's own (backwards) placement of that neutral character.
    Pure Latin text passes through completely unchanged.
    """
    if not text or not is_rtl_text(text):
        return text
    words = text.split(" ")
    fixed_words = []
    for w in words:
        m = _TRAILING_PUNCT_RE.match(w)
        if m:
            core, punct = m.groups()
            w = punct + core
        fixed_words.append(w)
    return " ".join(reversed(fixed_words))

# ---------------------------------------------------------------- palette
COL_BG_SIDEBAR = "#111B21"
COL_BG_CHAT = "#0B141A"
COL_BG_HEADER = "#202C33"
COL_BUBBLE_SELF = "#005C4B"
COL_BUBBLE_OTHER = "#202C33"
COL_TEXT = "#E9EDEF"
COL_TEXT_MUTED = "#8696A0"
COL_ACCENT = "#00A884"
COL_ACCENT_HOVER = "#02906F"
COL_DANGER = "#A33"
COL_ONLINE = "#00D9A0"
COL_OFFLINE = "#54656F"

AVATAR_COLORS = ["#00A884", "#6B4EFF", "#FF6B6B", "#FFA45B", "#4E9CFF", "#FF4EA0", "#4EDCFF", "#B5891B"]


def avatar_color_for(name: str) -> str:
    return AVATAR_COLORS[sum(ord(c) for c in name) % len(AVATAR_COLORS)] if name else COL_TEXT_MUTED


def initials_for(name: str) -> str:
    return (name[:1] or "?").upper()


def make_avatar(parent, name, bg, size=40):
    """A small circle canvas with the contact's initial - a lightweight 'avatar'."""
    color = avatar_color_for(name)
    canvas = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
    canvas.create_oval(2, 2, size - 2, size - 2, fill=color, outline="")
    canvas.create_text(size // 2, size // 2, text=initials_for(name), fill="white",
                        font=("Segoe UI", int(size * 0.4), "bold"))
    return canvas


def bind_click_recursive(widget, handler):
    """Binds a click handler on a widget and all its children (so clicking
    anywhere on a composite row - avatar, label, etc - triggers the same action)."""
    widget.bind("<Button-1>", handler)
    for child in widget.winfo_children():
        bind_click_recursive(child, handler)


# =============================================================================
class LoginFrame(ctk.CTkFrame):
    def __init__(self, master, on_success):
        super().__init__(master, fg_color=COL_BG_SIDEBAR)
        self.on_success = on_success
        self.nc = NetworkClient()

        ctk.CTkLabel(self, text=APP_TITLE, font=ctk.CTkFont(size=26, weight="bold"),
                     text_color=COL_ACCENT).pack(pady=(40, 5))
        ctk.CTkLabel(self, text="Unified LAN communication system", font=ctk.CTkFont(size=13),
                     text_color=COL_TEXT_MUTED).pack(pady=(0, 25))

        server_frame = ctk.CTkFrame(self, fg_color="transparent")
        server_frame.pack(pady=5)
        ctk.CTkLabel(server_frame, text="Server IP:", text_color=COL_TEXT).grid(row=0, column=0, padx=5)
        self.server_entry = ctk.CTkEntry(server_frame, placeholder_text="e.g. 192.168.1.50 (or auto-detect)", width=280)
        self.server_entry.grid(row=0, column=1, padx=5)
        ctk.CTkButton(server_frame, text="Find server", command=self._auto_discover, width=120,
                      fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER).grid(row=0, column=2, padx=5)

        self.username_entry = ctk.CTkEntry(self, placeholder_text="Username", width=300)
        self.username_entry.pack(pady=8)
        self.password_entry = ctk.CTkEntry(self, placeholder_text="Password", show="*", width=300)
        self.password_entry.pack(pady=8)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=15)
        ctk.CTkButton(btns, text="Sign in", command=self._login, width=140,
                      fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER).grid(row=0, column=0, padx=8)
        ctk.CTkButton(btns, text="Sign up", command=self._on_register_click, width=140,
                      fg_color="#555", hover_color="#444").grid(row=0, column=1, padx=8)

        self.status_label = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_label.pack(pady=10)

        self._login_seq = 0
        self._login_pending = False

        self.nc.on(MsgType.LOGIN_OK, lambda p: self.after(0, self._on_login_ok, p))
        self.nc.on(MsgType.LOGIN_ERROR, lambda p: self.after(0, self._on_login_error, p))
        self.nc.on(MsgType.REGISTER_OK, lambda p: self.after(0, self._set_status, p.get("message", "Registered!"), "lightgreen"))
        self.nc.on(MsgType.REGISTER_ERROR, lambda p: self.after(0, self._set_status, p.get("message", "Error"), "red"))

    def _set_status(self, text, color="orange"):
        self.status_label.configure(text=text, text_color=color)

    def _auto_discover(self):
        self._set_status("Searching for a server on the local network...", "orange")

        def worker():
            result = discover_server(timeout=2.5)
            if result:
                ip, port = result
                self.after(0, lambda: (self.server_entry.delete(0, "end"), self.server_entry.insert(0, ip)))
                self.after(0, self._set_status, f"Found server: {ip}:{port}", "lightgreen")
            else:
                self.after(0, self._set_status, "No server found automatically - enter IP manually", "red")

        threading.Thread(target=worker, daemon=True).start()

    def _ensure_connected(self) -> bool:
        if self.nc.connected:
            return True
        ip = self.server_entry.get().strip() or "127.0.0.1"
        try:
            self.nc.connect(ip, DEFAULT_PORT)
            return True
        except OSError as e:
            self._set_status(f"Could not connect to server: {e}", "red")
            return False

    def _login(self):
        if not self._ensure_connected():
            return
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            self._set_status("Please fill in username and password", "red")
            return
        self._set_status("Connecting...", "orange")
        self._login_pending = True
        self._login_seq += 1
        seq = self._login_seq
        self.after(6000, lambda: self._check_login_timeout(seq))
        self.nc.login(username, password)

    def _check_login_timeout(self, seq):
        if self._login_pending and seq == self._login_seq:
            self._login_pending = False
            self._set_status(
                "No response from the server within 6 seconds. Check that the server is "
                "running, the IP is correct, and no firewall is blocking the connection.", "red"
            )

    def _on_login_error(self, payload):
        self._login_pending = False
        self._set_status(payload.get("message", "Error"), "red")

    def _on_register_click(self):
        if not self._ensure_connected():
            return
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            self._set_status("Please fill in username and password", "red")
            return
        self.nc.register(username, password)

    def _on_login_ok(self, payload):
        self._login_pending = False
        self.on_success(self.nc, payload.get("username"))


# =============================================================================
class ChatWindow(ctk.CTkToplevel):
    """Chat window - used for both private (1-1) and group chats, with
    WhatsApp-style message bubbles."""

    def __init__(self, master, app, title, is_group: bool, conv_id):
        super().__init__(master)
        self.app = app
        self.is_group = is_group
        self.conv_id = conv_id  # username if private, group_id if group
        self.title(title)
        self.geometry("560x640")
        self.configure(fg_color=COL_BG_CHAT)

        # ---- header ----
        header = ctk.CTkFrame(self, fg_color=COL_BG_HEADER, height=56, corner_radius=0)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        make_avatar(header, title, COL_BG_HEADER, size=36).pack(side="left", padx=(12, 8), pady=10)
        ctk.CTkLabel(header, text=fix_rtl(title), font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=COL_TEXT).pack(side="left", pady=10)

        # ---- scrollable message feed ----
        self.messages_area = ctk.CTkScrollableFrame(self, fg_color=COL_BG_CHAT)
        self.messages_area.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # ---- input row ----
        bottom = ctk.CTkFrame(self, fg_color=COL_BG_HEADER, corner_radius=0)
        bottom.pack(fill="x", side="bottom")

        self.entry = ctk.CTkEntry(bottom, placeholder_text="Type a message...", height=38)
        self.entry.pack(side="left", fill="x", expand=True, padx=(10, 5), pady=8)
        self.entry.bind("<Return>", lambda e: self._send())

        ctk.CTkButton(bottom, text="Send", width=70, command=self._send,
                      fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER).pack(side="left", padx=3, pady=8)
        ctk.CTkButton(bottom, text="📎 File", width=70, command=self._send_file,
                      fg_color="#3a4a52", hover_color="#2e3b42").pack(side="left", padx=3, pady=8)
        if not is_group:
            ctk.CTkButton(bottom, text="📹 Call", width=80, command=self._start_call,
                          fg_color="#3a4a52", hover_color="#2e3b42").pack(side="left", padx=3, pady=8)
            self.record_btn = ctk.CTkButton(bottom, text="🎤 Record", width=90, command=self._toggle_recording,
                                             fg_color="#3a4a52", hover_color="#2e3b42")
            self.record_btn.pack(side="left", padx=(3, 10), pady=8)
        self._recorder = None
        self._is_recording = False

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if is_group:
            self.app.nc.group_history(conv_id)
        else:
            self.app.nc.private_history(conv_id)

    # -------------------------------------------------------------- bubbles
    def _scroll_to_bottom(self):
        self.messages_area.update_idletasks()
        try:
            self.messages_area._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _bubble_row(self, is_own):
        row = ctk.CTkFrame(self.messages_area, fg_color="transparent")
        row.pack(fill="x", pady=3, anchor="e" if is_own else "w")
        return row

    def append_message(self, sender, text, ts=None):
        is_own = sender == self.app.username
        row = self._bubble_row(is_own)
        bubble = ctk.CTkFrame(row, fg_color=COL_BUBBLE_SELF if is_own else COL_BUBBLE_OTHER,
                               corner_radius=12)
        bubble.pack(side="right" if is_own else "left", padx=10)

        if self.is_group and not is_own:
            ctk.CTkLabel(bubble, text=fix_rtl(sender), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=avatar_color_for(sender)).pack(anchor="w", padx=10, pady=(6, 0))

        text_anchor = "e" if is_rtl_text(text) else "w"
        text_justify = "right" if is_rtl_text(text) else "left"
        ctk.CTkLabel(bubble, text=fix_rtl(text), font=ctk.CTkFont(size=13), text_color=COL_TEXT,
                     wraplength=340, justify=text_justify).pack(anchor=text_anchor, padx=10, pady=(2, 2))

        time_str = time.strftime("%H:%M", time.localtime(ts)) if ts else time.strftime("%H:%M")
        ctk.CTkLabel(bubble, text=time_str, font=ctk.CTkFont(size=10),
                     text_color=COL_TEXT_MUTED).pack(anchor="e", padx=10, pady=(0, 5))
        self._scroll_to_bottom()

    def append_system(self, text):
        row = ctk.CTkFrame(self.messages_area, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(row, text=fix_rtl(text), font=ctk.CTkFont(size=11, slant="italic"),
                     text_color=COL_TEXT_MUTED).pack(anchor="center")
        self._scroll_to_bottom()

    def append_file_link(self, label_prefix, filename, filepath, ts=None):
        """Shows a file message as a clickable bubble (WhatsApp-style) -
        clicking it opens the file on this computer."""
        is_own = label_prefix.startswith("You")
        row = self._bubble_row(is_own)
        bubble = ctk.CTkFrame(row, fg_color=COL_BUBBLE_SELF if is_own else COL_BUBBLE_OTHER,
                               corner_radius=12)
        bubble.pack(side="right" if is_own else "left", padx=10)

        ctk.CTkLabel(bubble, text=fix_rtl(label_prefix), font=ctk.CTkFont(size=10),
                     text_color=COL_TEXT_MUTED).pack(anchor="w", padx=10, pady=(6, 0))

        file_label = ctk.CTkLabel(bubble, text=f"📎 {fix_rtl(filename)}", font=ctk.CTkFont(size=13, underline=True),
                                   text_color="#53BDEB", wraplength=300, justify="left", cursor="hand2")
        file_label.pack(anchor="w", padx=10, pady=(2, 2))
        file_label.bind("<Button-1>", lambda e, p=filepath: self._open_local_file(p))

        time_str = time.strftime("%H:%M", time.localtime(ts)) if ts else time.strftime("%H:%M")
        ctk.CTkLabel(bubble, text=time_str, font=ctk.CTkFont(size=10),
                     text_color=COL_TEXT_MUTED).pack(anchor="e", padx=10, pady=(0, 5))
        self._scroll_to_bottom()

    def _open_local_file(self, path):
        if os.path.exists(path):
            file_transfer.open_file_with_default_app(path)
        else:
            messagebox.showerror("Error", "This file no longer exists on this computer")

    def _send(self):
        text = self.entry.get().strip()
        if not text:
            return
        if self.is_group:
            self.app.nc.send_group_message(self.conv_id, text)
        else:
            self.app.nc.send_private_message(self.conv_id, text)
        self.entry.delete(0, "end")

    # ------------------------------------------------------------- files
    def _send_file(self):
        if self.is_group:
            messagebox.showinfo("Coming soon", "File transfer is currently only supported in private chats")
            return
        path = filedialog.askopenfilename()
        if not path:
            return
        self._send_file_path(path)

    def _send_file_path(self, path):
        """Sends a file already on disk (used by both the file button and voice recording)."""
        filename = os.path.basename(path)
        filesize = os.path.getsize(path)
        transfer_id = file_transfer.new_transfer_id()
        self.app.pending_outgoing_files[transfer_id] = (path, self.conv_id)
        self.app.transfer_chat_windows[transfer_id] = self
        self.app.nc.send(MsgType.FILE_OFFER, {
            "to": self.conv_id, "filename": filename, "filesize": filesize,
            "transfer_id": transfer_id,
        })
        self.append_system(f"📎 Sending file: {filename} ({filesize} bytes)...")

    def _start_call(self):
        self.app.start_call(self.conv_id)

    # -------------------------------------------------------- voice messages
    def _toggle_recording(self):
        if not self._is_recording:
            try:
                from client.voice_recorder import VoiceRecorder
            except ImportError as e:
                messagebox.showerror("Error", f"Voice recording requires the sounddevice library: {e}")
                return
            try:
                self._recorder = VoiceRecorder()
                self._recorder.start()
            except Exception as e:
                messagebox.showerror("Recording error", f"Could not start recording (is there a microphone?): {e}")
                self._recorder = None
                return
            self._is_recording = True
            self.record_btn.configure(text="⏹ Stop & send", fg_color=COL_DANGER, hover_color="#7d2828")
            self.append_system("🎤 Recording voice message...")
        else:
            self._is_recording = False
            self.record_btn.configure(text="🎤 Record", fg_color="#3a4a52", hover_color="#2e3b42")
            try:
                path = self._recorder.stop_and_save()
            except Exception as e:
                messagebox.showerror("Recording error", str(e))
                return
            finally:
                self._recorder = None
            self.append_system("🎤 Recording finished - sending...")
            self._send_file_path(path)

    def _on_close(self):
        if self._is_recording and self._recorder:
            self._recorder.cancel()
        if self.is_group:
            self.app.group_chats.pop(self.conv_id, None)
        else:
            self.app.private_chats.pop(self.conv_id, None)
        self.destroy()


# =============================================================================
class CallWindow(ctk.CTkToplevel):
    """Video call display window (shows the incoming WebRTC video stream)."""

    def __init__(self, master, app, peer_username):
        super().__init__(master)
        self.app = app
        self.peer_username = peer_username
        self.title(f"Video call with {peer_username}")
        self.geometry("440x420")
        self.configure(fg_color=COL_BG_CHAT)

        header = ctk.CTkFrame(self, fg_color=COL_BG_HEADER, height=50, corner_radius=0)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        make_avatar(header, peer_username, COL_BG_HEADER, size=34).pack(side="left", padx=(12, 8), pady=8)
        ctk.CTkLabel(header, text=f"Calling {fix_rtl(peer_username)}...", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=COL_TEXT).pack(side="left", pady=8)

        self.video_label = tk.Label(self, text="Waiting for video stream...", bg=COL_BG_CHAT, fg=COL_TEXT_MUTED)
        self.video_label.pack(fill="both", expand=True, padx=10, pady=10)

        ctk.CTkButton(self, text="🔴 Hang up", fg_color=COL_DANGER, hover_color="#7d2828",
                      command=self._hangup).pack(pady=10)
        self.protocol("WM_DELETE_WINDOW", self._hangup)

    def show_frame(self, pil_image):
        photo = ImageTk.PhotoImage(pil_image)
        self.video_label.configure(image=photo, text="")
        self.video_label.image = photo  # keep a reference so it isn't garbage-collected

    def _hangup(self):
        self.app.hang_up_call()
        self.destroy()


# =============================================================================
class MainWindow(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color=COL_BG_SIDEBAR)
        self.app = app

        top = ctk.CTkFrame(self, fg_color=COL_BG_HEADER, height=60, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)
        make_avatar(top, app.username, COL_BG_HEADER, size=38).pack(side="left", padx=(14, 8), pady=11)
        ctk.CTkLabel(top, text=fix_rtl(app.username), font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=COL_TEXT).pack(side="left", pady=11)

        tabs = ctk.CTkTabview(self, fg_color=COL_BG_SIDEBAR, segmented_button_selected_color=COL_ACCENT,
                              segmented_button_selected_hover_color=COL_ACCENT_HOVER)
        tabs.pack(fill="both", expand=True, padx=8, pady=8)
        contacts_tab = tabs.add("Contacts")
        groups_tab = tabs.add("Groups")

        # ---------------- Contacts tab: dynamic search + paging ----------------
        search_row = ctk.CTkFrame(contacts_tab, fg_color="transparent")
        search_row.pack(fill="x", pady=(4, 2))
        self.search_entry = ctk.CTkEntry(search_row, placeholder_text="🔍 Search contacts...")
        self.search_entry.pack(fill="x")
        self.search_entry.bind("<KeyRelease>", lambda e: self._search(reset_page=True))

        ctk.CTkLabel(contacts_tab, text="Click a contact to start chatting",
                     font=ctk.CTkFont(size=11), text_color=COL_TEXT_MUTED).pack(anchor="w", pady=(2, 4))

        self.contacts_list = ctk.CTkScrollableFrame(contacts_tab, fg_color=COL_BG_SIDEBAR)
        self.contacts_list.pack(fill="both", expand=True)

        page_row = ctk.CTkFrame(contacts_tab, fg_color="transparent")
        page_row.pack(fill="x", pady=(4, 0))
        ctk.CTkButton(page_row, text="⟵ Prev", command=self._prev_page, width=100,
                      fg_color="#3a4a52", hover_color="#2e3b42").pack(side="left", padx=5)
        self.page_label = ctk.CTkLabel(page_row, text="Page 1", text_color=COL_TEXT_MUTED)
        self.page_label.pack(side="left", expand=True)
        ctk.CTkButton(page_row, text="Next ⟶", command=self._next_page, width=100,
                      fg_color="#3a4a52", hover_color="#2e3b42").pack(side="left", padx=5)

        # ---------------- Groups tab ----------------
        group_top = ctk.CTkFrame(groups_tab, fg_color="transparent")
        group_top.pack(fill="x", pady=(4, 4))
        ctk.CTkButton(group_top, text="+ New group", command=self._create_group_dialog,
                      fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER).pack(side="left")
        ctk.CTkButton(group_top, text="Refresh", command=self.app.nc.list_groups,
                      fg_color="#3a4a52", hover_color="#2e3b42").pack(side="left", padx=5)

        self.groups_list = ctk.CTkScrollableFrame(groups_tab, fg_color=COL_BG_SIDEBAR)
        self.groups_list.pack(fill="both", expand=True, pady=(4, 0))

        self._groups_data = []
        self._contacts_data = []
        self._page = 0

        self._search(reset_page=True)
        self.app.nc.list_groups()

    # ---------------------------------------------------------------- search
    def _search(self, reset_page=False):
        if reset_page:
            self._page = 0
        self.app.nc.search_users(self.search_entry.get().strip(), self._page)

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._search()

    def _next_page(self):
        self._page += 1
        self._search()

    def update_contacts(self, users, page, has_more):
        self._contacts_data = users
        for child in self.contacts_list.winfo_children():
            child.destroy()

        for u in users:
            row = ctk.CTkFrame(self.contacts_list, fg_color=COL_BG_SIDEBAR, corner_radius=8, height=56)
            row.pack(fill="x", pady=2)
            row.pack_propagate(False)

            make_avatar(row, u["username"], COL_BG_SIDEBAR, size=40).pack(side="left", padx=10, pady=8)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="both", expand=True, pady=8)
            ctk.CTkLabel(info, text=fix_rtl(u["username"]), font=ctk.CTkFont(size=14, weight="bold"),
                         text_color=COL_TEXT).pack(anchor="w")
            status_text = "Online" if u["online"] else "Offline"
            status_color = COL_ONLINE if u["online"] else COL_OFFLINE
            ctk.CTkLabel(info, text=status_text, font=ctk.CTkFont(size=11),
                         text_color=status_color).pack(anchor="w")

            handler = lambda e, name=u["username"]: self.app.open_private_chat(name)
            bind_click_recursive(row, handler)
            row.configure(cursor="hand2")

        self.page_label.configure(text=f"Page {page + 1}" + ("  (more available)" if has_more else ""))

    # ---------------------------------------------------------------- groups
    def update_groups(self, groups):
        self._groups_data = groups
        for child in self.groups_list.winfo_children():
            child.destroy()

        for g in groups:
            row = ctk.CTkFrame(self.groups_list, fg_color=COL_BG_SIDEBAR, corner_radius=8, height=56)
            row.pack(fill="x", pady=2)
            row.pack_propagate(False)

            make_avatar(row, g["name"], COL_BG_SIDEBAR, size=40).pack(side="left", padx=10, pady=8)
            ctk.CTkLabel(row, text=fix_rtl(g["name"]), font=ctk.CTkFont(size=14, weight="bold"),
                         text_color=COL_TEXT).pack(side="left", pady=8)

            handler = lambda e, group=g: self.app.open_group_chat(group["id"], group["name"])
            bind_click_recursive(row, handler)
            row.configure(cursor="hand2")

    def _create_group_dialog(self):
        dialog = ctk.CTkInputDialog(text="Group name:", title="New group")
        name = dialog.get_input()
        if not name:
            return
        dialog2 = ctk.CTkInputDialog(text="Usernames to invite (comma-separated):", title="Invite members")
        members_raw = dialog2.get_input() or ""
        members = [m.strip() for m in members_raw.split(",") if m.strip()]
        self.app.nc.create_group(name, members)


# =============================================================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("480x460")
        self.configure(fg_color=COL_BG_SIDEBAR)

        self.nc: NetworkClient | None = None
        self.username = None
        self.main_window: MainWindow | None = None
        self.private_chats = {}   # username -> ChatWindow
        self.group_chats = {}     # group_id -> ChatWindow
        self.pending_outgoing_files = {}  # transfer_id -> (local filepath, peer_username)
        self.transfer_chat_windows = {}   # transfer_id -> ChatWindow (for status updates)
        self.incoming_file_transfers = {}  # transfer_id -> {"path","handle","win","sender"}
        self.call_window: CallWindow | None = None
        self.webrtc = None
        self._frame_queue = queue.Queue()

        self.login_frame = LoginFrame(self, self._on_logged_in)
        self.login_frame.pack(fill="both", expand=True)

        self.after(50, self._poll_frame_queue)

    # ------------------------------------------------------------------
    def _on_logged_in(self, nc: NetworkClient, username: str):
        self.nc = nc
        self.username = username
        self.login_frame.pack_forget()

        self.geometry("680x720")
        self.main_window = MainWindow(self, self)
        self.main_window.pack(fill="both", expand=True)

        nc.on(MsgType.USER_SEARCH_RESULT, lambda p: self.after(0, self.main_window.update_contacts, p["users"], p["page"], p["has_more"]))
        nc.on(MsgType.GROUP_LIST_RESULT, lambda p: self.after(0, self.main_window.update_groups, p["groups"]))
        nc.on(MsgType.GROUP_CREATE_OK, lambda p: self.after(0, self._on_group_created, p))
        nc.on(MsgType.GROUP_CREATE_ERROR, lambda p: self.after(0, lambda: messagebox.showerror("Error creating group", p.get("message", "Unknown error"))))
        nc.on(MsgType.GROUP_INVITE, lambda p: self.after(0, self._on_group_invite, p))
        nc.on(MsgType.PRESENCE_UPDATE, lambda p: self.after(0, self.main_window._search))

        nc.on(MsgType.PRIVATE_MESSAGE, lambda p: self.after(0, self._on_private_message, p))
        nc.on(MsgType.PRIVATE_HISTORY_RESULT, lambda p: self.after(0, self._on_private_history, p))
        nc.on(MsgType.GROUP_MESSAGE, lambda p: self.after(0, self._on_group_message, p))
        nc.on(MsgType.GROUP_HISTORY_RESULT, lambda p: self.after(0, self._on_group_history, p))

        nc.on(MsgType.FILE_OFFER, lambda p: self.after(0, self._on_file_offer, p))
        nc.on(MsgType.FILE_ACCEPT, lambda p: self.after(0, self._on_file_accept, p))
        nc.on(MsgType.FILE_DECLINE, lambda p: self.after(0, self._on_file_decline, p))
        nc.on(MsgType.FILE_CHUNK, lambda p: self.after(0, self._on_file_chunk, p))

        nc.on(MsgType.CALL_OFFER, lambda p: self.after(0, self._on_call_offer, p))
        nc.on(MsgType.CALL_ANSWER, lambda p: self.after(0, self._on_call_answer, p))
        nc.on(MsgType.CALL_ICE, lambda p: self.after(0, self._on_call_ice, p))
        nc.on(MsgType.CALL_HANGUP, lambda p: self.after(0, self._on_call_hangup, p))
        nc.on(MsgType.CALL_REJECT, lambda p: self.after(0, self._on_call_reject, p))

        nc.on("__disconnected__", lambda p: self.after(0, self._on_disconnected))
        nc.on(MsgType.ERROR, lambda p: self.after(0, lambda: messagebox.showwarning("Server message", p.get("message", ""))))

    def _on_disconnected(self):
        messagebox.showerror("Disconnected", "The connection to the server was lost.")

    # ------------------------------------------------------------ chat windows
    def open_private_chat(self, username):
        if username == self.username:
            return
        win = self.private_chats.get(username)
        if win is None or not win.winfo_exists():
            win = ChatWindow(self, self, username, is_group=False, conv_id=username)
            self.private_chats[username] = win
        win.focus()

    def open_group_chat(self, group_id, name):
        win = self.group_chats.get(group_id)
        if win is None or not win.winfo_exists():
            win = ChatWindow(self, self, name, is_group=True, conv_id=group_id)
            self.group_chats[group_id] = win
        win.focus()

    def _on_group_created(self, payload):
        self.nc.list_groups()
        messagebox.showinfo("Group created", f'Group "{payload["name"]}" was created successfully')

    def _on_group_invite(self, payload):
        self.nc.list_groups()
        messagebox.showinfo("Added to group", f'You were added to group "{payload.get("name")}"')

    def _on_private_message(self, payload):
        sender = payload["from"]
        other = payload.get("to", sender) if sender == self.username else sender
        win = self.private_chats.get(other)
        if win and win.winfo_exists():
            win.append_message(sender, payload["text"], payload.get("ts"))
        elif sender != self.username:
            self.open_private_chat(sender)
            self.private_chats[sender].append_message(sender, payload["text"], payload.get("ts"))

    def _on_private_history(self, payload):
        other = payload["with_user"]
        win = self.private_chats.get(other)
        if win and win.winfo_exists():
            for m in payload["messages"]:
                win.append_message(m["from"], m["text"], m.get("ts"))

    def _on_group_message(self, payload):
        win = self.group_chats.get(payload["group_id"])
        if win and win.winfo_exists():
            win.append_message(payload["from"], payload["text"], payload.get("ts"))

    def _on_group_history(self, payload):
        win = self.group_chats.get(payload["group_id"])
        if win and win.winfo_exists():
            for m in payload["messages"]:
                win.append_message(m["from"], m["text"], m.get("ts"))

    # ---------------------------------------------------------------- files
    # Files are relayed through the server (not direct P2P) - this works
    # regardless of whether the two clients are on reachable subnets.
    def _on_file_offer(self, payload):
        sender = payload["from"]
        transfer_id = payload.get("transfer_id")
        filename = os.path.basename(payload["filename"])
        self.open_private_chat(sender)
        win = self.private_chats.get(sender)
        if win and win.winfo_exists():
            win.append_system(f'📥 Receiving file from {sender}: {filename} ({payload["filesize"]} bytes)...')

        # No approval prompt - files are auto-accepted, just like a regular
        # chat message (this matches how file sending works in WhatsApp).
        dest_path = file_transfer._unique_path(os.path.join(file_transfer.DOWNLOADS_DIR, filename))
        self.incoming_file_transfers[transfer_id] = {
            "path": dest_path, "handle": open(dest_path, "wb"),
            "win": win, "sender": sender,
        }
        self.nc.send(MsgType.FILE_ACCEPT, {"to": sender, "transfer_id": transfer_id})

    def _on_file_accept(self, payload):
        transfer_id = payload.get("transfer_id")
        pending = self.pending_outgoing_files.pop(transfer_id, None)
        win = self.transfer_chat_windows.pop(transfer_id, None)
        if not pending:
            return
        path, peer = pending

        if win and win.winfo_exists():
            win.append_system("✅ Approved - sending the file now...")

        def worker():
            try:
                chunk_size = 196608  # 192KB raw before Base64 encoding
                total_size = os.path.getsize(path)
                sent = 0
                seq = 0
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        sent += len(chunk)
                        is_last = sent >= total_size
                        self.nc.send(MsgType.FILE_CHUNK, {
                            "to": peer, "transfer_id": transfer_id, "seq": seq,
                            "data_b64": base64.b64encode(chunk).decode("ascii"),
                            "is_last": is_last,
                        })
                        seq += 1
                if win and win.winfo_exists():
                    self.after(0, win.append_file_link, "You sent:", os.path.basename(path), path)
            except OSError as e:
                print(f"[FILE] Error sending file: {e}")
                if win and win.winfo_exists():
                    self.after(0, win.append_system, f"❌ Sending the file failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_file_decline(self, payload):
        transfer_id = payload.get("transfer_id")
        self.pending_outgoing_files.pop(transfer_id, None)
        win = self.transfer_chat_windows.pop(transfer_id, None)
        reason = payload.get("message")
        text = reason if reason else f'{payload.get("from")} declined the file'
        if win and win.winfo_exists():
            win.append_system(f'🚫 {text}')
        messagebox.showinfo("File declined", text)

    def _on_file_chunk(self, payload):
        transfer_id = payload.get("transfer_id")
        info = self.incoming_file_transfers.get(transfer_id)
        if not info:
            return
        try:
            data = base64.b64decode(payload["data_b64"])
            info["handle"].write(data)
            if payload.get("is_last"):
                info["handle"].close()
                path = info["path"]
                win = info["win"]
                sender = info["sender"]
                del self.incoming_file_transfers[transfer_id]
                if win and win.winfo_exists():
                    win.append_file_link(f"{sender} sent:", os.path.basename(path), path)
                file_transfer.open_file_with_default_app(path)
        except Exception as e:
            print(f"[FILE] Error receiving chunk: {e}")
            info["handle"].close()
            del self.incoming_file_transfers[transfer_id]

    # -------------------------------------------------------------- calls
    def start_call(self, to_username):
        try:
            from client.webrtc_manager import WebRTCManager
        except ImportError as e:
            messagebox.showerror("Error", f"Could not start video call - missing library: {e}")
            return
        self.webrtc = WebRTCManager(
            send_signal_callback=self._send_call_signal,
            on_remote_frame=self._on_remote_frame,
            on_call_ended=lambda: self.after(0, self._close_call_window),
        )
        self.call_window = CallWindow(self, self, to_username)
        self.webrtc.start_call(to_username)

    def _send_call_signal(self, msg_type_str, payload):
        mapping = {
            "call_offer": MsgType.CALL_OFFER, "call_answer": MsgType.CALL_ANSWER,
            "call_ice": MsgType.CALL_ICE, "call_hangup": MsgType.CALL_HANGUP,
        }
        self.nc.send(mapping[msg_type_str], payload)

    def _on_call_offer(self, payload):
        sender = payload["from"]
        answer = messagebox.askyesno("Incoming call", f"{sender} is calling you. Answer?")
        if not answer:
            self.nc.send(MsgType.CALL_REJECT, {"to": sender})
            return
        try:
            from client.webrtc_manager import WebRTCManager
        except ImportError as e:
            messagebox.showerror("Error", f"Could not answer the call - missing library: {e}")
            return
        self.webrtc = WebRTCManager(
            send_signal_callback=self._send_call_signal,
            on_remote_frame=self._on_remote_frame,
            on_call_ended=lambda: self.after(0, self._close_call_window),
        )
        self.call_window = CallWindow(self, self, sender)
        self.webrtc.accept_call(sender, payload["sdp"], payload["sdp_type"])

    def _on_call_answer(self, payload):
        if self.webrtc:
            self.webrtc.handle_remote_answer(payload["sdp"], payload["sdp_type"])

    def _on_call_ice(self, payload):
        if self.webrtc:
            self.webrtc.handle_remote_ice(payload.get("candidate"))

    def _on_call_hangup(self, payload):
        self._close_call_window()
        messagebox.showinfo("Call ended", f'{payload.get("from")} hung up')

    def _on_call_reject(self, payload):
        self._close_call_window()
        reason = payload.get("message")
        text = reason if reason else f'{payload.get("from")} rejected the call'
        messagebox.showinfo("Call not connected", text)

    def _on_remote_frame(self, bgr_frame):
        # Runs on the aiortc/asyncio thread - only push to the queue here,
        # never touch Tkinter widgets directly from this thread.
        self._frame_queue.put(bgr_frame)

    def _poll_frame_queue(self):
        try:
            while True:
                frame = self._frame_queue.get_nowait()
                if self.call_window and self.call_window.winfo_exists():
                    import cv2
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(rgb).resize((400, 300))
                    self.call_window.show_frame(pil_img)
        except queue.Empty:
            pass
        self.after(50, self._poll_frame_queue)

    def _close_call_window(self):
        if self.webrtc:
            self.webrtc.hang_up()
            self.webrtc = None
        if self.call_window and self.call_window.winfo_exists():
            self.call_window.destroy()
        self.call_window = None

    def hang_up_call(self):
        if self.webrtc and self.call_window:
            peer = self.call_window.peer_username
            self.nc.send(MsgType.CALL_HANGUP, {"to": peer})
        self._close_call_window()


def run():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run()