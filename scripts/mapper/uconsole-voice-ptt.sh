#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

IME_WAS_ACTIVE=0

hydrate_session_env() {
  local session_env=
  local key sep value

  if ! command -v systemctl >/dev/null 2>&1; then
    return 0
  fi
  session_env=$(systemctl --user show-environment 2>/dev/null || true)
  while IFS= read -r line; do
    key=
    sep=
    value=
    key=${line%%=*}
    sep=${line#*=}
    if [[ -n "${key}" && "${line}" == *"="* ]]; then
      value=${line#*=}
      case "${key}" in
        XDG_RUNTIME_DIR|WAYLAND_DISPLAY|DISPLAY|DBUS_SESSION_BUS_ADDRESS|XAUTHORITY)
          export "${key}=${value}"
          ;;
      esac
    fi
  done <<<"${session_env}"
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
  export DISPLAY="${DISPLAY:-:0}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
  export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
}

hydrate_session_env

usage() {
  cat <<'EOF'
Usage:
  uconsole-voice-ptt start
  uconsole-voice-ptt stop
  uconsole-voice-ptt cancel

Configuration is read from:
  $VOICE_PTT_CONFIG
  ~/.config/uconsole-helper-mapper/voice.env

Supported variables:
  ASR_URL            required, ASR endpoint
  ASR_LANGUAGE       optional multipart field
  ASR_AUTH_TOKEN     required for FlashAI ASR, bearer token with asr:transcribe
  ASR_PROMPT         optional short ASR prompt hint
  ASR_PROMPT_FIELD   multipart field for ASR prompt, default: prompt
  ASR_PROMPT_GLOSSARY_FIELD
                         multipart field for prompt glossary JSON, default: promptGlossary
  VOICE_GLOSSARY_FILE    glossary file path, one term per line; default:
                         ~/.config/uconsole-helper-mapper/voice-glossary.txt
  ASR_CONTEXT_FIELD  multipart field for tmux context, default: contextText
  ASR_CORRECTION_MODE
                         off | on | auto, default: auto
  ASR_NO_PROXY       1 disables proxy for ASR requests, default: 1
  ASR_TIMEOUT        ASR request timeout in seconds, default: 90; 0 disables
  ASR_REQUEST_ATTEMPT_TIMEOUT / ASR_CONNECT_TIMEOUT / ASR_RETRY_COUNT / ASR_RETRY_DELAY
                      HTTP finalize/upload retry knobs used by the stream client
  ASR_PREVIEW_WS_URL Qwen ASR streaming websocket URL; derived from ASR_URL when empty
  ASR_FINALIZE_TEXT_URL
                      endpoint for finalizing streaming text; derived from ASR_URL when empty
  ASR_PREVIEW_FINAL_WAIT_SECONDS
                      seconds to wait for Qwen streaming final after stop, default: 1.5
  VOICE_OUTPUT_MODE      type | type_enter | clipboard | paste | fcitx_commit, default: type
  VOICE_TMUX_OUTPUT_MODE output mode used when a tmux/terminal window is focused, default: type
  VOICE_WECHAT_OUTPUT_MODE
                         output mode used when WeChat is focused, default: paste
  VOICE_PASTE_SHORTCUT   ctrl_v | shift_insert, default: shift_insert
  VOICE_WECHAT_PASTE_SHORTCUT
                         ctrl_v | shift_insert, default: ctrl_v
  VOICE_PASTE_BACKEND    auto | uinput | wtype, default: auto
  VOICE_PASTE_DELAY      delay before wtype paste shortcut, default: 0.08
  VOICE_FCITX_COMMIT_FILE
                         pending text file used by fcitx_commit output
  VOICE_FCITX_COMMIT_TRIGGER
                         quick phrase trigger typed after writing the pending file, default: ;uv
  VOICE_RECORDER         auto | pw-record | ffmpeg | arecord, default: auto
  VOICE_INPUT            default audio input name, used by ffmpeg, default: default
  VOICE_MIN_RECORD_MS    minimum press duration before transcription, default: 350
  VOICE_MAX_RECORD_MS    maximum recording duration before automatic rollover, default: 60000; 0 disables
  VOICE_SAMPLE_RATE      default: 16000
  VOICE_CHANNELS         default: 1
  VOICE_STATE_DIR        default: ${XDG_STATE_HOME:-~/.local/state}/uconsole-helper-mapper
  VOICE_KEEP_AUDIO       1 keeps recorded audio after stop, default: 0
  VOICE_STREAM_PREVIEW   1 shows a WeChat-style recognition preview popup, default: 1
  VOICE_QWEN_ASR_STREAMING
                      1 uses Qwen ASR websocket streaming for preview/final, default: 1
  VOICE_NOTIFY_WHILE_PREVIEW
                        1 keeps the system recording notification even when preview is enabled, default: 0
  VOICE_STREAM_SEND_INTERVAL_MS
                        local recorder read interval, default: 50
  VOICE_STREAM_NOTIFY_FROM_READER
                        1 updates the preview popup from websocket reader events, default: 1
  VOICE_NOTIFY_USE_MARKUP
                         1 enables Pango markup for notifications, default: 0
  VOICE_NOTIFY_FONT_SIZE notification font size when markup is enabled, default: 22
  VOICE_NOTIFY_PADDING_LINES
                        extra blank lines for a taller notification, default: 1
  VOICE_TMUX_CONTEXT     1 adds active tmux pane visible text as ASR context, default: 1
  VOICE_TMUX_CONTEXT_LINES
                        minimum lines sent from the active tmux pane, default: 30
  VOICE_TMUX_CONTEXT_MAX_CHARS
                        max chars sent from tmux context, default: 1200
EOF
}

show_status() {
  local summary=$1
  local body=${2:-}
  local value=${3:-}
  local timeout=${4:-1200}

  if command -v dunstify >/dev/null 2>&1; then
    local args=(-a "uconsole-voice" -r "${VOICE_NOTIFY_ID}" -u low -t "${timeout}")
    if [[ -n "${value}" ]]; then
      args+=(-h "int:value:${value}")
    fi
    dunstify "${args[@]}" "$(format_status_text "${summary}")" "$(format_status_body "${body}")" >/dev/null 2>&1 || true
    return
  fi

  if command -v notify-send >/dev/null 2>&1; then
    notify-send "${summary}" "${body}" >/dev/null 2>&1 || true
  fi
}

close_notification() {
  local notify_id=${1:-}

  [[ -n "${notify_id}" ]] || return 0
  if command -v dunstify >/dev/null 2>&1; then
    dunstify -C "${notify_id}" >/dev/null 2>&1 || true
  fi
}

close_recording_notification() {
  local notification_pid_file="${VOICE_STATE_DIR}/voice-recording-notification.id"
  local notification_id=

  if [[ -f "${notification_pid_file}" ]]; then
    notification_id=$(cat "${notification_pid_file}" 2>/dev/null || true)
  fi
  close_notification "${notification_id}"
  close_notification "${VOICE_RECORDING_NOTIFY_ID}"
  rm -f "${notification_pid_file}" >/dev/null 2>&1 || true
}

close_recording_popup() {
  local popup_pid_file="${VOICE_STATE_DIR}/voice-recording-popup.pid"
  local popup_text_file="${VOICE_STATE_DIR}/voice-recording-popup.txt"
  local popup_pid=

  if [[ -f "${popup_pid_file}" ]]; then
    popup_pid=$(cat "${popup_pid_file}" 2>/dev/null || true)
  fi
  if [[ -n "${popup_pid}" ]]; then
    terminate_process_group "${popup_pid}" TERM
    wait_for_exit "${popup_pid}" 2 || true
    terminate_process_group "${popup_pid}" KILL
    wait_for_exit "${popup_pid}" 2 || true
  fi
  close_recording_popup_instances
  rm -f "${popup_pid_file}" >/dev/null 2>&1 || true
  rm -f "${popup_text_file}" >/dev/null 2>&1 || true
}

recording_popup_active() {
  local popup_pid=
  popup_pid=$(recording_popup_pid || true)
  [[ -n "${popup_pid}" ]] && kill -0 "${popup_pid}" >/dev/null 2>&1
}

dismiss_recording_popup() {
  mkdir -p "${VOICE_STATE_DIR}" >/dev/null 2>&1 || true
  : >"${VOICE_POPUP_DISMISSED_FILE}" || true
  close_recording_status
}

show_recording_popup_message_then_close() {
  local message=$1
  local delay_seconds=${2:-2}

  write_recording_popup_text "${message}"
  sleep "${delay_seconds}" || true
  close_recording_status
}

focus_recording_popup() {
  local spec="title:uconsole voice"

  [[ -x "${WLRCTL}" ]] || return 0
  "${WLRCTL}" window focus "${spec}" >/dev/null 2>&1 || true
  "${WLRCTL}" toplevel focus "${spec}" >/dev/null 2>&1 || true
  "${WLRCTL}" toplevel activate "${spec}" >/dev/null 2>&1 || true
}

launch_recording_popup() {
  local body="录音中"
  local popup_pid_file="${VOICE_STATE_DIR}/voice-recording-popup.pid"
  local popup_text_file="${VOICE_STATE_DIR}/voice-recording-popup.txt"
  local popup_pid=
  local session_launcher="/usr/local/bin/uconsole-launch-in-session"
  local popup_helper="${HOME}/.local/bin/uconsole-asr-popup"
  local launcher=()
  local popup_cmd=

  if [[ -x "${session_launcher}" ]]; then
    launcher=("${session_launcher}")
  fi

  close_recording_popup || true
  rm -f "${popup_text_file}" >/dev/null 2>&1 || true
  rm -f "${VOICE_POPUP_DISMISSED_FILE}" >/dev/null 2>&1 || true
  write_recording_popup_text "${body}"

  if [[ -x "${popup_helper}" ]]; then
    popup_cmd="${popup_helper}"
    log_ptt "recording popup launch cmd=${popup_cmd} launcher=${launcher[*]:-none}"
    setsid "${launcher[@]}" "${popup_helper}" "${popup_text_file}" >/dev/null 2>&1 &
    popup_pid=$!
    printf '%s\n' "${popup_pid}" >"${popup_pid_file}" || true
    log_ptt "recording popup started pid=${popup_pid}"
    (sleep 0.2; focus_recording_popup; sleep 0.5; focus_recording_popup) >/dev/null 2>&1 &
    return 0
  fi

  if command -v kdialog >/dev/null 2>&1; then
    popup_cmd=$(command -v kdialog || printf '%s' kdialog)
    log_ptt "recording popup launch cmd=${popup_cmd} launcher=${launcher[*]:-none}"
    setsid "${launcher[@]}" kdialog \
      --title "uconsole voice" \
      --msgbox "${body}" \
      >/dev/null 2>&1 &
    popup_pid=$!
    printf '%s\n' "${popup_pid}" >"${popup_pid_file}" || true
    log_ptt "recording popup started pid=${popup_pid}"
    (sleep 0.2; focus_recording_popup) >/dev/null 2>&1 &
    return 0
  fi

  if command -v zenity >/dev/null 2>&1; then
    popup_cmd=$(command -v zenity || printf '%s' zenity)
    log_ptt "recording popup launch cmd=${popup_cmd} launcher=${launcher[*]:-none}"
    setsid "${launcher[@]}" zenity \
      --info \
      --no-wrap \
      --title="uconsole voice" \
      --text="${body}" \
      --width=520 \
      >/dev/null 2>&1 &
    popup_pid=$!
    printf '%s\n' "${popup_pid}" >"${popup_pid_file}" || true
    log_ptt "recording popup started pid=${popup_pid}"
    (sleep 0.2; focus_recording_popup) >/dev/null 2>&1 &
    return 0
  fi

  log_ptt "recording popup unavailable"
  return 1
}

close_status() {
  close_notification "${VOICE_NOTIFY_ID}"
  close_recording_notification
  close_recording_popup
}

close_recording_status() {
  close_recording_notification
  close_recording_popup
}

show_recording_status() {
  local body="录音中"

  if launch_recording_popup >/dev/null 2>&1; then
    return
  fi
  log_ptt "recording popup failed; using notification only"

  if command -v dunstify >/dev/null 2>&1; then
    local notification_id=
    notification_id=$(dunstify \
      -a "uconsole-voice" \
      -r "${VOICE_RECORDING_NOTIFY_ID}" \
      -p \
      -u critical \
      -t 0 \
      -h "int:value:20" \
      "$(format_status_text "uconsole voice")" \
      "$(format_status_body "${body}")" 2>/dev/null || true)
    if [[ -n "${notification_id}" ]]; then
      printf '%s\n' "${notification_id}" >"${VOICE_STATE_DIR}/voice-recording-notification.id" || true
      log_ptt "recording notification started id=${notification_id}"
    fi
    return
  fi
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "uconsole voice" "${body}" >/dev/null 2>&1 || true
  fi
}

start_popup_watchdog() {
  local recorder_pid=$1
  local script_path=$2
  local popup_pid=

  popup_pid=$(recording_popup_pid || true)
  [[ -n "${popup_pid}" ]] || return 0
  (
    while [[ -f "${STATE_FILE}" ]]; do
      if ! kill -0 "${popup_pid}" >/dev/null 2>&1; then
        if [[ -f "${STATE_FILE}" ]]; then
          # shellcheck disable=SC1090
          source "${STATE_FILE}"
          if [[ "${RECORDER_PID:-}" == "${recorder_pid}" ]]; then
            VOICE_SUPPRESS_ASR_STATUS=1 "${script_path}" cancel "窗口已关闭"
          fi
        fi
        break
      fi
      sleep 0.2
    done
  ) >/dev/null 2>&1 &
  printf '%s\n' "$!"
}

escape_markup() {
  local text=${1:-}
  text=${text//&/&amp;}
  text=${text//</&lt;}
  text=${text//>/&gt;}
  printf '%s' "${text}"
}

format_status_text() {
  local text=${1:-}
  if [[ "${VOICE_NOTIFY_USE_MARKUP}" != "1" ]]; then
    printf '%s' "${text}"
    return
  fi

  text=$(escape_markup "${text}")
  printf '<span size="%s">%s</span>' "${VOICE_NOTIFY_FONT_SIZE_PANGO}" "${text}"
}

format_status_body() {
  local body=${1:-}
  if [[ -z "${body}" ]]; then
    printf '%s' ""
    return
  fi

  local text
  text=$(format_status_text "${body}")
  if (( VOICE_NOTIFY_PADDING_LINES > 0 )); then
    printf '\n%s' "${text}"
  else
    printf '%s' "${text}"
  fi
}

log_ptt() {
  mkdir -p "${VOICE_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/uconsole-helper-mapper}"
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" >>"${VOICE_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/uconsole-helper-mapper}/voice-ptt.log" 2>/dev/null || true
}

get_fcitx5_state() {
  command -v fcitx5-remote >/dev/null 2>&1 || return 1
  local state
  state=$(fcitx5-remote 2>/dev/null || true)
  [[ "${state}" =~ ^[012]$ ]] || return 1
  printf '%s\n' "${state}"
}

suspend_ime_for_injection() {
  IME_WAS_ACTIVE=0
  local state
  state=$(get_fcitx5_state || true)
  if [[ "${state}" == "2" ]]; then
    fcitx5-remote -c >/dev/null 2>&1 || true
    IME_WAS_ACTIVE=1
  fi
}

restore_ime_after_injection() {
  if [[ "${IME_WAS_ACTIVE}" == "1" ]]; then
    fcitx5-remote -o >/dev/null 2>&1 || true
  fi
  IME_WAS_ACTIVE=0
}

with_ime_suspended() {
  local status=0
  suspend_ime_for_injection
  "$@" || status=$?
  restore_ime_after_injection
  return "${status}"
}

type_text() {
  local text=$1
  wtype "${text}"
}

type_text_and_enter() {
  local text=$1
  wtype "${text}"
  wtype -k Return
}

paste_text() {
  local text=$1
  local shortcut=${2:-${VOICE_PASTE_SHORTCUT}}

  case "${VOICE_PASTE_BACKEND}" in
    auto)
      if command -v uconsole-paste >/dev/null 2>&1; then
        printf '%s' "${text}" | uconsole-paste "${shortcut}"
        return
      fi
      ;;
    uinput)
      command -v uconsole-paste >/dev/null 2>&1 || {
        echo "uconsole-paste is required for voice paste backend uinput" >&2
        return 1
      }
      printf '%s' "${text}" | uconsole-paste "${shortcut}"
      return
      ;;
    wtype)
      ;;
    *)
      echo "unsupported VOICE_PASTE_BACKEND: ${VOICE_PASTE_BACKEND}" >&2
      return 1
      ;;
  esac

  printf '%s' "${text}" | wl-copy
  sleep "${VOICE_PASTE_DELAY}"
  case "${shortcut}" in
    ctrl_v)
      wtype -M ctrl -k v -m ctrl
      ;;
    shift_insert)
      wtype -M shift -k Insert -m shift
      ;;
    *)
      echo "unsupported paste shortcut: ${shortcut}" >&2
      return 1
      ;;
  esac
}

