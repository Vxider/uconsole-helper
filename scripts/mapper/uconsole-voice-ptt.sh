#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

IME_WAS_ACTIVE=0

usage() {
  cat <<'EOF'
Usage:
  uconsole-voice-ptt start
  uconsole-voice-ptt stop
  uconsole-voice-ptt cancel
  uconsole-voice-ptt learn

Configuration is read from:
  $VOICE_PTT_CONFIG
  ~/.config/uconsole-helper-mapper/voice.env

Supported variables:
  WHISPER_URL            required, whisper endpoint
  WHISPER_LANGUAGE       optional multipart field
  WHISPER_AUTH_TOKEN     required for FlashAI ASR, bearer token with asr:transcribe and asr:learn
  WHISPER_FINALIZE_URL   optional finalize endpoint; defaults from WHISPER_URL
  WHISPER_PROMPT         optional short ASR prompt hint
  WHISPER_PROMPT_FIELD   multipart field for ASR prompt, default: prompt
  WHISPER_PROMPT_GLOSSARY_FIELD
                         multipart field for prompt glossary JSON, default: promptGlossary
  VOICE_GLOSSARY_FILE    glossary file path, one term per line; default:
                         ~/.config/uconsole-helper-mapper/voice-glossary.txt
  WHISPER_CONTEXT_FIELD  multipart field for tmux context, default: contextText
  WHISPER_CORRECTION_MODE
                         off | on | auto, default: auto
  WHISPER_ENABLE_CORRECTION
                         legacy compatibility only; 1 maps to correctionMode=on, 0 maps to off
  WHISPER_TEXT_JQ        jq expression, default: .data.text // .text // .result.text // empty
  WHISPER_NO_PROXY       1 disables proxy for whisper requests, default: 1
  WHISPER_TIMEOUT        ASR request timeout in seconds, default: 60; 0 disables
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
  VOICE_LEARN_MAX_AGE_SECONDS
                        max age for the last ASR state used by learn, default: 600
  VOICE_LEARN_MAX_EDIT_RATIO
                        max allowed correction edit ratio, default: 0.38
  VOICE_LEARN_REPLACE_INPUT
                        1 replaces the last inserted ASR text after correction, default: 1
  VOICE_LEARN_REPLACE_MAX_CHARS
                        max chars deleted when replacing ASR text, default: 300
  VOICE_LEARN_DIALOG_FONT_SIZE
                        correction dialog font size, default: 22
  VOICE_LEARN_DIALOG_COMMAND
                        custom correction dialog command, default: ~/.local/bin/uconsole-asr-correction-dialog
  VOICE_LEARN_DIALOG_WIDTH / VOICE_LEARN_DIALOG_HEIGHT
                        correction dialog size, defaults: 820 / 220
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

close_status() {
  if command -v dunstify >/dev/null 2>&1; then
    dunstify -C "${VOICE_NOTIFY_ID}" >/dev/null 2>&1 || true
  fi
}

show_recording_status() {
  local body="录音中..."

  if command -v dunstify >/dev/null 2>&1; then
    dunstify \
      -a "uconsole-voice" \
      -r "${VOICE_NOTIFY_ID}" \
      -u low \
      -t 0 \
      -h "int:value:20" \
      "$(format_status_text "uconsole voice")" \
      "$(format_status_body "${body}")" >/dev/null 2>&1 || true
    return
  fi
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "uconsole voice" "${body}" >/dev/null 2>&1 || true
  fi
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

  if [[ "${WHISPER_NO_PROXY}" == "1" ]]; then
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

normalize_learn_text() {
  perl -CS -pe 's/\e\[[0-9;?]*[ -\/]*[@-~]//g; s/\r//g' | sed 's/[[:space:]]\+$//'
}

derive_asr_finalize_url() {
  local request_id=$1
  [[ -n "${request_id}" ]] || return 1
  if [[ -n "${WHISPER_FINALIZE_URL}" ]]; then
    printf '%s\n' "${WHISPER_FINALIZE_URL//\{requestId\}/${request_id}}"
    return 0
  fi
  case "${WHISPER_URL}" in
    */api/asr/transcriptions)
      printf '%s\n' "${WHISPER_URL%/api/asr/transcriptions}/api/asr/transcription-events/${request_id}/finalize"
      ;;
    *)
      return 1
      ;;
  esac
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
  [[ -n "${WHISPER_PROMPT}" ]] || return 1
  printf '%s\n' "${WHISPER_PROMPT}"
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

current_tmux_target_json() {
  [[ "${VOICE_TMUX_CONTEXT}" == "1" ]] || return 1
  local session_name window_id pane_id pane_index pane_command
  IFS=$'\t' read -r session_name window_id < <(resolve_tmux_window_target) || return 1
  IFS=$'\t' read -r pane_id pane_index pane_command < <(
    tmux list-panes -t "${window_id}" -F '#{?pane_active,#{pane_id}'$'\t''#{pane_index}'$'\t''#{pane_current_command},}' 2>/dev/null \
      | awk 'NF { print; exit }'
  ) || true
  [[ -n "${pane_id}" ]] || return 1
  jq -n \
    --arg sessionName "${session_name:-}" \
    --arg windowId "${window_id:-}" \
    --arg paneId "${pane_id:-}" \
    --arg paneIndex "${pane_index:-}" \
    --arg paneCommand "${pane_command:-}" \
    '{sessionName:$sessionName, windowId:$windowId, paneId:$paneId, paneIndex:$paneIndex, paneCommand:$paneCommand}'
}

capture_tmux_pane_text_for_learn() {
  local pane_id=${1:-}
  [[ -n "${pane_id}" ]] || return 1
  tmux capture-pane -p -J -S "-${VOICE_LEARN_CAPTURE_LINES}" -t "${pane_id}" 2>/dev/null | normalize_learn_text
}

save_last_asr_state() {
  local request_id=$1
  local inserted_text=$2
  local raw_text=$3
  local corrected_text=$4
  local before_text=$5
  local after_text=$6
  [[ -n "${request_id}" ]] || return 0
  local target_json pane_id session_name window_id pane_command
  target_json=$(current_tmux_target_json || true)
  if [[ -z "${target_json}" ]]; then
    pane_id=
    session_name=
    window_id=
    pane_command=
    log_ptt "save_last_asr_state: no tmux target requestId=${request_id} insertedChars=${#inserted_text}"
  else
    pane_id=$(jq -r '.paneId // empty' <<<"${target_json}")
    session_name=$(jq -r '.sessionName // empty' <<<"${target_json}")
    window_id=$(jq -r '.windowId // empty' <<<"${target_json}")
    pane_command=$(jq -r '.paneCommand // empty' <<<"${target_json}")
  fi
  mkdir -p "${VOICE_STATE_DIR}"
  jq -n \
    --arg requestId "${request_id}" \
    --arg insertedText "${inserted_text}" \
    --arg rawText "${raw_text}" \
    --arg correctedText "${corrected_text}" \
    --arg paneId "${pane_id}" \
    --arg sessionName "${session_name}" \
    --arg windowId "${window_id}" \
    --arg paneCommand "${pane_command}" \
    --arg beforePaneText "${before_text}" \
    --arg afterPaneText "${after_text}" \
    --argjson createdAt "$(date +%s)" \
    '{requestId:$requestId, insertedText:$insertedText, rawText:$rawText, correctedText:$correctedText, paneId:$paneId, sessionName:$sessionName, windowId:$windowId, paneCommand:$paneCommand, beforePaneText:$beforePaneText, afterPaneText:$afterPaneText, createdAt:$createdAt}' \
    >"${LAST_ASR_STATE_FILE}"
  log_ptt "saved last ASR state requestId=${request_id} pane=${pane_id} insertedChars=${#inserted_text}"
}

start_recording() {
  mkdir -p "${VOICE_STATE_DIR}"
  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${STATE_FILE}"
    if [[ -n "${RECORDER_PID:-}" ]] && kill -0 "${RECORDER_PID}" >/dev/null 2>&1; then
      exit 0
    fi
    rm -f "${STATE_FILE}"
  fi

  local recorder
  recorder=$(choose_recorder)
  local audio_file
  audio_file=$(mktemp "${VOICE_STATE_DIR}/voice-XXXXXX.wav")

  case "${recorder}" in
    pw-record)
      local -a pw_record_args=(
        --rate "${VOICE_SAMPLE_RATE}"
        --channels "${VOICE_CHANNELS}"
      )
      if [[ "${VOICE_INPUT}" != "default" ]]; then
        pw_record_args+=(--target "${VOICE_INPUT}")
      fi
      setsid pw-record \
        "${pw_record_args[@]}" \
        "${audio_file}" >/dev/null 2>&1 &
      ;;
    ffmpeg)
      setsid ffmpeg \
        -hide_banner \
        -loglevel error \
        -y \
        -f pulse \
        -i "${VOICE_INPUT}" \
        -ac "${VOICE_CHANNELS}" \
        -ar "${VOICE_SAMPLE_RATE}" \
        "${audio_file}" >/dev/null 2>&1 &
      ;;
    arecord)
      local -a arecord_args=(
        -q
        -f S16_LE
        -r "${VOICE_SAMPLE_RATE}"
        -c "${VOICE_CHANNELS}"
      )
      if [[ "${VOICE_INPUT}" != "default" ]]; then
        arecord_args+=(-D "${VOICE_INPUT}")
      fi
      setsid arecord \
        "${arecord_args[@]}" \
        "${audio_file}" >/dev/null 2>&1 &
      ;;
  esac

  local recorder_pid=$!
  cat >"${STATE_FILE}" <<EOF
