# -*- coding: utf-8 -*-
"""
shared/protocol.py
===================
פרוטוקול האפליקציה המשותף לשרת וללקוח.

מבנה הודעה על גבי הרשת:

    [ 4 בתים - אורך הגוף (Big Endian, unsigned int) ][ גוף ההודעה - JSON מקודד ב-UTF-8 ]

גוף ההודעה הוא תמיד אובייקט JSON בצורה:
    {
        "type":    "<message_type>",   # מחרוזת שמזהה את סוג ההודעה
        "payload": { ... }              # מילון עם הנתונים הרלוונטיים לאותו סוג הודעה
    }

כל הקוד (גם בשרת וגם בלקוח) חייב לייבא מהמודול הזה ולא לשכפל את הלוגיקה,
כדי שלא ניצור חוסר-התאמה (mismatch) בין הצדדים.
"""

import json
import struct

HEADER_LEN = 4          # 4 בתים לאורך ההודעה
ENCODING = "utf-8"
DEFAULT_PORT = 5555      # השרת תמיד מאזין על פורט זה (כמצוין בדרישות)


class ConnectionClosed(Exception):
    """נזרקת כאשר הצד השני סגר את הסוקט."""
    pass


def encode_message(msg_type: str, payload: dict | None = None) -> bytes:
    """הופך סוג הודעה + payload למחרוזת בתים מוכנה לשליחה ברשת."""
    payload = payload if payload is not None else {}
    obj = {"type": msg_type, "payload": payload}
    body = json.dumps(obj, ensure_ascii=False).encode(ENCODING)
    header = struct.pack("!I", len(body))
    return header + body


def send_message(sock, msg_type: str, payload: dict | None = None) -> None:
    """שולח הודעה מלאה (header + body) על גבי סוקט TCP."""
    sock.sendall(encode_message(msg_type, payload))


def _recv_exact(sock, n: int) -> bytes:
    """מקבל בדיוק n בתים מהסוקט, גם אם ה-recv מחזיר בחלקים (TCP הוא stream!)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionClosed("הצד השני סגר את החיבור")
        buf.extend(chunk)
    return bytes(buf)


def recv_message(sock):
    """
    מקבל הודעה בודדת מהסוקט (חוסם עד שההודעה השלמה מגיעה).
    מחזיר טאפל: (msg_type, payload)
    זורק ConnectionClosed אם הצד השני התנתק.
    """
    header = _recv_exact(sock, HEADER_LEN)
    length = struct.unpack("!I", header)[0]
    body = _recv_exact(sock, length)
    obj = json.loads(body.decode(ENCODING))
    return obj.get("type"), obj.get("payload", {})


# ---------------------------------------------------------------------------
# קבועים לסוגי הודעות - כדי למנוע שגיאות הקלדה של מחרוזות בקוד
# ---------------------------------------------------------------------------

class MsgType:
    # רישום והתחברות
    REGISTER = "register"
    REGISTER_OK = "register_ok"
    REGISTER_ERROR = "register_error"

    LOGIN = "login"
    LOGIN_OK = "login_ok"
    LOGIN_ERROR = "login_error"

    LOGOUT = "logout"

    # רשימת משתמשים - חיפוש דינמי + דפדוף (Paging)
    USER_SEARCH = "user_search"          # {query, page}
    USER_SEARCH_RESULT = "user_search_result"  # {users: [...], page, has_more}

    # התראת נוכחות (מי online/offline) - עדכון לכל הלקוחות
    PRESENCE_UPDATE = "presence_update"  # {username, online}

    # קבוצות
    GROUP_CREATE = "group_create"        # {name, members: [usernames]}
    GROUP_CREATE_OK = "group_create_ok"
    GROUP_CREATE_ERROR = "group_create_error"
    GROUP_LIST = "group_list"            # בקשה
    GROUP_LIST_RESULT = "group_list_result"  # {groups: [{id, name}]}
    GROUP_INVITE = "group_invite"        # {group_id, username}
    GROUP_HISTORY = "group_history"      # {group_id} בקשה להיסטוריה
    GROUP_HISTORY_RESULT = "group_history_result"

    # הודעות צ'אט
    GROUP_MESSAGE = "group_message"      # {group_id, text}  (נשלח משרת ללקוחות: + from, ts)
    PRIVATE_MESSAGE = "private_message"  # {to, text} (נשלח משרת: + from, ts)
    PRIVATE_HISTORY = "private_history"  # {with_user} בקשה
    PRIVATE_HISTORY_RESULT = "private_history_result"

    # העברת קבצים - איתות (Signaling) בלבד; התוכן עצמו עובר ישירות P2P
    # העברת קבצים - עוברת Relay מלא דרך השרת (בדיוק כמו הודעות פרטיות),
    # ולא חיבור ישיר P2P - זה עובד גם כששני הלקוחות לא ברי-הגעה ישירה
    # זה מזה (לדוגמה כשהם על תת-רשתות/רשתות שונות, ומגיעים רק דרך השרת
    # המשותף). הקובץ מפוצל ל-chunks, כל אחד מקודד ב-Base64 ומועבר כהודעת
    # JSON רגילה - השרת רק מעביר הלאה בלי לגעת בתוכן (אין דיסק בשרת מעורב).
    FILE_OFFER = "file_offer"            # {to, filename, filesize, transfer_id}  -> משרת מעביר ל-target + from
    FILE_ACCEPT = "file_accept"          # {to, transfer_id} -> receiver מוכן, לשולח: תתחיל לשלוח chunks
    FILE_DECLINE = "file_decline"        # {to, transfer_id}
    FILE_CHUNK = "file_chunk"            # {to, transfer_id, seq, data_b64, is_last}

    # שיחות וידאו/אודיו - Signaling ל-WebRTC, מועבר דרך השרת בלבד (Relay טקסטואלי)
    CALL_OFFER = "call_offer"            # {to, sdp}
    CALL_ANSWER = "call_answer"          # {to, sdp}
    CALL_ICE = "call_ice"                # {to, candidate}
    CALL_HANGUP = "call_hangup"          # {to}
    CALL_REJECT = "call_reject"          # {to}

    ERROR = "error"                      # {message}