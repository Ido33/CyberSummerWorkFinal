# -*- coding: utf-8 -*-
"""
client/webrtc_manager.py
==========================
שיחות וידאו/אודיו ברשת המקומית באמצעות WebRTC (ספריית aiortc).

הפרוטוקול (SDP Offer/Answer + ICE Candidates) עובר Signaling דרך השרת
המרכזי (הודעות CALL_OFFER / CALL_ANSWER / CALL_ICE ב-shared.protocol),
בדיוק כפי שמתואר בדיאגרמת הארכיטקטורה. מכיוון שאנחנו ברשת מקומית (LAN)
אין צורך בשרתי STUN/TURN חיצוניים - ה-ICE candidates המקומיים (ה-IP
הפרטי) מספיקים כדי ליצור את חיבור ה-Peer-to-Peer.

לאחר סיום שלב ה-Signaling, זרם המדיה (וידאו+אודיו) עובר ישירות בין שני
המחשבים ברשת - ללא מעורבות השרת (Data Plane, הקווים הירוקים בדיאגרמה).

aiortc מבוסס asyncio, וה-GUI שלנו מבוסס Tkinter (סינכרוני) - לכן אנחנו
מריצים event-loop נפרד של asyncio ב-thread ברקע, ומתקשרים איתו דרך
call_soon_threadsafe / run_coroutine_threadsafe.
"""

import asyncio
import fractions
import queue
import sys
import threading
import time

import cv2
import numpy as np
import sounddevice as sd
from av import VideoFrame, AudioFrame
from av.audio.resampler import AudioResampler

from aiortc import (
    RTCPeerConnection, RTCSessionDescription, RTCIceCandidate,
    VideoStreamTrack, AudioStreamTrack,
)
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

AUDIO_SAMPLE_RATE = 48000  # קצב הדגימה הסטנדרטי ש-WebRTC/Opus עובדים איתו
AUDIO_CHANNELS = 1
AUDIO_FRAME_SAMPLES = 960  # 20ms בקצב 48kHz - גודל פריים סטנדרטי ב-WebRTC


class CameraVideoTrack(VideoStreamTrack):
    """קורא פריימים מהמצלמה המקומית (OpenCV) ומגיש אותם ל-aiortc."""

    def __init__(self, camera_index=0, fps=15, width=320, height=240):
        super().__init__()
        # ב-Windows, ה-Backend של Media Foundation (ברירת המחדל) לפעמים נכשל
        # שוב ושוב אם מצלמה כבר תפוסה ע"י תהליך אחר - DSHOW הרבה יותר יציב.
        if sys.platform.startswith("win"):
            self._cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        else:
            self._cap = cv2.VideoCapture(camera_index)
        # רזולוציה נמוכה = הרבה פחות עבודה ל-CPU (קידוד וידאו תוכנתי הוא כבד),
        # וזה מה שבעיקר גרם לדיליי/קטיעות - במיוחד יחד עם אודיו על אותו ליבה.
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._fps = fps
        self._time_base = fractions.Fraction(1, fps)
        self._frame_count = 0
        self._camera_ok = self._cap.isOpened()
        if not self._camera_ok:
            print("[WEBRTC] לא נמצאה מצלמה זמינה (אולי תפוסה ע\"י חלון/תהליך אחר) - "
                  "יישלח פריים שחור בלבד, בלי לנסות שוב בכל פריים.")

    async def recv(self):
        pts = self._frame_count
        self._frame_count += 1
        await asyncio.sleep(1 / self._fps)

        frame = None
        if self._camera_ok:
            # cap.read() חוסם (Blocking) - חובה להריץ אותו ב-thread נפרד
            # (executor) כדי לא לקפיא את כל ה-event loop (וידאו+אודיו+איתות)
            # בכל פעם שקוראים פריים.
            loop = asyncio.get_event_loop()
            ok, frame = await loop.run_in_executor(None, self._cap.read)
            if not ok:
                frame = None
        if frame is None:
            frame = np.zeros((240, 320, 3), dtype=np.uint8)
        else:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = self._time_base
        return video_frame

    def stop(self):
        super().stop()
        if self._cap:
            self._cap.release()


