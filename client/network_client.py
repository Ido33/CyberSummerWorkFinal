# -*- coding: utf-8 -*-
"""
client/network_client.py
=========================
אחראי על ערוץ הבקרה מול השרת (Control Plane): פתיחת socket, thread שמאזין
ברקע להודעות נכנסות, ושליחת הודעות יוצאות. ה-GUI לא נוגע ב-socket ישירות -
הוא רק רושם callbacks (listeners) לכל סוג הודעה, וה-network_client קורא
להם כשמגיעה הודעה מתאימה. כך ה-thread של הרשת לא "תקוע" בתוך ה-GUI, וה-GUI
לא חוסם את הרשת.
"""

import socket
import threading
from collections import defaultdict

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.protocol import send_message, recv_message, ConnectionClosed, MsgType, DEFAULT_PORT


class NetworkClient:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.username = None
        self.server_ip = None
        self._send_lock = threading.Lock()
        self._listeners = defaultdict(list)   # msg_type -> [callback(payload), ...]
        self._listen_thread = None

    # ------------------------------------------------------------------
    def on(self, msg_type, callback):
        """רושם callback שיופעל (מה-thread של הרשת!) כשמתקבלת הודעה מסוג msg_type."""
        self._listeners[msg_type].append(callback)

    def _fire(self, msg_type, payload):
        for cb in self._listeners.get(msg_type, []):
            try:
                cb(payload)
            except Exception as e:
                print(f"[CLIENT] שגיאה ב-listener עבור {msg_type}: {e}")

    # ------------------------------------------------------------------
    def connect(self, server_ip: str, server_port: int = DEFAULT_PORT, timeout=5.0):
        """
        זיהוי/חיבור לשרת: הלקוח מתחבר לכתובת ה-IP שהוזנה (או שהתגלתה אוטומטית
        ע"י discovery.py) על פורט 5555 הקבוע.
        """
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((server_ip, server_port))
        self.sock.settimeout(None)
        self.connected = True
        self.server_ip = server_ip
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listen_thread.start()

    def _listen_loop(self):
        try:
            while self.connected:
                msg_type, payload = recv_message(self.sock)
                if msg_type == MsgType.LOGIN_OK:
                    self.username = payload.get("username")
                self._fire(msg_type, payload)
        except ConnectionClosed:
            self._fire("__disconnected__", {})
        except OSError:
            self._fire("__disconnected__", {})
        finally:
            self.connected = False

    def send(self, msg_type, payload=None):
        if not self.connected:
            raise ConnectionError("Not connected to the server")
        with self._send_lock:
            send_message(self.sock, msg_type, payload or {})

    def close(self):
        self.connected = False
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    # --------------------------------------------------------- shortcuts
    def register(self, username, password):
        self.send(MsgType.REGISTER, {"username": username, "password": password})

    def login(self, username, password):
        self.send(MsgType.LOGIN, {"username": username, "password": password})

    def search_users(self, query="", page=0):
        self.send(MsgType.USER_SEARCH, {"query": query, "page": page})

    def create_group(self, name, members):
        self.send(MsgType.GROUP_CREATE, {"name": name, "members": members})

    def list_groups(self):
        self.send(MsgType.GROUP_LIST, {})

    def group_history(self, group_id):
        self.send(MsgType.GROUP_HISTORY, {"group_id": group_id})

    def send_group_message(self, group_id, text):
        self.send(MsgType.GROUP_MESSAGE, {"group_id": group_id, "text": text})

    def send_private_message(self, to_user, text):
        self.send(MsgType.PRIVATE_MESSAGE, {"to": to_user, "text": text})

    def private_history(self, with_user):
        self.send(MsgType.PRIVATE_HISTORY, {"with_user": with_user})