fcitx_commit_text() {
  local text=$1

  mkdir -p "$(dirname -- "${VOICE_FCITX_COMMIT_FILE}")"
  printf '%s' "${text}" >"${VOICE_FCITX_COMMIT_FILE}"

  command -v wtype >/dev/null 2>&1 || {
    echo "wtype is required for voice output mode fcitx_commit" >&2
    return 1
  }
  wtype "${VOICE_FCITX_COMMIT_TRIGGER}"
}

run_whisper_curl() {
  local -a args=("$@")

  if [[ "${ASR_NO_PROXY}" == "1" ]]; then
    env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      -u http_proxy -u https_proxy -u all_proxy \
      curl --noproxy '*' "${args[@]}"
    return
  fi

  curl "${args[@]}"
}

trim() {
  sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

normalize_transcript() {
  tr '\r\n' '  ' | sed 's/[[:space:]]\+/ /g' | trim
}

sanitize_asr_context() {
  perl -CS -0pe 's/[^\p{L}\p{N}\s]+/ /g; s/\s+/ /g; s/^ //; s/ $//'
}

wait_for_exit() {
  local pid=$1
  local timeout=$2
  local elapsed=0
  while kill -0 "$pid" >/dev/null 2>&1; do
    if (( elapsed >= timeout )); then
      return 1
    fi
    sleep 0.1
    elapsed=$((elapsed + 1))
  done
  return 0
}

terminate_process_group() {
  local pid=$1
  local signal_name=${2:-TERM}

  [[ -n "${pid}" ]] || return 0
  kill -"${signal_name}" -- "-${pid}" >/dev/null 2>&1 || true
  kill -"${signal_name}" "${pid}" >/dev/null 2>&1 || true
}

terminate_matching_processes() {
  local pattern=$1
  local signal_name=${2:-TERM}
  local timeout=${3:-2}
  local pid=
  local pids=()

  while IFS= read -r pid; do
    [[ -n "${pid}" ]] || continue
    [[ "${pid}" != "$$" && "${pid}" != "${BASHPID}" && "${pid}" != "${PPID}" ]] || continue
    pids+=("${pid}")
  done < <(pgrep -f "${pattern}" 2>/dev/null || true)

  (( ${#pids[@]} > 0 )) || return 0
  for pid in "${pids[@]}"; do
    terminate_process_group "${pid}" "${signal_name}"
  done
  for pid in "${pids[@]}"; do
    wait_for_exit "${pid}" "${timeout}" || true
  done
}

regex_escape() {
  printf '%s' "$1" | sed 's/[][\\.^$*+?{}()|]/\\&/g'
}

close_recording_popup_instances() {
  local popup_text_file="${VOICE_STATE_DIR}/voice-recording-popup.txt"
  local escaped_text_file

  escaped_text_file=$(regex_escape "${popup_text_file}")
  terminate_matching_processes "uconsole-asr-popup .*${escaped_text_file}" TERM 2
  terminate_matching_processes "uconsole-asr-popup .*${escaped_text_file}" KILL 1
}

recording_popup_pid() {
  local popup_pid_file="${VOICE_STATE_DIR}/voice-recording-popup.pid"
  local popup_pid=

  [[ -f "${popup_pid_file}" ]] || return 1
  popup_pid=$(cat "${popup_pid_file}" 2>/dev/null || true)
  [[ -n "${popup_pid}" ]] || return 1
  printf '%s\n' "${popup_pid}"
}

write_recording_popup_text() {
  local message=$1
  local pulse=${2:-0}
  local popup_text_file="${VOICE_STATE_DIR}/voice-recording-popup.txt"
  local tmp_file="${popup_text_file}.$$"

  [[ ! -f "${VOICE_POPUP_DISMISSED_FILE}" ]] || return 0
  mkdir -p "${VOICE_STATE_DIR}" >/dev/null 2>&1 || true
  {
    printf '# %s\n' "${message}"
    if [[ "${pulse}" == "1" ]]; then
      printf '@pulse=1\n'
    fi
  } >"${tmp_file}" || return 0
  mv -f "${tmp_file}" "${popup_text_file}" >/dev/null 2>&1 || true
}

stop_orphan_recording_processes() {
  local state_dir_pattern

  state_dir_pattern=$(regex_escape "${VOICE_STATE_DIR}")
  terminate_matching_processes "uconsole-voice-stream.*${state_dir_pattern}" INT 2
  terminate_matching_processes "uconsole-voice-stream.*${state_dir_pattern}" TERM 2
  terminate_matching_processes "uconsole-voice-stream.*${state_dir_pattern}" KILL 1
  terminate_matching_processes "pw-record --rate ${VOICE_SAMPLE_RATE} --channels ${VOICE_CHANNELS}" TERM 2
  terminate_matching_processes "pw-record --rate ${VOICE_SAMPLE_RATE} --channels ${VOICE_CHANNELS}" KILL 1
}

stop_existing_recording_session() {
  [[ -f "${STATE_FILE}" ]] || return 0

  local RECORDER_PID=
  local STREAM_PID=
  local STREAM_STOP_FILE=
  local STREAM_RESULT_FILE=
  local WATCHDOG_PID=
  local POPUP_WATCH_PID=

  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  rm -f "${STATE_FILE}"

  if [[ -n "${WATCHDOG_PID:-}" && "${WATCHDOG_PID}" != "$$" ]]; then
    kill "${WATCHDOG_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${POPUP_WATCH_PID:-}" && "${POPUP_WATCH_PID}" != "$$" ]]; then
    kill "${POPUP_WATCH_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${STREAM_STOP_FILE:-}" ]]; then
    : >"${STREAM_STOP_FILE}" || true
  fi

  local pid=${STREAM_PID:-${RECORDER_PID:-}}
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    log_ptt "stopping existing voice session pid=${pid}"
    terminate_process_group "${pid}" INT
    wait_for_exit "${pid}" 20 || true
  fi
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    terminate_process_group "${pid}" TERM
    wait_for_exit "${pid}" 10 || true
  fi
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    terminate_process_group "${pid}" KILL
    wait_for_exit "${pid}" 10 || true
  fi

  rm -f "${STREAM_RESULT_FILE:-}" "${STREAM_STOP_FILE:-}"
}

