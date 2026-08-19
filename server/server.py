# -*- coding: utf-8 -*-
"""
server/server.py
=================
השרת המרכזי של Local-CyberComm.

ארכיטקטורה: Multi-threaded (thread לכל לקוח מחובר) - עונה על הדרישה לתמוך
בחיבור בו-זמני של הרבה משתמשים מאותה הרשת.

השרת תמיד מאזין על 0.0.0.0:5555 (כמצוין בדרישות - "השרת מאזין על פורט 5555").
הרצה על 0.0.0.0 מאפשרת חיבור הן מ-127.0.0.1 (לבדיקות מקומיות) והן מכל
מחשב אחר ברשת ה-LAN לפי כתובת ה-IP הפרטית של השרת (לדוגמה 192.168.1.50).

השרת אחראי אך ורק על ה"ערוץ הבקרה והניהול" (Control Plane):
  - הרשמה / התחברות
  - חיפוש ודפדוף במשתמשים
  - ניהול קבוצות
  - העברת (Relay) הודעות צ'אט
  - Signaling עבור העברת קבצים (השרת עצמו לא מעביר את תוכן הקובץ!)
  - Signaling עבור שיחות WebRTC (SDP/ICE) - גם כאן השרת רק "מתווך" טקסט,
    זרם המדיה בפועל עובר Peer-to-Peer ישירות בין הלקוחות.
"""

import socket
import threading
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.protocol import (
    send_message, recv_message, ConnectionClosed, MsgType, DEFAULT_PORT
)
from shared.discovery import start_discovery_responder
from server.database import Database

HOST = "0.0.0.0"
PORT = DEFAULT_PORT