class MicrophoneAudioTrack(AudioStreamTrack):
    """קורא שמע מהמיקרופון המקומי (sounddevice) ומגיש ל-aiortc כפריימי PCM."""

    def __init__(self):
        super().__init__()
        # תור קטן בכוונה (עד 100ms) - כדי שדיליי לא יצטבר לאורך זמן: אם
        # מצטבר עומס, נזרוק פריימים ישנים ונעדיף תמיד שמע טרי (בדיוק כמו
        # בשידור חי - עדיף לדלג קדימה מאשר "להישאר תקוע בעבר").
        self._queue = queue.Queue(maxsize=5)
        self._timestamp = 0
        self._mic_ok = True
        try:
            self._stream = sd.InputStream(
                samplerate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS,
                dtype="int16", blocksize=AUDIO_FRAME_SAMPLES, callback=self._callback,
                latency="low",
            )
            self._stream.start()
        except Exception as e:
            print(f"[WEBRTC] לא נמצא מיקרופון זמין - השיחה תהיה בלי שמע יוצא: {e}")
            self._mic_ok = False
            self._stream = None

    def _callback(self, indata, frames, time_info, status):
        if self._queue.full():
            try:
                self._queue.get_nowait()  # זורקים את הישן ביותר כדי לפנות מקום לטרי
            except queue.Empty:
                pass
        try:
            self._queue.put_nowait(indata.copy())
        except queue.Full:
            pass

    async def recv(self):
        if self._mic_ok:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, self._queue.get)
        else:
            await asyncio.sleep(AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE)
            data = np.zeros((AUDIO_FRAME_SAMPLES, AUDIO_CHANNELS), dtype=np.int16)

        frame = AudioFrame.from_ndarray(data.T, format="s16", layout="mono")
        frame.sample_rate = AUDIO_SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, AUDIO_SAMPLE_RATE)
        self._timestamp += data.shape[0]
        return frame

    def stop(self):
        super().stop()
        if self._stream:
            self._stream.stop()
            self._stream.close()


