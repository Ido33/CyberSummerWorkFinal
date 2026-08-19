# -*- coding: utf-8 -*-
"""
shared/discovery.py
====================
מימוש "זיהוי אוטומטי של כתובת השרת בעזרת העיקרון של פרוטוקול DHCP", כפי
שנדרש במסמך: הלקוח לא צריך לדעת מראש את ה-IP של השרת - הוא "צועק" בשידור
(Broadcast) ברשת המקומית "מי השרת?", וכל שרת ששומע עונה עם כתובת ה-IP וה-
Port שלו (5555, קבוע). זהו בדיוק העיקרון של DHCP: בקשת Broadcast, תשובת
Unicast מהשרת/ים שענו.

הפרוטוקול רץ מעל UDP (ולא TCP) כי Broadcast אפשרי רק ב-UDP, ומשתמש בפורט
נפרד (DISCOVERY_PORT) כדי לא להתנגש עם ערוץ ה-TCP הרגיל (5555).
"""

import socket
import time

DISCOVERY_PORT = 5556
DISCOVERY_REQUEST = b"CYBERCOMM_DISCOVER_REQUEST"
DISCOVERY_REPLY_PREFIX = b"CYBERCOMM_DISCOVER_REPLY:"  # + tcp_port כטקסט


def start_discovery_responder(tcp_port: int):
    """
    להרצה בתהליכון (thread) נפרד בתוך השרת: מאזין ל-Broadcast מהלקוחות
    ועונה עם כתובת/פורט ה-TCP שלו. רץ לנצח (daemon thread).
    """
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_sock.bind(("0.0.0.0", DISCOVERY_PORT))
    print(f"[DISCOVERY] מאזין לבקשות Broadcast על פורט UDP {DISCOVERY_PORT}")
    while True:
        try:
            data, addr = udp_sock.recvfrom(1024)
            if data == DISCOVERY_REQUEST:
                reply = DISCOVERY_REPLY_PREFIX + str(tcp_port).encode()
                udp_sock.sendto(reply, addr)
        except OSError:
            break


def discover_server(timeout: float = 2.0, broadcast_addr: str = "255.255.255.255"):
    """
    נשלח מהלקוח: משדר בקשת Discovery ברשת המקומית וממתין לתשובה הראשונה
    שמגיעה. מחזיר (server_ip, tcp_port) או None אם לא נמצא שרת בזמן שהוקצב.
    """
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    udp_sock.settimeout(timeout)
    try:
        udp_sock.sendto(DISCOVERY_REQUEST, (broadcast_addr, DISCOVERY_PORT))
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = udp_sock.recvfrom(1024)
            except socket.timeout:
                break
            if data.startswith(DISCOVERY_REPLY_PREFIX):
                port = int(data[len(DISCOVERY_REPLY_PREFIX):].decode())
                return addr[0], port
        return None
    finally:
        udp_sock.close()