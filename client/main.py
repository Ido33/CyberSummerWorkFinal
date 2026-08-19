# -*- coding: utf-8 -*-
"""
client/main.py
===============
נקודת הכניסה להרצת אפליקציית הלקוח (GUI).
הרצה: python -m client.main   (מהתיקייה הראשית של הפרויקט)
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client.gui_app import run

if __name__ == "__main__":
    run()