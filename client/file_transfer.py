# -*- coding: utf-8 -*-
"""
client/file_transfer.py
=========================
מודול העברת קבצים ישירה (Direct P2P), כפי שמופיע בדיאגרמת הארכיטקטורה
("DIRECT P2P FILE TRANSFER"). השרת המרכזי מעורב רק ב-Signaling (הודעות
file_offer / file_accept / file_decline דרך shared.protocol) - תוכן
הקובץ עצמו **לא** עובר דרך השרת, אלא ישירות בין שני הלקוחות.

זרימה:
  1. השולח (Sender) שולח FILE_OFFER לשרת -> מגיע ליעד (Receiver) עם from/filename/filesize.
  2. ה-Receiver (אם המשתמש מאשר בממשק) פותח socket מאזין על פורט אקראי פנוי,
     ושולח FILE_ACCEPT חזרה עם ה-IP/Port שלו + transfer_id.
  3. השולח מתחבר ישירות (Socket TCP רגיל) ל-IP/Port של ה-Receiver וזורם את
     הקובץ (header קטן עם שם+גודל, ואז הבתים עצמם).
  4. ה-Receiver שומר את הקובץ בתיקיית downloads ופותח אותו אוטומטית
     באמצעות אפליקציית ברירת המחדל של מערכת ההפעלה (os.startfile ב-Windows,
     או subprocess.Popen(["open"/"xdg-open", ...]) בשאר המערכות).
"""

import os
import socket
import struct
import subprocess
import sys
import threading
import uuid

DOWNLOADS_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

_HEADER_FMT = "!I"  # 4 bytes = אורך שם הקובץ, ואז 8 בתים לגודל התוכן


def get_local_ip(target_ip: str = "8.8.8.8") -> str:
    """
    מגלה את ה-IP המקומי (ברשת ה-LAN) של המחשב הזה.
    חשוב: העברת target_ip = כתובת השרת (שאנחנו כבר יודעים בוודאות שהיא
    ברשת המקומית שלנו) עדיפה מאוד על פני הניחוש הכללי ל-8.8.8.8 - אם יש
    במחשב VPN/כרטיס וירטואלי (Docker/Hyper-V/VMware) פעיל, הניחוש הכללי
    עלול "לבחור" בכרטיס הלא נכון ולהחזיר IP שלא נגיש בכלל לצד השני.

    הגנה: אם target_ip הוא loopback (127.x.x.x) - למשל אם הלקוח הזה
    התחבר לשרת דרך 127.0.0.1 (כי הוא רץ על אותו מחשב כמו השרת) - השימוש
    בו כיעד יחזיר IP חסר תועלת (127.0.0.1) שלא נגיש בכלל לצד השני ברשת.
    במקרה כזה נופלים חזרה לניחוש הכללי דרך 8.8.8.8.
    """
    if not target_ip or target_ip.startswith("127."):
        target_ip = "8.8.8.8"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def new_transfer_id() -> str:
    return uuid.uuid4().hex[:12]


def open_file_with_default_app(path: str):
    """פותח קובץ עם אפליקציית ברירת המחדל של מערכת ההפעלה."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: type: ignore  (קיים רק ב-Windows)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"[FILE_TRANSFER] לא ניתן לפתוח את הקובץ אוטומטית: {e}")


def send_file(host: str, port: int, filepath: str, on_progress=None, connect_timeout=10.0):
    """
    מתחבר ישירות ל-Receiver ושולח את הקובץ. רץ בדרך כלל בתוך thread נפרד
    כדי לא לחסום את ה-GUI.
    """
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    name_bytes = filename.encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout)
    sock.connect((host, port))
    sock.settimeout(None)
    try:
        header = struct.pack("!I Q", len(name_bytes), filesize)
        sock.sendall(header + name_bytes)
        sent = 0
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                sock.sendall(chunk)
                sent += len(chunk)
                if on_progress:
                    on_progress(sent, filesize)
    finally:
        sock.close()


FILE_PORT_RANGE_START = 5557
FILE_PORT_RANGE_END = 5567


def start_receiver(save_dir: str = DOWNLOADS_DIR, on_complete=None, on_progress=None, on_fail=None):
    """
    פותח socket מאזין על פורט קבוע מתוך טווח ידוע מראש (5557-5567) - כדי
    שאפשר יהיה להגדיר כלל Firewall אחד וקבוע שיעבוד תמיד, במקום פורט
    אקראי חדש בכל פעם. אם כל הפורטים בטווח תפוסים (למשל כמה העברות
    קבצים בו-זמנית), נופלים חזרה לפורט אקראי שה-OS יקצה.
    מחזיר (port, thread). ברגע שמגיע חיבור אחד - מקבל את הקובץ ואז נסגר.
    on_complete(filepath) נקרא כאשר הקובץ נשמר בהצלחה (מה-thread של הרשת!).
    on_fail(reason: str) נקרא אם אין חיבור בזמן, או אם ההעברה נכשלת/נקטעת.
    """
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    port = None
    for candidate in range(FILE_PORT_RANGE_START, FILE_PORT_RANGE_END + 1):
        try:
            listen_sock.bind(("0.0.0.0", candidate))
            port = candidate
            break
        except OSError:
            continue
    if port is None:
        listen_sock.bind(("0.0.0.0", 0))  # כל הפורטים הקבועים תפוסים - נופלים לפורט אקראי
        port = listen_sock.getsockname()[1]

    listen_sock.listen(1)

    def _accept_and_receive():
        try:
            listen_sock.settimeout(60)  # דקה להמתין לחיבור השולח
            conn, _addr = listen_sock.accept()
        except socket.timeout:
            listen_sock.close()
            if on_fail:
                on_fail("השולח לא התחבר בזמן (יתכן שהחיבור נחסם ע\"י חומת אש)")
            return
        try:
            header = _recv_exact(conn, 12)  # 4 bytes name_len + 8 bytes filesize
            name_len, filesize = struct.unpack("!I Q", header)
            filename = _recv_exact(conn, name_len).decode("utf-8")
            filename = os.path.basename(filename)  # הגנה מפני path traversal
            dest_path = _unique_path(os.path.join(save_dir, filename))

            received = 0
            with open(dest_path, "wb") as f:
                while received < filesize:
                    chunk = conn.recv(min(65536, filesize - received))
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if on_progress:
                        on_progress(received, filesize)

            if received == filesize:
                if on_complete:
                    on_complete(dest_path)
            elif on_fail:
                on_fail(f"החיבור נקטע באמצע ההעברה ({received}/{filesize} בתים התקבלו)")
        except (ConnectionError, OSError) as e:
            if on_fail:
                on_fail(str(e))
        finally:
            conn.close()
            listen_sock.close()

    t = threading.Thread(target=_accept_and_receive, daemon=True)
    t.start()
    return port, t


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("החיבור נסגר באמצע העברת הקובץ")
        buf.extend(chunk)
    return bytes(buf)


def _unique_path(path: str) -> str:
    """אם קובץ בשם הזה כבר קיים, מוסיף (1), (2) וכו' כדי לא לדרוס."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"