class ClientSession:
    """מייצג לקוח מחובר ומאומת (אחרי login מוצלח)."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.username = None
        self.send_lock = threading.Lock()

    def send(self, msg_type, payload=None):
        try:
            with self.send_lock:
                send_message(self.sock, msg_type, payload)
        except OSError:
            pass


class CyberCommServer:
    def __init__(self, host=HOST, port=PORT):
        self.host = host
        self.port = port
        self.db = Database()
        self.clients_lock = threading.Lock()
        self.clients: dict[str, ClientSession] = {}  # username -> session (רק משתמשים מחוברים)

    # ------------------------------------------------------------------
    def start(self):
        # thread נפרד שעונה לבקשות "מי השרת?" (עקרון ה-DHCP הנדרש)
        threading.Thread(target=start_discovery_responder, args=(self.port,), daemon=True).start()

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((self.host, self.port))
        server_sock.listen(100)
        print(f"[SERVER] Local-CyberComm listening on {self.host}:{self.port} ...")
        try:
            while True:
                client_sock, addr = server_sock.accept()
                thread = threading.Thread(
                    target=self._handle_client, args=(client_sock, addr), daemon=True
                )
                thread.start()
        except KeyboardInterrupt:
            print("\n[SERVER] Shutting down.")
        finally:
            server_sock.close()

    # ------------------------------------------------------------------
    def _broadcast_presence(self, username, online):
        with self.clients_lock:
            sessions = list(self.clients.values())
        for s in sessions:
            if s.username != username:
                s.send(MsgType.PRESENCE_UPDATE, {"username": username, "online": online})

    def _handle_client(self, sock, addr):
        session = ClientSession(sock, addr)
        print(f"[SERVER] New connection from {addr}")
        try:
            while True:
                msg_type, payload = recv_message(sock)
                self._dispatch(session, msg_type, payload)
        except ConnectionClosed:
            pass
        except Exception as e:
            print(f"[SERVER] Error handling client {addr}: {e}")
        finally:
            if session.username:
                with self.clients_lock:
                    self.clients.pop(session.username, None)
                self._broadcast_presence(session.username, online=False)
                print(f"[SERVER] {session.username} disconnected")
            sock.close()

    # ------------------------------------------------------------------
    def _dispatch(self, session: ClientSession, msg_type, payload):
        handler_name = f"_on_{msg_type}"
        handler = getattr(self, handler_name, None)
        if handler is None:
            session.send(MsgType.ERROR, {"message": f"Unknown message type: {msg_type}"})
            return
        handler(session, payload)

    # ------------------------------------------------------------------ auth
    def _on_register(self, session, payload):
        username = payload.get("username", "").strip()
        password = payload.get("password", "")
        if not username or not password:
            session.send(MsgType.REGISTER_ERROR, {"message": "Please fill in a username and password"})
            return
        ok = self.db.create_user(username, password)
        if ok:
            session.send(MsgType.REGISTER_OK, {"message": "Registered successfully, you can now sign in"})
        else:
            session.send(MsgType.REGISTER_ERROR, {"message": "That username is already taken"})

    def _on_login(self, session, payload):
        username = payload.get("username", "").strip()
        password = payload.get("password", "")
        row = self.db.verify_user(username, password)
        if row is None:
            session.send(MsgType.LOGIN_ERROR, {"message": "Incorrect username or password"})
            return
        with self.clients_lock:
            if username in self.clients:
                session.send(MsgType.LOGIN_ERROR, {"message": "This user is already logged in elsewhere"})
                return
            session.username = username
            self.clients[username] = session
        session.send(MsgType.LOGIN_OK, {"username": username})
        self._broadcast_presence(username, online=True)
        print(f"[SERVER] {username} signed in successfully")

    def _on_logout(self, session, payload):
        if session.username:
            with self.clients_lock:
                self.clients.pop(session.username, None)
            self._broadcast_presence(session.username, online=False)
            session.username = None

    # ------------------------------------------------------------- search
    def _on_user_search(self, session, payload):
        query = payload.get("query", "")
        page = int(payload.get("page", 0))
        usernames, has_more = self.db.search_users(query, page)
        # לא מחזירים למשתמש את עצמו ברשימת אנשי הקשר
        usernames = [u for u in usernames if u != session.username]
        with self.clients_lock:
            online_now = set(self.clients.keys())
        users = [{"username": u, "online": u in online_now} for u in usernames]
        session.send(MsgType.USER_SEARCH_RESULT, {"users": users, "page": page, "has_more": has_more})

    # -------------------------------------------------------------- groups
    def _require_auth(self, session) -> bool:
        if session.username is None:
            session.send(MsgType.ERROR, {"message": "Please sign in first"})
            return False
        return True

    def _on_group_create(self, session, payload):
        if not self._require_auth(session):
            return
        name = payload.get("name", "").strip()
        members = payload.get("members", [])
        if not name:
            session.send(MsgType.GROUP_CREATE_ERROR, {"message": "Please enter a group name"})
            return
        group_id = self.db.create_group(name, session.username, members)
        session.send(MsgType.GROUP_CREATE_OK, {"group_id": group_id, "name": name})
        # מודיעים לכל חבר שהוזמן, אם הוא online
        for uname in members:
            self._notify_group_invite(uname, group_id, name)

    def _notify_group_invite(self, username, group_id, name):
        with self.clients_lock:
            target = self.clients.get(username)
        if target:
            target.send(MsgType.GROUP_INVITE, {"group_id": group_id, "name": name})

    def _on_group_invite(self, session, payload):
        if not self._require_auth(session):
            return
        group_id = payload.get("group_id")
        username = payload.get("username")
        if self.db.add_group_member(group_id, username):
            self._notify_group_invite(username, group_id, payload.get("name", ""))

    def _on_group_list(self, session, payload):
        if not self._require_auth(session):
            return
        groups = self.db.get_user_groups(session.username)
        session.send(MsgType.GROUP_LIST_RESULT, {"groups": groups})

    def _on_group_history(self, session, payload):
        if not self._require_auth(session):
            return
        group_id = payload.get("group_id")
        history = self.db.get_group_history(group_id)
        session.send(MsgType.GROUP_HISTORY_RESULT, {"group_id": group_id, "messages": history})

    # ------------------------------------------------------------- chat
    def _on_group_message(self, session, payload):
        if not self._require_auth(session):
            return
        group_id = payload.get("group_id")
        text = payload.get("text", "")
        ts = self.db.save_group_message(group_id, session.username, text)
        members = self.db.get_group_members(group_id)
        with self.clients_lock:
            targets = [self.clients[m] for m in members if m in self.clients]
        for t in targets:
            t.send(MsgType.GROUP_MESSAGE, {
                "group_id": group_id, "from": session.username, "text": text, "ts": ts
            })

    def _on_private_message(self, session, payload):
        if not self._require_auth(session):
            return
        to_user = payload.get("to")
        text = payload.get("text", "")
        ts = self.db.save_private_message(session.username, to_user, text)
        with self.clients_lock:
            target = self.clients.get(to_user)
        if target:
            target.send(MsgType.PRIVATE_MESSAGE, {"from": session.username, "text": text, "ts": ts})
        # אישור חוזר לשולח (כדי שה-GUI שלו יעדכן את חלון הצ'אט)
        session.send(MsgType.PRIVATE_MESSAGE, {"from": session.username, "to": to_user, "text": text, "ts": ts})

    def _on_private_history(self, session, payload):
        if not self._require_auth(session):
            return
        other = payload.get("with_user")
        history = self.db.get_private_history(session.username, other)
        session.send(MsgType.PRIVATE_HISTORY_RESULT, {"with_user": other, "messages": history})

    # ------------------------------------------------------- file transfer
    # השרת רק מעביר (Relay) הודעות טקסט קטנות בין הצדדים כדי לסכם היכן
    # ה-receiver יאזין; תוכן הקובץ בפועל זורם ישירות מחשב-למחשב (P2P).
    def _relay_to(self, target_username, msg_type, payload):
        with self.clients_lock:
            target = self.clients.get(target_username)
        if target:
            target.send(msg_type, payload)
            return True
        return False

    def _on_file_offer(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        delivered = self._relay_to(payload.get("to"), MsgType.FILE_OFFER, payload)
        if not delivered:
            # שולחים FILE_DECLINE (ולא ERROR כללי) כדי שהלקוח ינקה בפועל
            # את מצב ה"ממתין לאישור" הפנימי, ולא ישאר תקוע.
            session.send(MsgType.FILE_DECLINE, {
                "from": payload.get("to"),
                "transfer_id": payload.get("transfer_id"),
                "message": f'{payload.get("to")} is not online right now',
            })

    def _on_file_accept(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        self._relay_to(payload.get("to"), MsgType.FILE_ACCEPT, payload)

    def _on_file_decline(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        self._relay_to(payload.get("to"), MsgType.FILE_DECLINE, payload)

    def _on_file_chunk(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        self._relay_to(payload.get("to"), MsgType.FILE_CHUNK, payload)

    # ------------------------------------------------------- webrtc calls
    def _on_call_offer(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        delivered = self._relay_to(payload.get("to"), MsgType.CALL_OFFER, payload)
        if not delivered:
            # שולחים CALL_REJECT (ולא ERROR כללי) כדי שהלקוח יסגור בפועל
            # את חלון "מחייג..." שכבר נפתח - ולא ישאיר אותו תקוע לנצח.
            session.send(MsgType.CALL_REJECT, {
                "from": payload.get("to"),
                "message": f'{payload.get("to")} is not online right now',
            })

    def _on_call_answer(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        self._relay_to(payload.get("to"), MsgType.CALL_ANSWER, payload)

    def _on_call_ice(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        self._relay_to(payload.get("to"), MsgType.CALL_ICE, payload)

    def _on_call_hangup(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        self._relay_to(payload.get("to"), MsgType.CALL_HANGUP, payload)

    def _on_call_reject(self, session, payload):
        if not self._require_auth(session):
            return
        payload = dict(payload)
        payload["from"] = session.username
        self._relay_to(payload.get("to"), MsgType.CALL_REJECT, payload)


if __name__ == "__main__":
    CyberCommServer().start()
