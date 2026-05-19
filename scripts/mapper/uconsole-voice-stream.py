#!/usr/bin/env python3
from __future__ import annotations

import audioop
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path


def env_int(name: str, fallback: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def env_float(name: str, fallback: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split()).strip()


def is_placeholder_text(value: str) -> bool:
    normalized = normalize_text(value)
    if not normalized:
        return True
    return normalized in {"嗯", "嗯。", "嗯，", "呃", "呃。", "啊", "啊。", "啊，"}


def strip_auto_segment_terminal_punctuation(value: str) -> str:
    return normalize_text(value).rstrip("。.").rstrip()


def strip_trailing_sentence_punctuation(value: str) -> str:
    text = normalize_text(value)
    return text.rstrip("。！？!?…").rstrip()


def should_insert_segment_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_tail = left[-1]
    right_head = right[0]
    if left_tail in "，。！？；：、）》」』】…,.!?;:)\"'’”":
        return False
    if right_head in "，。！？；：、）》」』】…,.!?;:)\"'’”":
        return False
    if left_tail.isascii() or right_head.isascii():
        return True
    return False


def append_transcript(current: str, segment: str) -> str:
    current = str(current or "").rstrip()
    segment = normalize_text(segment)
    if not segment:
        return current
    if not current:
        return segment
    max_overlap = min(12, len(current), len(segment))
    for size in range(max_overlap, 0, -1):
        if current[-size:] == segment[:size]:
            segment = segment[size:].lstrip()
            break
    if not segment:
        return current
    if should_insert_segment_space(current, segment):
        return f"{current} {segment}"
    return f"{current}{segment}"


def resolve_transcription_url() -> str:
    explicit = str(os.environ.get("ASR_URL", "")).strip()
    if explicit:
        return explicit
    raise RuntimeError("ASR_URL is required for final ASR transcription")


def append_log(message: str) -> None:
    log_file = Path(os.environ.get("VOICE_STREAM_LOG_FILE", ""))
    if not log_file:
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%F %T')}] {message}\n")
    except Exception:
        pass


def notify_text(value: str, *, progress: int = 50) -> None:
    text = normalize_text(value)
    if not text:
        return
    notify_id = os.environ.get("VOICE_NOTIFY_ID", "991199")
    if shutil_which("dunstify"):
        subprocess.run(
            [
                "dunstify",
                "-a",
                "uconsole-voice",
                "-r",
                notify_id,
                "-u",
                "low",
                "-t",
                "0",
                "-h",
                f"int:value:{progress}",
                "uconsole voice",
                text,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    if shutil_which("notify-send"):
        subprocess.run(
            ["notify-send", "uconsole voice", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def build_multipart_form(fields: dict[str, str], file_field: str, file_path: Path, content_type: str) -> tuple[bytes, str]:
    boundary = f"----uconsoleVoiceAsr{int(time.time() * 1000)}{os.getpid()}"
    parts: list[bytes] = []
    for key, value in fields.items():
        normalized_value = str(value or "")
        if not normalized_value:
            continue
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{normalized_value}\r\n"
            ).encode("utf-8")
        )
    file_name = file_path.name
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(file_path.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def extract_transcription_fields(payload: object) -> tuple[str, str, str, bool, str]:
    if not isinstance(payload, dict):
        return "", "", "", False, ""
    data = payload.get("data")
    if isinstance(data, dict):
        corrected = normalize_text(str(data.get("correctedText") or ""))
        raw = normalize_text(str(data.get("rawText") or ""))
        text = normalize_text(str(data.get("text") or ""))
        applied = bool(data.get("correctionApplied"))
        request_id = normalize_text(str(data.get("requestId") or ""))
        return (corrected or text or raw), raw, corrected, applied, request_id
    text = normalize_text(str(payload.get("text") or payload.get("rawText") or ""))
    return text, normalize_text(str(payload.get("rawText") or text)), "", False, normalize_text(str(payload.get("requestId") or ""))


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class SegmentingTranscriptionSession:
    def __init__(self) -> None:
        self.state_dir = Path(os.environ["VOICE_STATE_DIR"])
        self.result_file = Path(os.environ["STREAM_RESULT_FILE"])
        self.stop_file = Path(os.environ["STREAM_STOP_FILE"])
        self.sample_rate = env_int("VOICE_SAMPLE_RATE", 16000)
        self.channels = env_int("VOICE_CHANNELS", 1)
        self.chunk_ms = env_int("VOICE_STREAM_SEND_INTERVAL_MS", 200)
        self.pause_ms = env_int("VOICE_PAUSE_SEGMENT_MS", 1600)
        self.min_segment_ms = env_int("VOICE_MIN_SEGMENT_MS", 1000)
        self.rms_threshold = env_float("VOICE_AUTO_SEGMENT_RMS_THRESHOLD", 0.006)
        self.noise_margin = env_float("VOICE_AUTO_SEGMENT_NOISE_MARGIN", 0.004)
        self.timeout = max(1.0, float(os.environ.get("ASR_TIMEOUT", "60") or 60))
        self.stop_requested = False
        self.request_id = f"uconsole_final_{int(time.time() * 1000)}_{os.getpid()}"
        self.segment_index = 0
        self.final_text = ""
        self.raw_text = ""
        self.corrected_text = ""
        self.last_request_id = self.request_id
        signal.signal(signal.SIGTERM, self._signal_stop)
        signal.signal(signal.SIGINT, self._signal_stop)

    def _signal_stop(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True
        try:
            self.stop_file.touch()
        except Exception:
            pass

    def recorder_command(self) -> list[str]:
        input_name = os.environ.get("VOICE_INPUT", "default")
        recorder = os.environ.get("VOICE_RECORDER", "auto")
        if recorder == "auto":
            if shutil_which("pw-record"):
                recorder = "pw-record"
            elif shutil_which("arecord"):
                recorder = "arecord"
            else:
                recorder = "ffmpeg"
        if recorder == "pw-record":
            command = [
                "pw-record",
                "--rate",
                str(self.sample_rate),
                "--channels",
                str(self.channels),
                "--format",
                "s16",
            ]
            if input_name != "default":
                command.extend(["--target", input_name])
            command.append("-")
            return command
        if recorder == "arecord":
            command = [
                "arecord",
                "-q",
                "-t",
                "raw",
                "-f",
                "S16_LE",
                "-r",
                str(self.sample_rate),
                "-c",
                str(self.channels),
            ]
            if input_name != "default":
                command.extend(["-D", input_name])
            command.append("-")
            return command
        if recorder == "ffmpeg":
            return [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "pulse",
                "-i",
                input_name,
                "-ac",
                str(self.channels),
                "-ar",
                str(self.sample_rate),
                "-f",
                "s16le",
                "-",
            ]
        raise RuntimeError(f"unsupported VOICE_RECORDER: {recorder}")

    def write_result(self, *, status: str, error: str = "") -> None:
        payload = {
            "status": status,
            "requestId": self.last_request_id,
            "text": normalize_text(self.final_text),
            "rawText": normalize_text(self.raw_text or self.final_text),
            "streamText": "",
            "correctedText": normalize_text(self.corrected_text),
            "error": error,
        }
        tmp = self.result_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.result_file)

    def build_fields(self, request_id: str) -> dict[str, str]:
        fields = {
            "requestId": request_id,
            "language": os.environ.get("ASR_LANGUAGE", os.environ.get("VOICE_LANGUAGE", "zh")),
            "correctionMode": os.environ.get("ASR_CORRECTION_MODE", "auto"),
        }
        prompt = os.environ.get("ASR_PROMPT", "")
        if prompt:
            fields[os.environ.get("ASR_PROMPT_FIELD", "prompt")] = prompt
        prompt_glossary = os.environ.get("ASR_PROMPT_GLOSSARY", "")
        if prompt_glossary:
            fields[os.environ.get("ASR_PROMPT_GLOSSARY_FIELD", "promptGlossary")] = prompt_glossary
        context_text = os.environ.get("ASR_CONTEXT_TEXT", "")
        if context_text:
            fields[os.environ.get("ASR_CONTEXT_FIELD", "contextText")] = context_text
        return fields

    def write_wav(self, pcm: bytes, request_id: str) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(prefix=f"{request_id}-", suffix=".wav", dir=str(self.state_dir), delete=False)
        handle.close()
        path = Path(handle.name)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm)
        return path

    def transcribe_segment(self, pcm: bytes, *, final_segment: bool = False) -> str:
        if len(pcm) < int(self.sample_rate * self.channels * 2 * 0.25):
            return ""
        self.segment_index += 1
        request_id = f"{self.request_id}_{self.segment_index}"
        audio_path = self.write_wav(pcm, request_id)
        try:
            body, boundary = build_multipart_form(self.build_fields(request_id), "file", audio_path, "audio/wav")
            request = urllib.request.Request(
                resolve_transcription_url(),
                data=body,
                headers={
                    "Authorization": f"Bearer {os.environ.get('ASR_AUTH_TOKEN', '').strip()}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError(f"final transcription failed: HTTP {exc.code} {detail}") from exc
            text, raw_text, corrected_text, _applied, response_request_id = extract_transcription_fields(json.loads(raw) if raw else {})
            if is_placeholder_text(text):
                text = ""
                raw_text = ""
                corrected_text = ""
            elif not final_segment:
                text = strip_trailing_sentence_punctuation(text)
                raw_text = strip_trailing_sentence_punctuation(raw_text or text)
                corrected_text = strip_trailing_sentence_punctuation(corrected_text)
            if text:
                self.final_text = append_transcript(self.final_text, text)
                self.raw_text = append_transcript(self.raw_text, raw_text or text)
                self.corrected_text = append_transcript(self.corrected_text, corrected_text)
                self.last_request_id = response_request_id or request_id
                notify_text(self.final_text, progress=80 if final_segment else 50)
                append_log(
                    f"segment transcribed requestId={request_id} final={int(final_segment)} "
                    f"chars={len(text)} totalChars={len(self.final_text)}"
                )
            return text
        except Exception:
            raise
        finally:
            if os.environ.get("VOICE_KEEP_AUDIO", "0") != "1":
                try:
                    audio_path.unlink()
                except FileNotFoundError:
                    pass

    def should_segment(
        self,
        *,
        now: float,
        segment_started_at: float,
        last_speech_at: float,
        has_speech: bool,
        rms: float,
        noise_floor: float,
    ) -> tuple[bool, bool, float]:
        threshold = max(0.004, min(self.rms_threshold, noise_floor + self.noise_margin)) if noise_floor > 0 else self.rms_threshold
        is_speech = rms >= threshold
        if is_speech:
            return False, True, noise_floor
        if not has_speech:
            next_floor = rms if noise_floor <= 0 else (noise_floor * 0.92) + (rms * 0.08)
            return False, False, next_floor
        segment_ms = (now - segment_started_at) * 1000
        silence_ms = (now - last_speech_at) * 1000
        return segment_ms >= self.min_segment_ms and silence_ms >= self.pause_ms, False, noise_floor

    def process_segment(self, segment: bytearray, *, force: bool = False) -> None:
        if not segment:
            return
        pcm = bytes(segment)
        if force or len(pcm) >= int(self.sample_rate * self.channels * 2 * self.min_segment_ms / 1000):
            try:
                self.transcribe_segment(pcm, final_segment=force)
            except Exception as exc:
                if not force:
                    append_log(f"skip auto segment transcription: {type(exc).__name__}: {exc}")
                    return
                raise
        segment.clear()

    def run(self) -> int:
        if not os.environ.get("ASR_AUTH_TOKEN", "").strip():
            raise RuntimeError("ASR_AUTH_TOKEN is required for FlashAI ASR")
        if not (shutil_which("pw-record") or shutil_which("arecord") or shutil_which("ffmpeg")):
            raise RuntimeError("pw-record, arecord, or ffmpeg is required for voice input")
        recorder = subprocess.Popen(self.recorder_command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        frame_bytes = max(320, int(self.sample_rate * self.chunk_ms / 1000) * 2 * self.channels)
        segment = bytearray()
        segment_started_at = time.monotonic()
        last_speech_at = segment_started_at
        has_speech = False
        noise_floor = 0.0
        segment_had_speech = False
        append_log(
            f"segmenting ASR started requestId={self.request_id} frameBytes={frame_bytes} "
            f"pauseMs={self.pause_ms} rmsThreshold={self.rms_threshold}"
        )
        try:
            while not self.stop_requested and not self.stop_file.exists():
                assert recorder.stdout is not None
                data = recorder.stdout.read(frame_bytes)
                if not data:
                    break
                segment.extend(data)
                rms = audioop.rms(data, 2) / 32768.0 if data else 0.0
                now = time.monotonic()
                should_upload, is_speech, noise_floor = self.should_segment(
                    now=now,
                    segment_started_at=segment_started_at,
                    last_speech_at=last_speech_at,
                    has_speech=has_speech,
                    rms=rms,
                    noise_floor=noise_floor,
                )
                if is_speech:
                    has_speech = True
                    segment_had_speech = True
                    last_speech_at = now
                if should_upload:
                    self.process_segment(segment)
                    segment_started_at = time.monotonic()
                    last_speech_at = segment_started_at
                    has_speech = False
                    noise_floor = 0.0
                    segment_had_speech = False
            self.stop_requested = True
            if segment_had_speech:
                self.process_segment(segment, force=True)
            else:
                segment.clear()
            self.write_result(status="ok" if self.final_text else "empty")
            append_log(f"segmenting ASR finished requestId={self.request_id} finalChars={len(self.final_text)}")
            return 0
        finally:
            try:
                if recorder.poll() is None:
                    recorder.terminate()
                    recorder.wait(timeout=2)
            except Exception:
                try:
                    recorder.kill()
                except Exception:
                    pass


def main() -> int:
    session = SegmentingTranscriptionSession()
    try:
        return session.run()
    except Exception as exc:
        append_log(f"segmenting ASR failed: {type(exc).__name__}: {exc}")
        try:
            session.write_result(status="error", error=str(exc))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