class WebRTCManager:
    """
    מנהל שיחת WebRTC בודדת בכל פעם. אחראי על:
      - יצירת PeerConnection + מסלול וידאו מקומי
      - יצירת Offer / קבלת Answer, והחלפת ICE candidates
      - הפניית פריימי הווידאו הנכנסים ל-callback שמצייר אותם על ה-GUI
    כל התקשורת עם השרת (Signaling) מתבצעת ע"י ה-caller (gui_app.py) שמעביר
    אליו send_signal - כך המודול הזה לא תלוי ב-network_client ישירות.
    """

    def __init__(self, send_signal_callback, on_remote_frame=None, on_call_ended=None):
        self._send_signal = send_signal_callback   # (msg_type, payload) -> None
        self._on_remote_frame = on_remote_frame     # (numpy_bgr_frame) -> None
        self._on_call_ended = on_call_ended

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._pc: RTCPeerConnection | None = None
        self._local_track: CameraVideoTrack | None = None
        self._local_audio_track: MicrophoneAudioTrack | None = None
        self._peer_username = None

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------ API
    def start_call(self, to_username: str):
        """הצד שמתקשר (Caller): יוצר Offer ושולח לשרת."""
        self._peer_username = to_username
        self._run_coro(self._async_start_call(to_username))

    def accept_call(self, from_username: str, remote_sdp: str, remote_type: str):
        """הצד שעונה (Callee): מקבל Offer, יוצר Answer ושולח בחזרה."""
        self._peer_username = from_username
        self._run_coro(self._async_accept_call(from_username, remote_sdp, remote_type))

    def handle_remote_answer(self, remote_sdp: str, remote_type: str):
        self._run_coro(self._async_set_remote(remote_sdp, remote_type))

    def handle_remote_ice(self, candidate_dict: dict):
        self._run_coro(self._async_add_ice(candidate_dict))

    def hang_up(self):
        self._run_coro(self._async_close())

    # ------------------------------------------------------------ internals
    def _new_pc(self):
        pc = RTCPeerConnection()

        @pc.on("track")
        def on_track(track):
            if track.kind == "video":
                self._run_coro(self._consume_video(track))
            elif track.kind == "audio":
                self._run_coro(self._consume_audio(track))

        @pc.on("iceconnectionstatechange")
        def on_ice_state_change():
            if pc.iceConnectionState in ("failed", "closed", "disconnected"):
                if self._on_call_ended:
                    self._on_call_ended()

        @pc.on("icecandidate")
        def on_ice_candidate(candidate):
            if candidate and self._peer_username:
                self._send_signal("call_ice", {
                    "to": self._peer_username,
                    "candidate": {
                        "sdp": candidate_to_sdp(candidate),
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    },
                })
        return pc

    async def _consume_video(self, track):
        while True:
            try:
                frame = await track.recv()
            except Exception:
                break
            img = frame.to_ndarray(format="bgr24")
            if self._on_remote_frame:
                self._on_remote_frame(img)

    async def _consume_audio(self, track):
        """
        מנגן את השמע המתקבל מהצד השני. מפריד את קבלת הפריימים המפוענחים
        (async, על ה-event loop המשותף) מהניגון בפועל לרמקולים (thread
        נפרד עם תור קטן שזורק ישן לטובת טרי) - כדי שגם אם הניגון עצמו
        מתעכב לרגע (עומס CPU), זה לא "יצטבר" לדיליי גדל-והולך.

        חשוב: כל פריים עובר resample מפורש לפורמט קבוע וידוע מראש
        (48000Hz / מונו / s16) - בלי זה, אם ה-sample_rate שהפריים "מצהיר"
        עליו לא תואם בדיוק למה שבאמת מנגנים, נשמע בדיוק אפקט ה"קול-דמון/
        האקר" המעוות שתיארת (ניגון בקצב לא נכון = פיץ' ומהירות שגויים).
        """
        resampler = AudioResampler(format="s16", layout="mono", rate=AUDIO_SAMPLE_RATE)
        playback_queue = queue.Queue(maxsize=5)  # עד ~100ms buffer
        stop_flag = threading.Event()

        def playback_worker():
            output_stream = sd.OutputStream(
                samplerate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS,
                dtype="int16", latency="low",
            )
            output_stream.start()
            try:
                while not stop_flag.is_set():
                    try:
                        pcm = playback_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    output_stream.write(pcm)
            finally:
                output_stream.stop()
                output_stream.close()

        threading.Thread(target=playback_worker, daemon=True).start()

        try:
            while True:
                try:
                    frame = await track.recv()
                except Exception:
                    break
                for resampled in resampler.resample(frame):
                    pcm = resampled.to_ndarray().T.copy()  # -> shape (samples, 1)
                    if playback_queue.full():
                        try:
                            playback_queue.get_nowait()  # זורקים ישן כדי לא לצבור דיליי
                        except queue.Empty:
                            pass
                    try:
                        playback_queue.put_nowait(pcm)
                    except queue.Full:
                        pass
        finally:
            stop_flag.set()

    async def _async_start_call(self, to_username):
        self._pc = self._new_pc()
        self._local_track = CameraVideoTrack()
        self._local_audio_track = MicrophoneAudioTrack()
        self._pc.addTrack(self._local_track)
        self._pc.addTrack(self._local_audio_track)

        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        self._send_signal("call_offer", {
            "to": to_username,
            "sdp": self._pc.localDescription.sdp,
            "sdp_type": self._pc.localDescription.type,
        })

    async def _async_accept_call(self, from_username, remote_sdp, remote_type):
        self._pc = self._new_pc()
        self._local_track = CameraVideoTrack()
        self._local_audio_track = MicrophoneAudioTrack()
        self._pc.addTrack(self._local_track)
        self._pc.addTrack(self._local_audio_track)

        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=remote_sdp, type=remote_type))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        self._send_signal("call_answer", {
            "to": from_username,
            "sdp": self._pc.localDescription.sdp,
            "sdp_type": self._pc.localDescription.type,
        })

    async def _async_set_remote(self, remote_sdp, remote_type):
        if self._pc:
            await self._pc.setRemoteDescription(RTCSessionDescription(sdp=remote_sdp, type=remote_type))

    async def _async_add_ice(self, candidate_dict):
        if not self._pc or not candidate_dict:
            return
        try:
            cand = candidate_from_sdp(candidate_dict["sdp"])
            cand.sdpMid = candidate_dict.get("sdpMid")
            cand.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
            await self._pc.addIceCandidate(cand)
        except Exception as e:
            print(f"[WEBRTC] שגיאה בהוספת ICE candidate: {e}")

    async def _async_close(self):
        if self._local_track:
            self._local_track.stop()
            self._local_track = None
        if self._local_audio_track:
            self._local_audio_track.stop()
            self._local_audio_track = None
        if self._pc:
            await self._pc.close()
            self._pc = None