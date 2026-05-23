#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path


def env_int(name: str, fallback: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return fallback
    return value if value > 0 else fallback



def env_bool(name: str, fallback: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return fallback
    return value in {"1", "yes", "true", "on", "enabled"}

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


def resolve_finalize_text_url() -> str:
    explicit = str(os.environ.get("ASR_FINALIZE_TEXT_URL", "")).strip()
    if explicit:
        return explicit
    transcription_url = resolve_transcription_url()
    parsed = urllib.parse.urlparse(transcription_url)
    if parsed.path.rstrip("/").endswith("/api/asr/transcriptions"):
        path = parsed.path.rstrip("/") + "/finalize-text"
    else:
        path = "/api/asr/transcriptions/finalize-text"
    return urllib.parse.urlunparse(parsed._replace(path=path, params="", query="", fragment=""))


def resolve_asr_preview_ws_url() -> str:
    explicit = str(os.environ.get("ASR_PREVIEW_WS_URL", "")).strip()
    if explicit:
        return explicit
    transcription_url = resolve_transcription_url()
    parsed = urllib.parse.urlparse(transcription_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunparse(parsed._replace(scheme=scheme, path="/api/asr-preview/ws", params="", query="", fragment=""))


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


def notify_text(value: str, *, progress: int = 50, notify_id: str | None = None) -> None:
    text = normalize_text(value)
    if not text:
        return
    popup_text_file = os.environ.get("VOICE_RECORDING_POPUP_TEXT_FILE", "").strip()
    if popup_text_file:
        try:
            Path(popup_text_file).write_text(f"# {text}\n", encoding="utf-8")
            return
        except Exception as exc:
            append_log(f"recording popup update failed: {type(exc).__name__}: {exc}")
    notify_id = notify_id or os.environ.get("VOICE_NOTIFY_ID", "991199")
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


class QwenAsrStreamingPreview:
    def __init__(self, *, sample_rate: int, channels: int) -> None:
        self.enabled = env_bool("VOICE_QWEN_ASR_STREAMING", True)
        self.sample_rate = sample_rate
        self.channels = channels
        self.timeout = max(0.2, env_float("ASR_PREVIEW_WS_TIMEOUT", 2.0))
        self.final_wait_seconds = max(0.1, env_float("ASR_PREVIEW_FINAL_WAIT_SECONDS", 2.5))
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self.connected = False
        self.last_text = ""
        self.final_text = ""
        self.done = False
        if not self.enabled:
            return
        if self.channels != 1:
            append_log("qwen ASR streaming disabled: VOICE_CHANNELS must be 1")
            self.enabled = False
            return
        try:
            self.connect(resolve_asr_preview_ws_url())
            self.send_json({"type": "start", "sampleRate": self.sample_rate, "channels": self.channels, "format": "s16le"})
            append_log("qwen ASR streaming preview connected")
        except Exception as exc:
            append_log(f"qwen ASR streaming preview unavailable: {type(exc).__name__}: {exc}")
            self.close()
            self.enabled = False

    def connect(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise RuntimeError(f"unsupported ASR preview websocket scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise RuntimeError("ASR preview websocket host is required")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw_sock = socket.create_connection((parsed.hostname, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            sock: socket.socket | ssl.SSLSocket = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=parsed.hostname)
        else:
            sock = raw_sock
        sock.settimeout(self.timeout)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{port}"
        headers = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(headers.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 8192:
                break
        head = bytes(response).split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
        if " 101 " not in head.split("\r\n", 1)[0]:
            raise RuntimeError(f"websocket upgrade failed: {head.splitlines()[0] if head else 'empty response'}")
        accept_expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        if accept_expected.lower() not in head.lower():
            raise RuntimeError("websocket upgrade failed: invalid Sec-WebSocket-Accept")
        self.sock = sock
        self.connected = True

    def send_frame(self, opcode: int, payload: bytes) -> None:
        if self.sock is None or not self.connected:
            return
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv_exact(self, size: int) -> bytes:
        if self.sock is None:
            return b""
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.sock.recv(size - len(chunks))
            if not chunk:
                raise RuntimeError("websocket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def recv_frame(self, timeout: float) -> tuple[int, bytes] | None:
        if self.sock is None:
            return None
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            first = self.recv_exact(2)
            if not first:
                return None
            b1, b2 = first
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self.recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.recv_exact(8))[0]
            mask = self.recv_exact(4) if masked else b""
            payload = self.recv_exact(length) if length else b""
            if masked and payload:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            return opcode, payload
        except socket.timeout:
            return None
        finally:
            try:
                self.sock.settimeout(old_timeout)
            except Exception:
                pass

    def send_json(self, payload: dict[str, object]) -> None:
        self.send_frame(1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def process_message(self, payload: bytes) -> str:
        try:
            message = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            return ""
        if not isinstance(message, dict):
            return ""
        data = message.get("data")
        if isinstance(data, dict):
            candidates = [data.get("text"), data.get("finalText"), data.get("partial"), data.get("rawText")]
        else:
            candidates = [message.get("text"), message.get("finalText"), message.get("partial"), message.get("rawText")]
        text = ""
        for candidate in candidates:
            text = normalize_text(str(candidate or ""))
            if text:
                break
        event_type = normalize_text(str(message.get("type") or message.get("event") or "")).lower()
        is_final = bool(message.get("final") or message.get("isFinal") or message.get("done") or event_type in {"final", "segment", "done"})
        if text:
            self.last_text = text
            if is_final:
                self.final_text = text
        if event_type in {"done", "end", "closed"} or bool(message.get("done")):
            self.done = True
        if message.get("error"):
            append_log(f"qwen ASR streaming error: {message.get('error')}")
        return text

    def drain(self, total_timeout: float, idle_timeout: float = 0.02) -> str:
        if not self.connected:
            return ""
        deadline = time.monotonic() + max(0.0, total_timeout)
        latest = ""
        while time.monotonic() < deadline and not self.done:
            timeout = min(max(0.001, deadline - time.monotonic()), idle_timeout)
            frame = self.recv_frame(timeout)
            if frame is None:
                if latest:
                    break
                continue
            opcode, payload = frame
            if opcode == 1:
                text = self.process_message(payload)
                if text:
                    latest = text
            elif opcode == 8:
                self.done = True
                break
            elif opcode == 9:
                self.send_frame(10, payload)
        return latest

    def accept_pcm(self, pcm: bytes) -> str:
        if not self.enabled or not self.connected or not pcm:
            return ""
        try:
            self.send_frame(2, pcm)
            text = self.drain(0.001)
            return text
        except Exception as exc:
            append_log(f"qwen ASR streaming failed: {type(exc).__name__}: {exc}")
            self.close()
            self.enabled = False
            return ""

    def finish(self) -> str:
        if not self.enabled or not self.connected:
            return normalize_text(self.final_text or self.last_text)
        try:
            self.send_json({"type": "stop"})
            self.drain(self.final_wait_seconds, idle_timeout=0.1)
        except Exception as exc:
            append_log(f"qwen ASR streaming final wait failed: {type(exc).__name__}: {exc}")
        finally:
            self.close()
        return normalize_text(self.final_text or self.last_text)

    def close(self) -> None:
        sock = self.sock
        self.sock = None
        self.connected = False
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class SegmentingTranscriptionSession:
    def __init__(self) -> None:
        self.state_dir = Path(os.environ["VOICE_STATE_DIR"])
        self.result_file = Path(os.environ["STREAM_RESULT_FILE"])
        self.stop_file = Path(os.environ["STREAM_STOP_FILE"])
        self.sample_rate = env_int("VOICE_SAMPLE_RATE", 16000)
        self.channels = env_int("VOICE_CHANNELS", 1)
        self.chunk_ms = env_int("VOICE_STREAM_SEND_INTERVAL_MS", 100)
        self.max_record_ms = env_int("VOICE_MAX_RECORD_MS", 60000)
        self.timeout = max(1.0, float(os.environ.get("ASR_TIMEOUT", "60") or 60))
        self.request_attempt_timeout = max(1.0, env_float("ASR_REQUEST_ATTEMPT_TIMEOUT", min(8.0, self.timeout)))
        self.connect_timeout = max(0.2, env_float("ASR_CONNECT_TIMEOUT", min(2.0, self.request_attempt_timeout)))
        self.retry_count = max(1, env_int("ASR_RETRY_COUNT", 3))
        self.retry_delay = max(0.0, env_float("ASR_RETRY_DELAY", 0.35))
        self.stop_requested = False
        self.request_id = f"uconsole_final_{int(time.time() * 1000)}_{os.getpid()}"
        self.segment_index = 0
        self.final_text = ""
        self.raw_text = ""
        self.corrected_text = ""
        self.qwen_preview_text = ""
        self.last_request_id = self.request_id
        self.pending_segment = bytearray()
        self.qwen_preview = QwenAsrStreamingPreview(sample_rate=self.sample_rate, channels=self.channels)
        signal.signal(signal.SIGTERM, self._signal_stop)
        signal.signal(signal.SIGINT, self._signal_stop)

    def _signal_stop(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True
        try:
            self.stop_file.touch()
        except Exception:
            pass

    def recorder_commands(self) -> list[list[str]]:
        recorder = os.environ.get("VOICE_RECORDER", "auto")
        if recorder == "auto":
            return [
                self.recorder_command_for(candidate)
                for candidate in ("arecord", "pw-record", "ffmpeg")
                if shutil_which(candidate)
            ]
        return [self.recorder_command_for(recorder)]

    def recorder_command_for(self, recorder: str) -> list[str]:
        input_name = os.environ.get("VOICE_INPUT", "default")
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

    def recorder_command(self) -> list[str]:
        commands = self.recorder_commands()
        if not commands:
            raise RuntimeError("pw-record, arecord, or ffmpeg is required for voice input")
        return commands[0]

    def write_result(self, *, status: str, error: str = "") -> None:
        payload = {
            "status": status,
            "requestId": self.last_request_id,
            "text": normalize_text(self.final_text),
            "rawText": normalize_text(self.raw_text or self.final_text),
            "streamText": normalize_text(self.qwen_preview_text),
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

    def check_asr_connectivity(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return
        if not parsed.hostname:
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((parsed.hostname, port), timeout=self.connect_timeout):
                return
        except OSError as exc:
            raise RuntimeError(
                f"ASR endpoint unreachable before upload: {parsed.hostname}:{port} "
                f"connectTimeout={self.connect_timeout:g}s"
            ) from exc

    def post_transcription_request(self, request: urllib.request.Request) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                self.check_asr_connectivity(request.full_url)
                append_log(
                    f"final ASR request attempt={attempt}/{self.retry_count} "
                    f"connectTimeout={self.connect_timeout:g}s requestTimeout={self.request_attempt_timeout:g}s"
                )
                with urllib.request.urlopen(request, timeout=self.request_attempt_timeout) as response:
                    return response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError:
                raise
            except Exception as exc:
                last_error = exc
                append_log(f"final ASR request attempt failed attempt={attempt}/{self.retry_count}: {type(exc).__name__}: {exc}")
                if attempt < self.retry_count and self.retry_delay > 0:
                    time.sleep(self.retry_delay)
        if last_error is not None:
            raise RuntimeError(f"final transcription failed after {self.retry_count} attempts: {last_error}") from last_error
        raise RuntimeError("final transcription failed without an error")

    def post_json_request(self, url: str, payload: dict[str, object]) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {os.environ.get('ASR_AUTH_TOKEN', '').strip()}",
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                self.check_asr_connectivity(url)
                append_log(
                    f"streaming final text request attempt={attempt}/{self.retry_count} "
                    f"connectTimeout={self.connect_timeout:g}s requestTimeout={self.request_attempt_timeout:g}s"
                )
                with urllib.request.urlopen(request, timeout=self.request_attempt_timeout) as response:
                    return response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError:
                raise
            except Exception as exc:
                last_error = exc
                append_log(f"streaming final text request attempt failed attempt={attempt}/{self.retry_count}: {type(exc).__name__}: {exc}")
                if attempt < self.retry_count and self.retry_delay > 0:
                    time.sleep(self.retry_delay)
        if last_error is not None:
            raise RuntimeError(f"streaming final text failed after {self.retry_count} attempts: {last_error}") from last_error
        raise RuntimeError("streaming final text failed without an error")

    def finalize_streaming_text(self, raw_text: str) -> str:
        text = normalize_text(raw_text)
        if not text:
            return ""
        payload: dict[str, object] = self.build_fields(self.request_id)
        payload["rawText"] = text
        try:
            raw = self.post_json_request(resolve_finalize_text_url(), payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"streaming final text failed: HTTP {exc.code} {detail}") from exc
        final_text, raw_text_result, corrected_text, _applied, response_request_id = extract_transcription_fields(json.loads(raw) if raw else {})
        if is_placeholder_text(final_text):
            return ""
        self.final_text = final_text or text
        self.raw_text = raw_text_result or text
        self.corrected_text = corrected_text
        self.last_request_id = response_request_id or self.request_id
        append_log(
            f"streaming final text finalized requestId={self.last_request_id} "
            f"chars={len(self.final_text)} rawChars={len(self.raw_text)}"
        )
        return self.final_text

    def transcribe_segment(self, pcm: bytes, *, final_segment: bool = False) -> str:
        if len(pcm) < int(self.sample_rate * self.channels * 2 * 0.25):
            return ""
        self.segment_index += 1
        request_id = self.request_id if final_segment else f"{self.request_id}_{self.segment_index}"
        audio_path = self.write_wav(pcm, request_id)
        try:
            append_log(
                f"final ASR upload started requestId={request_id} bytes={len(pcm)} "
                f"durationMs={int(len(pcm) / max(1, self.sample_rate * self.channels * 2) * 1000)}"
            )
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
                raw = self.post_transcription_request(request)
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
                if final_segment:
                    self.final_text = text
                    self.raw_text = raw_text or text
                    self.corrected_text = corrected_text
                else:
                    self.final_text = append_transcript(self.final_text, text)
                    self.raw_text = append_transcript(self.raw_text, raw_text or text)
                    self.corrected_text = append_transcript(self.corrected_text, corrected_text)
                self.last_request_id = response_request_id or request_id
                append_log(
                    f"server ASR transcribed requestId={request_id} final={int(final_segment)} "
                    f"chars={len(text)} totalChars={len(self.final_text)}"
                )
            else:
                append_log(f"server ASR returned empty requestId={request_id}")
            return text
        except Exception:
            raise
        finally:
            if os.environ.get("VOICE_KEEP_AUDIO", "0") != "1":
                try:
                    audio_path.unlink()
                except FileNotFoundError:
                    pass

    def run(self) -> int:
        if not os.environ.get("ASR_AUTH_TOKEN", "").strip():
            raise RuntimeError("ASR_AUTH_TOKEN is required for FlashAI ASR")
        recorder_commands = self.recorder_commands()
        if not recorder_commands:
            raise RuntimeError("pw-record, arecord, or ffmpeg is required for voice input")
        frame_bytes = max(320, int(self.sample_rate * self.chunk_ms / 1000) * 2 * self.channels)
        recording = bytearray()
        append_log(
            f"stream ASR started requestId={self.request_id} frameBytes={frame_bytes} "
            "mode=qwen-streaming-final-local-fallback"
        )
        recorder: subprocess.Popen[bytes] | None = None
        try:
            deadline = time.monotonic() + (self.max_record_ms / 1000) if self.max_record_ms > 0 else None
            for command_index, command in enumerate(recorder_commands, start=1):
                if self.stop_requested or self.stop_file.exists():
                    break
                append_log(
                    "stream recorder starting "
                    f"attempt={command_index}/{len(recorder_commands)} "
                    f"cmd={shlex.join(command)}"
                )
                recorder = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                while not self.stop_requested and not self.stop_file.exists():
                    if deadline is not None and time.monotonic() >= deadline:
                        append_log(f"stream ASR max record reached requestId={self.request_id} maxMs={self.max_record_ms}")
                        break
                    assert recorder.stdout is not None
                    data = recorder.stdout.read(frame_bytes)
                    if not data:
                        return_code = recorder.poll()
                        stderr_text = ""
                        try:
                            if recorder.stderr is not None:
                                stderr_text = recorder.stderr.read(4000).decode("utf-8", "replace").strip()
                        except Exception:
                            stderr_text = ""
                        append_log(
                            "stream recorder ended without audio "
                            f"attempt={command_index}/{len(recorder_commands)} "
                            f"returnCode={return_code} stderr={stderr_text[:500]}"
                        )
                        break
                    recording.extend(data)
                    qwen_preview_text = self.qwen_preview.accept_pcm(data)
                    if qwen_preview_text:
                        self.qwen_preview_text = qwen_preview_text
                        notify_text(qwen_preview_text, progress=35, notify_id=os.environ.get("VOICE_RECORDING_NOTIFY_ID", "991200"))
                try:
                    if recorder.poll() is None:
                        recorder.terminate()
                        recorder.wait(timeout=2)
                except Exception:
                    try:
                        recorder.kill()
                    except Exception:
                        pass
                recorder = None
                if recording or self.stop_requested or self.stop_file.exists():
                    break
            self.stop_requested = True
            qwen_text = self.qwen_preview.finish()
            if qwen_text:
                self.qwen_preview_text = qwen_text
            preferred_text = normalize_text(qwen_text)
            if preferred_text:
                try:
                    finalized_text = self.finalize_streaming_text(preferred_text)
                    if not finalized_text:
                        self.final_text = preferred_text
                        self.raw_text = preferred_text
                        self.corrected_text = ""
                        append_log(
                            f"streaming final text returned empty; using preview result fallback "
                            f"requestId={self.request_id} chars={len(preferred_text)}"
                        )
                except Exception as exc:
                    self.final_text = preferred_text
                    self.raw_text = preferred_text
                    self.corrected_text = ""
                    append_log(
                        f"streaming final text failed; using preview result fallback "
                        f"requestId={self.request_id} chars={len(preferred_text)} error={type(exc).__name__}: {exc}"
                    )
            elif recording:
                self.transcribe_segment(bytes(recording), final_segment=True)
            self.write_result(status="ok" if self.final_text else "empty")
            append_log(
                f"stream ASR finished requestId={self.request_id} bytes={len(recording)} "
                f"finalChars={len(self.final_text)}"
            )
            return 0
        finally:
            try:
                if recorder is not None and recorder.poll() is None:
                    recorder.terminate()
                    recorder.wait(timeout=2)
            except Exception:
                try:
                    if recorder is not None:
                        recorder.kill()
                except Exception:
                    pass


def main() -> int:
    session = SegmentingTranscriptionSession()
    try:
        return session.run()
    except Exception as exc:
        append_log(f"stream ASR failed: {type(exc).__name__}: {exc}")
        try:
            session.write_result(status="error", error=str(exc))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