RECORDER_PID=${recorder_pid}
AUDIO_FILE=$(printf '%q' "${audio_file}")
RECORDER_NAME=${recorder}
STARTED_AT_MS=$(date +%s%3N)
EOF

  if (( VOICE_MAX_RECORD_MS > 0 )); then
    local watchdog_sleep_s=$(((VOICE_MAX_RECORD_MS + 999) / 1000))
    local script_path=${BASH_SOURCE[0]}
    if [[ "${script_path}" != */* ]]; then
      script_path=$(command -v -- "${script_path}" || printf '%s' "${script_path}")
    fi
    script_path=$(readlink -f -- "${script_path}" 2>/dev/null || printf '%s' "${script_path}")
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

  show_recording_status
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
  if [[ ! -f "${STATE_FILE}" ]]; then
    exit 0
  fi

  local rollover_after_stop=${VOICE_ROLLOVER_AFTER_STOP:-0}
  local suppress_asr_status=${VOICE_SUPPRESS_ASR_STATUS:-0}

  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  rm -f "${STATE_FILE}"

  if [[ -n "${WATCHDOG_PID:-}" && "${WATCHDOG_PID}" != "$$" ]]; then
    kill "${WATCHDOG_PID}" >/dev/null 2>&1 || true
  fi

  if [[ -z "${RECORDER_PID:-}" || -z "${AUDIO_FILE:-}" ]]; then
    echo "state file is incomplete" >&2
    exit 1
  fi

  local stopped_at_ms duration_ms
  stopped_at_ms=$(date +%s%3N)
  duration_ms=0
  if [[ -n "${STARTED_AT_MS:-}" ]]; then
    duration_ms=$((stopped_at_ms - STARTED_AT_MS))
  fi

  if kill -0 "${RECORDER_PID}" >/dev/null 2>&1; then
    kill -INT "${RECORDER_PID}" >/dev/null 2>&1 || true
    if ! wait_for_exit "${RECORDER_PID}" 30; then
      kill -TERM "${RECORDER_PID}" >/dev/null 2>&1 || true
      wait_for_exit "${RECORDER_PID}" 20 || true
    fi
  fi

  if [[ "${rollover_after_stop}" == "1" ]]; then
    start_recording
  fi

  if (( duration_ms < VOICE_MIN_RECORD_MS )); then
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_status "uconsole voice" "录音太短，已取消" "0" "800"
    fi
    [[ "${VOICE_KEEP_AUDIO}" == "1" ]] || rm -f "${AUDIO_FILE}"
    exit 0
  fi

  if [[ ! -s "${AUDIO_FILE}" ]]; then
    echo "recorded audio is empty" >&2
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_status "uconsole voice" "录音失败" "0" "1000"
    fi
    [[ "${VOICE_KEEP_AUDIO}" == "1" ]] || rm -f "${AUDIO_FILE}"
    exit 1
  fi

  if [[ -z "${WHISPER_URL}" ]]; then
    echo "WHISPER_URL is required" >&2
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_status "uconsole voice" "未配置 WHISPER_URL" "0" "1200"
    fi
    exit 1
  fi

  if [[ -z "${WHISPER_AUTH_TOKEN}" ]]; then
    echo "WHISPER_AUTH_TOKEN is required for FlashAI ASR" >&2
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_status "uconsole voice" "未配置 ASR Token" "0" "1200"
    fi
    exit 1
  fi

  command -v curl >/dev/null 2>&1 || {
    echo "curl is required" >&2
    exit 1
  }
  command -v jq >/dev/null 2>&1 || {
    echo "jq is required" >&2
    exit 1
  }

  local response_file
  response_file=$(mktemp "${VOICE_STATE_DIR}/whisper-XXXXXX.json")

  local prompt_text=
  local prompt_glossary_json=
  local context_text=
  local correction_mode=
  local before_pane_text=
  before_pane_text=$(capture_tmux_pane_text_for_learn "$(current_tmux_target_json 2>/dev/null | jq -r '.paneId // empty' 2>/dev/null || true)" || true)
  local -a curl_args=(
    -fsS
    --max-time "${WHISPER_TIMEOUT}"
    -X POST
    "${WHISPER_URL}"
    -F "file=@${AUDIO_FILE}"
  )
  curl_args+=(-H "Authorization: Bearer ${WHISPER_AUTH_TOKEN}")
  if [[ -n "${WHISPER_LANGUAGE}" ]]; then
    curl_args+=(-F "language=${WHISPER_LANGUAGE}")
  fi
  prompt_text=$(build_whisper_prompt || true)
  if [[ -n "${prompt_text}" ]]; then
    curl_args+=(--form-string "${WHISPER_PROMPT_FIELD}=${prompt_text}")
  fi
  prompt_glossary_json=$(build_prompt_glossary_json || true)
  if [[ -n "${prompt_glossary_json}" ]]; then
    curl_args+=(--form-string "${WHISPER_PROMPT_GLOSSARY_FIELD}=${prompt_glossary_json}")
  fi
  context_text=$(build_whisper_context || true)
  if [[ -n "${context_text}" ]]; then
    curl_args+=(--form-string "${WHISPER_CONTEXT_FIELD}=${context_text}")
  fi
  correction_mode=${WHISPER_CORRECTION_MODE}
  if [[ -n "${correction_mode}" ]]; then
    curl_args+=(-F "correctionMode=${correction_mode}")
  elif [[ "${WHISPER_ENABLE_CORRECTION}" == "1" ]]; then
    curl_args+=(-F "enableCorrection=true")
  fi
  if [[ "${suppress_asr_status}" != "1" ]]; then
    show_status "uconsole voice" "识别中..." "65" "0"
  fi
  local curl_status=0
  run_whisper_curl "${curl_args[@]}" >"${response_file}" || curl_status=$?
  if (( curl_status != 0 )); then
    if [[ "${suppress_asr_status}" != "1" ]]; then
      if (( curl_status == 28 )); then
        show_status "uconsole voice" "识别超时" "0" "1200"
      else
        show_status "uconsole voice" "Whisper 请求失败" "0" "1200"
      fi
    fi
    rm -f "${response_file}"
    [[ "${VOICE_KEEP_AUDIO}" == "1" ]] || rm -f "${AUDIO_FILE}"
    exit 1
  fi

  local text request_id raw_text corrected_text
  text=$(jq -r "${WHISPER_TEXT_JQ}" "${response_file}" | normalize_transcript)
  request_id=$(jq -r '.data.requestId // .requestId // empty' "${response_file}" | normalize_transcript)
  raw_text=$(jq -r '.data.rawText // .rawText // empty' "${response_file}" | normalize_transcript)
  corrected_text=$(jq -r '.data.correctedText // .correctedText // empty' "${response_file}" | normalize_transcript)
  rm -f "${response_file}"

  if [[ -z "${text}" ]]; then
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_status "uconsole voice" "未识别到文本" "0" "1000"
    fi
    [[ "${VOICE_KEEP_AUDIO}" == "1" ]] || rm -f "${AUDIO_FILE}"
    exit 1
  fi

  if ! inject_text "${text}" "${context_text}"; then
    if [[ "${suppress_asr_status}" != "1" ]]; then
      show_status "uconsole voice" "文本注入失败" "0" "1200"
    fi
    [[ "${VOICE_KEEP_AUDIO}" == "1" ]] || rm -f "${AUDIO_FILE}"
    exit 1
  fi

  local after_pane_text=
  after_pane_text=$(capture_tmux_pane_text_for_learn "$(current_tmux_target_json 2>/dev/null | jq -r '.paneId // empty' 2>/dev/null || true)" || true)
  if [[ -n "${request_id}" ]]; then
    save_last_asr_state "${request_id}" "${text}" "${raw_text}" "${corrected_text}" "${before_pane_text}" "${after_pane_text}"
  else
    log_ptt "skip save_last_asr_state: ASR response missing requestId insertedChars=${#text}"
  fi

  if [[ "${suppress_asr_status}" != "1" ]]; then
    close_status
  fi
  [[ "${VOICE_KEEP_AUDIO}" == "1" ]] || rm -f "${AUDIO_FILE}"
}

open_asr_correction_editor() {
  local initial_text=$1
  local title=${2:-"修正语音输入"}

  log_ptt "open ASR correction editor chars=${#initial_text}"

  local custom_editor="${VOICE_LEARN_DIALOG_COMMAND:-${HOME}/.local/bin/uconsole-asr-correction-dialog}"
  if [[ -x "${custom_editor}" ]]; then
    QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-wayland} "${custom_editor}" "${initial_text}" "${title}"
    return $?
  fi
  if command -v uconsole-asr-correction-dialog >/dev/null 2>&1; then
    QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-wayland} uconsole-asr-correction-dialog "${initial_text}" "${title}"
    return $?
  fi

  if command -v kdialog >/dev/null 2>&1; then
    QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-wayland} kdialog --title "${title}" --inputbox "修正上次语音识别文本" "${initial_text}"
    return $?
  fi

  if command -v zenity >/dev/null 2>&1; then
    zenity --entry \
      --title="${title}" \
      --text="修正上次语音识别文本，按回车确认" \
      --entry-text="${initial_text}"
    return $?
  fi

  echo "zenity or kdialog is required for ASR correction editor" >&2
  return 127
}

compute_edit_ratio() {
  python3 - "$1" "$2" <<'PYRATIO'
import difflib, sys
old = sys.argv[1].strip()
new = sys.argv[2].strip()
print(1.0 - difflib.SequenceMatcher(None, old, new).ratio())
PYRATIO
}

replace_last_inserted_text() {
  local old_text=$1
  local new_text=$2
  [[ "${VOICE_LEARN_REPLACE_INPUT}" == "1" ]] || return 0
  [[ "${old_text}" != "${new_text}" ]] || return 0
  command -v wtype >/dev/null 2>&1 || return 1

  local count=${#old_text}
  if (( count < 1 || count > VOICE_LEARN_REPLACE_MAX_CHARS )); then
    log_ptt "skip replace_last_inserted_text: old text length out of range chars=${count}"
    return 1
  fi

  suspend_ime_for_injection
  local i
  for ((i = 0; i < count; i++)); do
    wtype -k BackSpace
  done
  wtype "${new_text}"
  restore_ime_after_injection
}

learn_last_asr() {
  if [[ -f "${STATE_FILE}" ]]; then
    cancel_recording "已取消短按录音" || true
  fi
  command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }
  command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
  if [[ -z "${WHISPER_AUTH_TOKEN}" ]]; then
    show_status "uconsole voice" "未配置 ASR Token" "0" "1200"
    exit 1
  fi
  if [[ ! -s "${LAST_ASR_STATE_FILE}" ]]; then
    show_status "uconsole voice" "没有可学习的语音输入" "0" "1000"
    exit 1
  fi

  local request_id inserted_text created_at now age final_text finalize_url edit_ratio
  request_id=$(jq -r '.requestId // empty' "${LAST_ASR_STATE_FILE}")
  inserted_text=$(jq -r '.insertedText // empty' "${LAST_ASR_STATE_FILE}")
  created_at=$(jq -r '.createdAt // 0' "${LAST_ASR_STATE_FILE}")
  now=$(date +%s)
  age=$((now - created_at))

  if [[ -z "${request_id}" || -z "${inserted_text}" ]]; then
    show_status "uconsole voice" "语音输入状态不完整，未学习" "0" "1000"
    exit 1
  fi
  if (( age < 0 || age > VOICE_LEARN_MAX_AGE_SECONDS )); then
    show_status "uconsole voice" "语音输入已过期，未学习" "0" "1000"
    exit 1
  fi

  log_ptt "learn_last_asr: requestId=${request_id} insertedChars=${#inserted_text}"
  final_text=$(open_asr_correction_editor "${inserted_text}" || true)
  final_text=$(printf '%s' "${final_text}" | normalize_transcript)
  if [[ -z "${final_text}" ]]; then
    show_status "uconsole voice" "已取消学习" "0" "900"
    exit 0
  fi
  if [[ "${final_text}" == "${inserted_text}" ]]; then
    show_status "uconsole voice" "文本未修改，未学习" "0" "900"
    exit 0
  fi

  edit_ratio=$(compute_edit_ratio "${inserted_text}" "${final_text}")
  if ! python3 - "${edit_ratio}" "${VOICE_LEARN_MAX_EDIT_RATIO}" <<'PYCHECK'
import sys
ratio = float(sys.argv[1])
limit = float(sys.argv[2])
raise SystemExit(0 if ratio <= limit else 1)
PYCHECK
  then
    log_ptt "skip learn_last_asr: edit ratio too large ratio=${edit_ratio} limit=${VOICE_LEARN_MAX_EDIT_RATIO}"
    show_status "uconsole voice" "差异过大，未学习" "0" "1200"
    exit 1
  fi

  finalize_url=$(derive_asr_finalize_url "${request_id}" || true)
  if [[ -z "${finalize_url}" ]]; then
    show_status "uconsole voice" "无法生成学习接口" "0" "1200"
    exit 1
  fi
  run_whisper_curl \
    -fsS \
    --max-time "${WHISPER_TIMEOUT}" \
    -X POST \
    -H "Authorization: Bearer ${WHISPER_AUTH_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "$(jq -n --arg finalText "${final_text}" '{finalText: $finalText}')" \
    "${finalize_url}" >/dev/null

  if ! replace_last_inserted_text "${inserted_text}" "${final_text}"; then
    show_status "uconsole voice" "已学习，未改写输入框" "80" "1200"
    exit 0
  fi

  jq --arg finalText "${final_text}" --argjson finalizedAt "$(date +%s)" \
    '.finalText=$finalText | .finalizedAt=$finalizedAt' \
    "${LAST_ASR_STATE_FILE}" >"${LAST_ASR_STATE_FILE}.tmp" \
    && mv "${LAST_ASR_STATE_FILE}.tmp" "${LAST_ASR_STATE_FILE}"
  log_ptt "learned ASR correction requestId=${request_id} oldChars=${#inserted_text} finalChars=${#final_text} editRatio=${edit_ratio}"
  show_status "uconsole voice" "已学习并改写输入" "100" "900"
}

cancel_recording() {
  local message=${1:-"已取消"}
  if [[ ! -f "${STATE_FILE}" ]]; then
    exit 0
  fi

  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  rm -f "${STATE_FILE}"

  if [[ -n "${RECORDER_PID:-}" ]] && kill -0 "${RECORDER_PID}" >/dev/null 2>&1; then
    kill -INT "${RECORDER_PID}" >/dev/null 2>&1 || true
    if ! wait_for_exit "${RECORDER_PID}" 20; then
      kill -TERM "${RECORDER_PID}" >/dev/null 2>&1 || true
      wait_for_exit "${RECORDER_PID}" 10 || true
    fi
  fi

  if [[ -n "${AUDIO_FILE:-}" && "${VOICE_KEEP_AUDIO}" != "1" ]]; then
    rm -f "${AUDIO_FILE}"
  fi
  show_status "uconsole voice" "${message}" "0" "800"
}

ACTION=${1:-}
if [[ "${ACTION}" == "-h" || "${ACTION}" == "--help" || -z "${ACTION}" ]]; then
  usage
  exit 0
fi

CONFIG_FILE=${VOICE_PTT_CONFIG:-"${HOME}/.config/uconsole-helper-mapper/voice.env"}
if [[ -f "${CONFIG_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi

VOICE_GLOSSARY_FILE=${VOICE_GLOSSARY_FILE:-"${HOME}/.config/uconsole-helper-mapper/voice-glossary.txt"}
VOICE_STATE_DIR=${VOICE_STATE_DIR:-"${XDG_STATE_HOME:-${HOME}/.local/state}/uconsole-helper-mapper"}
STATE_FILE="${VOICE_STATE_DIR}/voice-ptt.state"
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
VOICE_NOTIFY_USE_MARKUP=${VOICE_NOTIFY_USE_MARKUP:-0}
VOICE_NOTIFY_FONT_SIZE=${VOICE_NOTIFY_FONT_SIZE:-22}
VOICE_NOTIFY_PADDING_LINES=${VOICE_NOTIFY_PADDING_LINES:-1}
VOICE_NOTIFY_FONT_SIZE_PANGO=$((VOICE_NOTIFY_FONT_SIZE * 1000))
VOICE_TMUX_CONTEXT=${VOICE_TMUX_CONTEXT:-1}
VOICE_TMUX_CONTEXT_LINES=${VOICE_TMUX_CONTEXT_LINES:-30}
VOICE_TMUX_CONTEXT_MAX_CHARS=${VOICE_TMUX_CONTEXT_MAX_CHARS:-1200}
WLRCTL=${WLRCTL:-"${HOME}/.local/bin/wlrctl"}
WHISPER_URL=${WHISPER_URL:-}
WHISPER_LANGUAGE=${WHISPER_LANGUAGE:-}
WHISPER_AUTH_TOKEN=${WHISPER_AUTH_TOKEN:-}
WHISPER_PROMPT=${WHISPER_PROMPT:-}
WHISPER_PROMPT_FIELD=${WHISPER_PROMPT_FIELD:-prompt}
WHISPER_PROMPT_GLOSSARY_FIELD=${WHISPER_PROMPT_GLOSSARY_FIELD:-promptGlossary}
WHISPER_CONTEXT_FIELD=${WHISPER_CONTEXT_FIELD:-contextText}
WHISPER_FINALIZE_URL=${WHISPER_FINALIZE_URL:-}
WHISPER_ENABLE_CORRECTION=${WHISPER_ENABLE_CORRECTION:-}
WHISPER_CORRECTION_MODE=${WHISPER_CORRECTION_MODE:-}
if [[ -z "${WHISPER_CORRECTION_MODE}" ]]; then
  if [[ "${WHISPER_ENABLE_CORRECTION}" == "1" ]]; then
    WHISPER_CORRECTION_MODE=on
  elif [[ "${WHISPER_ENABLE_CORRECTION}" == "0" ]]; then
    WHISPER_CORRECTION_MODE=off
  else
    WHISPER_CORRECTION_MODE=auto
  fi
fi
WHISPER_TEXT_JQ=${WHISPER_TEXT_JQ:-'.data.text // .text // .result.text // empty'}
WHISPER_NO_PROXY=${WHISPER_NO_PROXY:-1}
WHISPER_TIMEOUT=${WHISPER_TIMEOUT:-60}
VOICE_LEARN_MAX_AGE_SECONDS=${VOICE_LEARN_MAX_AGE_SECONDS:-600}
VOICE_LEARN_MAX_EDIT_RATIO=${VOICE_LEARN_MAX_EDIT_RATIO:-0.38}
VOICE_LEARN_CAPTURE_LINES=${VOICE_LEARN_CAPTURE_LINES:-120}
VOICE_LEARN_REPLACE_INPUT=${VOICE_LEARN_REPLACE_INPUT:-1}
VOICE_LEARN_REPLACE_MAX_CHARS=${VOICE_LEARN_REPLACE_MAX_CHARS:-300}
VOICE_LEARN_DIALOG_FONT_SIZE=${VOICE_LEARN_DIALOG_FONT_SIZE:-22}
VOICE_LEARN_DIALOG_WIDTH=${VOICE_LEARN_DIALOG_WIDTH:-820}
VOICE_LEARN_DIALOG_HEIGHT=${VOICE_LEARN_DIALOG_HEIGHT:-220}
LAST_ASR_STATE_FILE=${LAST_ASR_STATE_FILE:-"${VOICE_STATE_DIR}/voice-last-asr.json"}

case "${ACTION}" in
  start)
    start_recording
    ;;
  stop)
    stop_recording
    ;;
  cancel)
    cancel_recording
    ;;
  learn)
    learn_last_asr
    ;;
  *)
    echo "unknown action: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac
