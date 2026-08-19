# -*- coding: utf-8 -*-
"""
client/voice_recorder.py
==========================
הקלטת הודעות קוליות (Voice Messages) למיקרופון המקומי.

מימוש פשוט מבוסס sounddevice: פותחים InputStream, כל callback מוסיף
פריים לרשימה בזיכרון, ובסיום ההקלטה שומרים הכל כקובץ WAV רגיל (מודול
wave של הפייתון הסטנדרטי - אין תלות בספריות קידוד נוספות).

קובץ ה-WAV שנוצר נשלח בדיוק כמו כל קובץ אחר - דרך אותו מנגנון של
client/file_transfer.py (Direct P2P) - כך שבצד המקבל הוא גם "נופל"
לתוך אותה זרימה (הצעה -> אישור -> קבלה -> פתיחה אוטומטית בנגן ברירת
המחדל של Windows).
"""

import os
import queue
import threading
import time
import wave

import numpy as np
import sounddevice as sd

VOICE_MESSAGES_DIR = os.path.join(os.path.dirname(__file__), "voice_messages")
os.makedirs(VOICE_MESSAGES_DIR, exist_ok=True)

SAMPLE_RATE = 44100
CHANNELS = 1


class VoiceRecorder:
    """מקליט הודעה קולית אחת. שימוש: start() ואז stop_and_save() כשמסיימים."""

    def __init__(self, samplerate=SAMPLE_RATE, channels=CHANNELS):
        self.samplerate = samplerate
        self.channels = channels
        self._frames = []
        self._stream = None
        self._recording = False

    def start(self):
        self._frames = []
        self._recording = True

        def callback(indata, frames, time_info, status):
            if self._recording:
                self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.samplerate, channels=self.channels,
            dtype="int16", callback=callback,
        )
        self._stream.start()

    def stop_and_save(self) -> str:
        """עוצר את ההקלטה ושומר כ-WAV. מחזיר את הנתיב לקובץ שנשמר."""
        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if not self._frames:
            raise RuntimeError("No audio was recorded (maybe no microphone is available)")

        audio = np.concatenate(self._frames, axis=0)
        filename = f"voice_{int(time.time())}.wav"
        path = os.path.join(VOICE_MESSAGES_DIR, filename)

        with wave.open(path, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(self.samplerate)
            wf.writeframes(audio.tobytes())

        return path

    def cancel(self):
        """מבטל הקלטה בלי לשמור קובץ."""
        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._frames = []


def play_wav(path: str):
    """מנגן קובץ WAV דרך רמקולי המחשב (חוסם - להריץ ב-thread נפרד)."""
    with wave.open(path, "rb") as wf:
        samplerate = wf.getframerate()
        n_channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16)
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels)
    sd.play(audio, samplerate)
    sd.wait()