choose_recorder() {
  case "${VOICE_RECORDER}" in
    auto)
      if command -v pw-record >/dev/null 2>&1; then
        echo "pw-record"
        return 0
      fi
      if command -v ffmpeg >/dev/null 2>&1; then
        echo "ffmpeg"
        return 0
      fi
      if command -v arecord >/dev/null 2>&1; then
        echo "arecord"
        return 0
      fi
      ;;
    pw-record|ffmpeg|arecord)
      if command -v "${VOICE_RECORDER}" >/dev/null 2>&1; then
        echo "${VOICE_RECORDER}"
        return 0
      fi
      echo "configured recorder not found: ${VOICE_RECORDER}" >&2
      return 1
      ;;
    *)
      echo "unsupported recorder: ${VOICE_RECORDER}" >&2
      return 1
      ;;
  esac

  echo "no supported recorder found; install pw-record, ffmpeg, or arecord" >&2
  return 1
}

resolve_stream_client() {
  local script_dir
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  local candidate
  for candidate in \
    "${script_dir}/uconsole-voice-stream" \
    "${script_dir}/uconsole-voice-stream.py" \
    "${HOME}/.local/bin/uconsole-voice-stream" \
    "${HOME}/.local/bin/uconsole-voice-stream.py"
  do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  if command -v uconsole-voice-stream >/dev/null 2>&1; then
    command -v uconsole-voice-stream
    return 0
  fi
  echo "uconsole-voice-stream is required" >&2
  return 1
}

terminal_window_is_focused() {
  local spec
  local specs=(
    "title:QuickTerm"
    "app_id:lxterminal"
    "app_id:QuickTerm"
    "app_id:quickterm"
  )

  [[ -x "${WLRCTL}" ]] || return 1

  for spec in "${specs[@]}"; do
    if "${WLRCTL}" window find "${spec}" "state:active" >/dev/null 2>&1; then
      return 0
    fi
  done

  return 1
}

terminal_window_is_active() {
  terminal_window_is_focused
}

wechat_window_is_focused() {
  local spec
  local specs=(
    "app_id:wechat"
    "app_id:WeChat"
    "app_id:wechat-uos"
    "app_id:com.tencent.WeChat"
    "title:微信"
    "title:WeChat"
  )

  [[ -x "${WLRCTL}" ]] || return 1

  for spec in "${specs[@]}"; do
    if "${WLRCTL}" window find "${spec}" "state:active" >/dev/null 2>&1; then
      return 0
    fi
  done

  return 1
}

resolve_tmux_window_target() {
  local best_activity=-1
  local best_window=
  local best_session=
  local control_mode activity session_name window_id

  command -v tmux >/dev/null 2>&1 || return 1

  while IFS=$'\t' read -r control_mode activity session_name window_id; do
    [[ "${control_mode}" == "1" ]] && continue
    [[ -n "${window_id}" ]] || continue
    [[ -n "${activity}" ]] || continue

    if (( activity > best_activity )); then
      best_activity=${activity}
      best_window=${window_id}
      best_session=${session_name}
    fi
  done < <(
    tmux list-clients -F '#{?client_control_mode,1,0}'$'\t''#{client_activity}'$'\t''#{session_name}'$'\t''#{window_id}' 2>/dev/null || true
  )

  [[ -n "${best_window}" ]] || return 1
  printf '%s\t%s\n' "${best_session}" "${best_window}"
}

capture_tmux_window_context() {
  local session_name window_id
  local window_name=
  local context=
  local pane_id=
  local pane_index=
  local pane_command=
  local pane_text

  [[ "${VOICE_TMUX_CONTEXT}" == "1" ]] || return 1
  IFS=$'\t' read -r session_name window_id < <(resolve_tmux_window_target) || return 1
  window_name=$(
    tmux display-message -p -t "${window_id}" '#{window_name}' 2>/dev/null | tr -d '\r' || true
  )

  context="tmux session: ${session_name:-unknown}"$'\n'
  context+="tmux window: ${window_name:-${window_id}}"$'\n'

  IFS=$'\t' read -r pane_id pane_index pane_command < <(
    tmux list-panes -t "${window_id}" -F '#{?pane_active,#{pane_id}'$'\t''#{pane_index}'$'\t''#{pane_current_command},}' 2>/dev/null \
      | awk 'NF { print; exit }'
  ) || true
  [[ -n "${pane_id}" ]] || return 1

  pane_text=$(
    tmux capture-pane -p -t "${pane_id}" 2>/dev/null | tr -d '\r' || true
  )
  local visible_line_count=0
  visible_line_count=$(printf '%s\n' "${pane_text}" | awk 'END { print NR }')
  if (( visible_line_count < VOICE_TMUX_CONTEXT_LINES )); then
    pane_text=$(
      tmux capture-pane -p -S "-${VOICE_TMUX_CONTEXT_LINES}" -t "${pane_id}" 2>/dev/null | tr -d '\r' || true
    )
  fi
  [[ -n "${pane_text}" ]] || return 1

  context+=$'\n'
  context+="[active pane ${pane_index:-?} command=${pane_command:-unknown}]"$'\n'
  context+="${pane_text}"$'\n'

  [[ -n "${context}" ]] || return 1

  context=$(printf '%s\n' "${context}" | sanitize_asr_context)
  [[ -n "${context}" ]] || return 1

  if (( ${#context} > VOICE_TMUX_CONTEXT_MAX_CHARS )); then
    context=${context: -$VOICE_TMUX_CONTEXT_MAX_CHARS}
  fi

  printf '%s\n' "${context}"
}

build_whisper_prompt() {
  [[ -n "${ASR_PROMPT}" ]] || return 1
  printf '%s\n' "${ASR_PROMPT}"
}

build_prompt_glossary_json() {
  local glossary_file=${VOICE_GLOSSARY_FILE:-}
  [[ -n "${glossary_file}" ]] || return 1
  [[ -f "${glossary_file}" ]] || return 1

  local -a terms=()
  local term
  local -A seen=()

  while IFS= read -r term || [[ -n "${term}" ]]; do
    term=$(printf '%s' "${term}" | trim)
    [[ -n "${term}" ]] || continue
    [[ "${term:0:1}" != "#" ]] || continue
    [[ -z "${seen[$term]+x}" ]] || continue
    seen[$term]=1
    terms+=("${term}")
  done <"${glossary_file}"

  (( ${#terms[@]} > 0 )) || return 1

  printf '%s\n' "${terms[@]}" | jq -Rn '{terms: [inputs | select(length > 0)]}'
}

build_whisper_context() {
  local tmux_context=
  tmux_context=$(capture_tmux_window_context || true)
  [[ -n "${tmux_context}" ]] || return 1
  printf '%s\n' "${tmux_context}"
}

start_recording() {
  mkdir -p "${VOICE_STATE_DIR}"
  log_ptt "start_recording begin"
  if [[ -f "${STATE_FILE}" ]]; then
    log_ptt "start_recording ignored: recording already active"
    return
  fi
  : >"${STARTING_FILE}" || true
  if [[ ! -f "${STATE_FILE}" ]] && recording_popup_active; then
    log_ptt "recording popup dismissed by short press"
    dismiss_recording_popup
    rm -f "${STARTING_FILE}" >/dev/null 2>&1 || true
    return
  fi
  close_recording_popup || true
  stop_existing_recording_session
  stop_orphan_recording_processes

  if [[ -z "${ASR_URL}" ]]; then
    echo "ASR_URL is required" >&2
    rm -f "${STARTING_FILE}" >/dev/null 2>&1 || true
    show_status "uconsole voice" "未配置 ASR Endpoint" "0" "1200"
    exit 1
  fi
  if [[ -z "${ASR_AUTH_TOKEN}" ]]; then
    echo "ASR_AUTH_TOKEN is required for FlashAI ASR" >&2
    rm -f "${STARTING_FILE}" >/dev/null 2>&1 || true
    show_status "uconsole voice" "未配置 ASR Token" "0" "1200"
    exit 1
  fi
  show_recording_status || log_ptt "show_recording_status returned nonzero"

  local context_text prompt_glossary_json
  log_ptt "building ASR context"
  context_text=$(build_whisper_context || true)
  prompt_glossary_json=$(build_prompt_glossary_json || true)

  local script_path=${BASH_SOURCE[0]}
  if [[ "${script_path}" != */* ]]; then
    script_path=$(command -v -- "${script_path}" || printf '%s' "${script_path}")
  fi
  script_path=$(readlink -f -- "${script_path}" 2>/dev/null || printf '%s' "${script_path}")

  local stream_client stream_result_file stream_stop_file stream_log_file
  stream_client=$(resolve_stream_client)
  stream_result_file=$(mktemp "${VOICE_STATE_DIR}/voice-stream-XXXXXX.json")
  stream_stop_file=$(mktemp "${VOICE_STATE_DIR}/voice-stream-stop-XXXXXX.flag")
  rm -f "${stream_stop_file}" "${stream_result_file}"
  stream_log_file="${VOICE_STATE_DIR}/voice-ptt.log"

  local stream_python=""
  local repo_root
  local asr_python_candidate
  repo_root=$(cd -- "${SCRIPT_DIR}/../.." && pwd)
  for asr_python_candidate in     "${repo_root}/.venv-asr/bin/python"     "${HOME}/WorkSpace/uconsole-helper/.venv-asr/bin/python"
  do
    if [[ -x "${asr_python_candidate}" ]]; then
      stream_python="${asr_python_candidate}"
      break
    fi
  done
  log_ptt "launching ASR stream client=${stream_client} python=${stream_python:-direct}"

  STREAM_RESULT_FILE="${stream_result_file}" \
    STREAM_STOP_FILE="${stream_stop_file}" \
    VOICE_STREAM_LOG_FILE="${stream_log_file}" \
    ASR_URL="${ASR_URL}" \
    ASR_LANGUAGE="${ASR_LANGUAGE}" \
    ASR_AUTH_TOKEN="${ASR_AUTH_TOKEN}" \
    ASR_TIMEOUT="${ASR_TIMEOUT}" \
    ASR_REQUEST_ATTEMPT_TIMEOUT="${ASR_REQUEST_ATTEMPT_TIMEOUT}" \
    ASR_CONNECT_TIMEOUT="${ASR_CONNECT_TIMEOUT}" \
    ASR_RETRY_COUNT="${ASR_RETRY_COUNT}" \
    ASR_RETRY_DELAY="${ASR_RETRY_DELAY}" \
    ASR_PREVIEW_WS_URL="${ASR_PREVIEW_WS_URL}" \
    ASR_FINALIZE_TEXT_URL="${ASR_FINALIZE_TEXT_URL}" \
    ASR_PREVIEW_FINAL_WAIT_SECONDS="${ASR_PREVIEW_FINAL_WAIT_SECONDS}" \
    ASR_PREVIEW_FINAL_STABLE_WAIT_SECONDS="${ASR_PREVIEW_FINAL_STABLE_WAIT_SECONDS}" \
    ASR_PREVIEW_WS_TIMEOUT="${ASR_PREVIEW_WS_TIMEOUT}" \
    ASR_PROMPT="${ASR_PROMPT}" \
    ASR_PROMPT_FIELD="${ASR_PROMPT_FIELD}" \
    ASR_PROMPT_GLOSSARY="${prompt_glossary_json}" \
    ASR_PROMPT_GLOSSARY_FIELD="${ASR_PROMPT_GLOSSARY_FIELD}" \
    ASR_CONTEXT_TEXT="${context_text}" \
    ASR_CONTEXT_FIELD="${ASR_CONTEXT_FIELD}" \
    ASR_CORRECTION_MODE="${ASR_CORRECTION_MODE}" \
    VOICE_RECORDER="${VOICE_RECORDER}" \
    VOICE_INPUT="${VOICE_INPUT}" \
    VOICE_SAMPLE_RATE="${VOICE_SAMPLE_RATE}" \
    VOICE_CHANNELS="${VOICE_CHANNELS}" \
    VOICE_STATE_DIR="${VOICE_STATE_DIR}" \
    VOICE_KEEP_AUDIO="${VOICE_KEEP_AUDIO}" \
    VOICE_KEEP_FAILED_AUDIO="${VOICE_KEEP_FAILED_AUDIO}" \
    VOICE_NOTIFY_ID="${VOICE_NOTIFY_ID}" \
    VOICE_RECORDING_NOTIFY_ID="${VOICE_RECORDING_NOTIFY_ID}" \
    VOICE_RECORDING_POPUP_TEXT_FILE="${VOICE_STATE_DIR}/voice-recording-popup.txt" \
    VOICE_RECORDING_POPUP_PID_FILE="${VOICE_STATE_DIR}/voice-recording-popup.pid" \
    VOICE_RECORDING_POPUP_DISMISSED_FILE="${VOICE_POPUP_DISMISSED_FILE}" \
    VOICE_STREAM_PREVIEW="${VOICE_STREAM_PREVIEW}" \
    VOICE_QWEN_ASR_STREAMING="${VOICE_QWEN_ASR_STREAMING}" \
    VOICE_STREAM_SEND_INTERVAL_MS="${VOICE_STREAM_SEND_INTERVAL_MS}" \
    VOICE_STREAM_NOTIFY_FROM_READER="${VOICE_STREAM_NOTIFY_FROM_READER}" \
    VOICE_STOP_DRAIN_MS="${VOICE_STOP_DRAIN_MS}" \
    VOICE_MAX_RECORD_MS="${VOICE_MAX_RECORD_MS}" \
    setsid ${stream_python:+"${stream_python}"} "${stream_client}" >/dev/null 2>&1 &

  local recorder_pid=$!
  log_ptt "ASR stream launched pid=${recorder_pid}"
  cat >"${STATE_FILE}" <<EOF
RECORDER_PID=${recorder_pid}
STREAM_PID=${recorder_pid}
STREAM_RESULT_FILE=$(printf '%q' "${stream_result_file}")
STREAM_STOP_FILE=$(printf '%q' "${stream_stop_file}")
RECORDER_NAME=stream
STARTED_AT_MS=$(date +%s%3N)
EOF
  rm -f "${STARTING_FILE}" >/dev/null 2>&1 || true
  log_ptt "recording state written pid=${recorder_pid} state=${STATE_FILE}"

  local popup_watch_pid=
  popup_watch_pid=$(start_popup_watchdog "${recorder_pid}" "${script_path}" || true)
  if [[ -n "${popup_watch_pid}" ]]; then
    printf 'POPUP_WATCH_PID=%s\n' "${popup_watch_pid}" >>"${STATE_FILE}"
  fi

  if (( VOICE_MAX_RECORD_MS > 0 )); then
    local watchdog_sleep_s=$(((VOICE_MAX_RECORD_MS + 999) / 1000))
    (
      sleep "${watchdog_sleep_s}"
      if [[ -f "${STATE_FILE}" ]]; then
        # shellcheck disable=SC1090
        source "${STATE_FILE}"
        if [[ "${RECORDER_PID:-}" == "${recorder_pid}" ]]; then
          VOICE_ROLLOVER_AFTER_STOP=1 VOICE_SUPPRESS_ASR_STATUS=1 "${script_path}" stop
        fi
      fi
    ) >/dev/null 2>&1 &
    local watchdog_pid=$!
    printf 'WATCHDOG_PID=%s\n' "${watchdog_pid}" >>"${STATE_FILE}"
  fi

  log_ptt "recording started pid=${recorder_pid}"
}

inject_text() {
  local text=$1
  local tmux_context=${2:-}
  local output_mode=${VOICE_OUTPUT_MODE}
  local paste_shortcut=${VOICE_PASTE_SHORTCUT}
  if [[ -n "${tmux_context}" ]] || terminal_window_is_focused; then
    output_mode=${VOICE_TMUX_OUTPUT_MODE}
  elif wechat_window_is_focused; then
    output_mode=${VOICE_WECHAT_OUTPUT_MODE}
    paste_shortcut=${VOICE_WECHAT_PASTE_SHORTCUT}
  elif [[ "${output_mode}" == "type" ]]; then
    output_mode=fcitx_commit
  fi

  case "${output_mode}" in
    type)
      command -v wtype >/dev/null 2>&1 || {
        echo "wtype is required for voice output mode type" >&2
        return 1
      }
      with_ime_suspended type_text "${text}"
      ;;
    type_enter)
      command -v wtype >/dev/null 2>&1 || {
        echo "wtype is required for voice output mode type_enter" >&2
        return 1
      }
      with_ime_suspended type_text_and_enter "${text}"
      ;;
    clipboard)
      command -v wl-copy >/dev/null 2>&1 || {
        echo "wl-copy is required for voice output mode clipboard" >&2
        return 1
      }
      printf '%s' "${text}" | wl-copy
      ;;
    paste)
      command -v wl-copy >/dev/null 2>&1 || {
        echo "wl-copy is required for voice output mode paste" >&2
        return 1
      }
      command -v wtype >/dev/null 2>&1 || {
        echo "wtype is required for voice output mode paste" >&2
        return 1
      }
      with_ime_suspended paste_text "${text}" "${paste_shortcut}"
      ;;
    fcitx_commit)
      fcitx_commit_text "${text}"
      ;;
    *)
      echo "unsupported voice output mode: ${output_mode}" >&2
      return 1
      ;;
  esac
}

stop_recording() {
  log_ptt "stop_recording begin state=$([[ -f "${STATE_FILE}" ]] && printf 1 || printf 0) starting=$([[ -f "${STARTING_FILE}" ]] && printf 1 || printf 0)"
  local wait_started=0
  while [[ ! -f "${STATE_FILE}" && -f "${STARTING_FILE}" && ${wait_started} -lt 50 ]]; do
    sleep 0.1
    wait_started=$((wait_started + 1))
  done
  if (( wait_started > 0 )); then
    log_ptt "stop_recording waited_for_start ticks=${wait_started} state=$([[ -f "${STATE_FILE}" ]] && printf 1 || printf 0)"
  fi
  if [[ ! -f "${STATE_FILE}" ]]; then
    close_recording_status
    exit 0
  fi

  local rollover_after_stop=${VOICE_ROLLOVER_AFTER_STOP:-0}
  local suppress_asr_status=${VOICE_SUPPRESS_ASR_STATUS:-0}

  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  rm -f "${STATE_FILE}"
  close_recording_notification
  if [[ "${suppress_asr_status}" != "1" ]]; then
    write_recording_popup_text "识别中" 1
  else
    close_recording_popup
  fi

  if [[ -n "${WATCHDOG_PID:-}" && "${WATCHDOG_PID}" != "$$" ]]; then
    kill "${WATCHDOG_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${POPUP_WATCH_PID:-}" && "${POPUP_WATCH_PID}" != "$$" ]]; then
    kill "${POPUP_WATCH_PID}" >/dev/null 2>&1 || true
  fi

  if [[ -z "${RECORDER_PID:-}" ]]; then
    echo "state file is incomplete" >&2
    exit 1
  fi

  local stopped_at_ms duration_ms
  stopped_at_ms=$(date +%s%3N)
  duration_ms=0
  if [[ -n "${STARTED_AT_MS:-}" ]]; then
    duration_ms=$((stopped_at_ms - STARTED_AT_MS))
  fi

  if [[ -n "${STREAM_STOP_FILE:-}" ]]; then
    log_ptt "stop_recording touch stop file=${STREAM_STOP_FILE}"
    : >"${STREAM_STOP_FILE}" || true
  fi
  if [[ "${suppress_asr_status}" != "1" ]]; then
    write_recording_popup_text "识别中" 1
  fi
  if kill -0 "${RECORDER_PID}" >/dev/null 2>&1; then
    if ! wait_for_exit "${RECORDER_PID}" 3; then
      log_ptt "stop_recording interrupt stream pid=${RECORDER_PID}"
      terminate_process_group "${RECORDER_PID}" INT
      if [[ "${suppress_asr_status}" != "1" ]]; then
        write_recording_popup_text "识别中" 1
      fi
      wait_for_exit "${RECORDER_PID}" 120 || true
    fi
    if kill -0 "${RECORDER_PID}" >/dev/null 2>&1; then
      terminate_process_group "${RECORDER_PID}" TERM
      wait_for_exit "${RECORDER_PID}" 10 || true
    fi
    if kill -0 "${RECORDER_PID}" >/dev/null 2>&1; then
      terminate_process_group "${RECORDER_PID}" KILL
      wait_for_exit "${RECORDER_PID}" 10 || true
    fi
  fi

  if [[ "${rollover_after_stop}" == "1" ]]; then
    start_recording
  fi

  if (( duration_ms < VOICE_MIN_RECORD_MS )); then
    log_ptt "stop_recording too short durationMs=${duration_ms} minMs=${VOICE_MIN_RECORD_MS}"
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_recording_popup_message_then_close "录音太短，已取消" 2
    else
      close_recording_status
    fi
    rm -f "${STREAM_RESULT_FILE:-}" "${STREAM_STOP_FILE:-}"
    exit 0
  fi

  if [[ "${suppress_asr_status}" != "1" ]]; then
    write_recording_popup_text "识别中" 1
  fi

  if [[ ! -s "${STREAM_RESULT_FILE:-}" ]]; then
    echo "ASR result is empty" >&2
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_recording_popup_message_then_close "语音识别失败" 2
    else
      close_recording_status
    fi
    rm -f "${STREAM_RESULT_FILE:-}" "${STREAM_STOP_FILE:-}"
    exit 1
  fi

  command -v jq >/dev/null 2>&1 || {
    echo "jq is required" >&2
    exit 1
  }

  local context_text=
  context_text=$(build_whisper_context || true)

  local stream_status stream_error
  if [[ "${suppress_asr_status}" != "1" ]]; then
    write_recording_popup_text "识别中" 1
  fi
  stream_status=$(jq -r '.status // empty' "${STREAM_RESULT_FILE}")
  stream_error=$(jq -r '.error // empty' "${STREAM_RESULT_FILE}")
  if [[ "${stream_status}" == "error" ]]; then
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_recording_popup_message_then_close "${stream_error:-语音识别失败}" 2
    else
      close_recording_status
    fi
    rm -f "${STREAM_RESULT_FILE:-}" "${STREAM_STOP_FILE:-}"
    exit 1
  fi

  local text
  text=$(jq -r '.text // empty' "${STREAM_RESULT_FILE}" | normalize_transcript)

  if [[ -z "${text}" ]]; then
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_recording_popup_message_then_close "未识别到文本" 2
    else
      close_recording_status
    fi
    rm -f "${STREAM_RESULT_FILE:-}" "${STREAM_STOP_FILE:-}"
    exit 1
  fi

  if ! inject_text "${text}" "${context_text}"; then
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_recording_popup_message_then_close "文本注入失败" 2
    else
      close_recording_status
    fi
    rm -f "${STREAM_RESULT_FILE:-}" "${STREAM_STOP_FILE:-}"
    exit 1
  fi

  if [[ "${suppress_asr_status}" != "1" ]]; then
    close_status
  fi
  log_ptt "stop_recording finished durationMs=${duration_ms}"
  rm -f "${STREAM_RESULT_FILE:-}" "${STREAM_STOP_FILE:-}"
}

cancel_recording() {
  local message=${1:-"已取消"}
  if [[ ! -f "${STATE_FILE}" ]]; then
    close_recording_status
    exit 0
  fi

  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  rm -f "${STATE_FILE}"
  close_recording_status

  if [[ -n "${WATCHDOG_PID:-}" && "${WATCHDOG_PID}" != "$$" ]]; then
    kill "${WATCHDOG_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${POPUP_WATCH_PID:-}" && "${POPUP_WATCH_PID}" != "$$" ]]; then
    kill "${POPUP_WATCH_PID}" >/dev/null 2>&1 || true
  fi

  if [[ -n "${STREAM_STOP_FILE:-}" ]]; then
    : >"${STREAM_STOP_FILE}" || true
  fi
  if [[ -n "${RECORDER_PID:-}" ]] && kill -0 "${RECORDER_PID}" >/dev/null 2>&1; then
    terminate_process_group "${RECORDER_PID}" INT
    if ! wait_for_exit "${RECORDER_PID}" 20; then
      terminate_process_group "${RECORDER_PID}" TERM
      wait_for_exit "${RECORDER_PID}" 10 || true
    fi
    if kill -0 "${RECORDER_PID}" >/dev/null 2>&1; then
      terminate_process_group "${RECORDER_PID}" KILL
      wait_for_exit "${RECORDER_PID}" 10 || true
    fi
  fi

  rm -f "${STREAM_RESULT_FILE:-}" "${STREAM_STOP_FILE:-}"
  show_status "uconsole voice" "${message}" "0" "800"
}

ACTION=${1:-}
if [[ "${ACTION}" == "-h" || "${ACTION}" == "--help" || -z "${ACTION}" ]]; then
  usage
  exit 0
fi
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

CONFIG_FILE=${VOICE_PTT_CONFIG:-"${HOME}/.config/uconsole-helper-mapper/voice.env"}
if [[ -f "${CONFIG_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi

VOICE_GLOSSARY_FILE=${VOICE_GLOSSARY_FILE:-"${HOME}/.config/uconsole-helper-mapper/voice-glossary.txt"}
VOICE_STATE_DIR=${VOICE_STATE_DIR:-"${XDG_STATE_HOME:-${HOME}/.local/state}/uconsole-helper-mapper"}
STATE_FILE="${VOICE_STATE_DIR}/voice-ptt.state"
STARTING_FILE="${VOICE_STATE_DIR}/voice-ptt.starting"
VOICE_POPUP_DISMISSED_FILE="${VOICE_STATE_DIR}/voice-recording-popup.dismissed"
VOICE_RECORDER=${VOICE_RECORDER:-auto}
VOICE_INPUT=${VOICE_INPUT:-default}
VOICE_MIN_RECORD_MS=${VOICE_MIN_RECORD_MS:-350}
VOICE_MAX_RECORD_MS=${VOICE_MAX_RECORD_MS:-60000}
VOICE_SAMPLE_RATE=${VOICE_SAMPLE_RATE:-16000}
VOICE_CHANNELS=${VOICE_CHANNELS:-1}
VOICE_OUTPUT_MODE=${VOICE_OUTPUT_MODE:-type}
VOICE_TMUX_OUTPUT_MODE=${VOICE_TMUX_OUTPUT_MODE:-type}
VOICE_WECHAT_OUTPUT_MODE=${VOICE_WECHAT_OUTPUT_MODE:-paste}
VOICE_PASTE_SHORTCUT=${VOICE_PASTE_SHORTCUT:-shift_insert}
VOICE_WECHAT_PASTE_SHORTCUT=${VOICE_WECHAT_PASTE_SHORTCUT:-ctrl_v}
VOICE_PASTE_BACKEND=${VOICE_PASTE_BACKEND:-auto}
VOICE_PASTE_DELAY=${VOICE_PASTE_DELAY:-0.08}
VOICE_FCITX_COMMIT_FILE=${VOICE_FCITX_COMMIT_FILE:-"${VOICE_STATE_DIR}/fcitx-voice-commit.txt"}
VOICE_FCITX_COMMIT_TRIGGER=${VOICE_FCITX_COMMIT_TRIGGER:-";uv"}
VOICE_KEEP_AUDIO=${VOICE_KEEP_AUDIO:-0}
VOICE_NOTIFY_ID=${VOICE_NOTIFY_ID:-991199}
VOICE_RECORDING_NOTIFY_ID=${VOICE_RECORDING_NOTIFY_ID:-991200}
VOICE_NOTIFY_USE_MARKUP=${VOICE_NOTIFY_USE_MARKUP:-0}
VOICE_NOTIFY_FONT_SIZE=${VOICE_NOTIFY_FONT_SIZE:-22}
VOICE_NOTIFY_PADDING_LINES=${VOICE_NOTIFY_PADDING_LINES:-1}
VOICE_NOTIFY_FONT_SIZE_PANGO=$((VOICE_NOTIFY_FONT_SIZE * 1000))
VOICE_TMUX_CONTEXT=${VOICE_TMUX_CONTEXT:-1}
VOICE_TMUX_CONTEXT_LINES=${VOICE_TMUX_CONTEXT_LINES:-30}
VOICE_TMUX_CONTEXT_MAX_CHARS=${VOICE_TMUX_CONTEXT_MAX_CHARS:-1200}
VOICE_KEEP_FAILED_AUDIO=${VOICE_KEEP_FAILED_AUDIO:-1}
WLRCTL=${WLRCTL:-"${HOME}/.local/bin/wlrctl"}
ASR_URL=${ASR_URL:-}
ASR_LANGUAGE=${ASR_LANGUAGE:-}
ASR_AUTH_TOKEN=${ASR_AUTH_TOKEN:-}
ASR_PROMPT=${ASR_PROMPT:-}
ASR_PROMPT_FIELD=${ASR_PROMPT_FIELD:-prompt}
ASR_PROMPT_GLOSSARY_FIELD=${ASR_PROMPT_GLOSSARY_FIELD:-promptGlossary}
ASR_CONTEXT_FIELD=${ASR_CONTEXT_FIELD:-contextText}
ASR_CORRECTION_MODE=${ASR_CORRECTION_MODE:-}
if [[ -z "${ASR_CORRECTION_MODE}" ]]; then
  ASR_CORRECTION_MODE=auto
fi
ASR_NO_PROXY=${ASR_NO_PROXY:-1}
ASR_TIMEOUT=${ASR_TIMEOUT:-90}
ASR_REQUEST_ATTEMPT_TIMEOUT=${ASR_REQUEST_ATTEMPT_TIMEOUT:-75}
ASR_CONNECT_TIMEOUT=${ASR_CONNECT_TIMEOUT:-2}
ASR_RETRY_COUNT=${ASR_RETRY_COUNT:-2}
ASR_RETRY_DELAY=${ASR_RETRY_DELAY:-0.35}
ASR_PREVIEW_WS_URL=${ASR_PREVIEW_WS_URL:-}
ASR_FINALIZE_TEXT_URL=${ASR_FINALIZE_TEXT_URL:-}
ASR_PREVIEW_FINAL_WAIT_SECONDS=${ASR_PREVIEW_FINAL_WAIT_SECONDS:-1.5}
ASR_PREVIEW_FINAL_STABLE_WAIT_SECONDS=${ASR_PREVIEW_FINAL_STABLE_WAIT_SECONDS:-0.5}
ASR_PREVIEW_WS_TIMEOUT=${ASR_PREVIEW_WS_TIMEOUT:-2}
VOICE_STREAM_PREVIEW=${VOICE_STREAM_PREVIEW:-1}
VOICE_QWEN_ASR_STREAMING=${VOICE_QWEN_ASR_STREAMING:-1}
VOICE_NOTIFY_WHILE_PREVIEW=${VOICE_NOTIFY_WHILE_PREVIEW:-0}
VOICE_STREAM_SEND_INTERVAL_MS=${VOICE_STREAM_SEND_INTERVAL_MS:-50}
VOICE_STREAM_NOTIFY_FROM_READER=${VOICE_STREAM_NOTIFY_FROM_READER:-1}
VOICE_STOP_DRAIN_MS=${VOICE_STOP_DRAIN_MS:-250}

case "${ACTION}" in
  start)
    start_recording
    ;;
  stop)
    stop_recording
    ;;
  cancel)
    cancel_recording "${2:-已取消}"
    ;;
  *)
    echo "unknown action: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac
