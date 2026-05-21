# mc888_router_server_v3.py
# from Chris :) 

import threading
import time
import requests
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from flask import Flask, render_template_string, jsonify, request

# ============================================================
# 1. CONFIG / TIMING CONSTANTS
# ============================================================

CONFIG_FILE = "mc888_config.txt"

# ---------------- SCHEDULER TIMING ----------------

SCHEDULER_TICK_SEC          = 1
TELEMETRY_POLL_INTERVAL_SEC = 60
WAN_ALIVE_INTERVAL_SEC      = 60
CONSOLE_FLUSH_INTERVAL_SEC  = 1

# ---------------- ROUTER TIMING ----------------

ROUTER_SETTLE_DELAY_SEC  = 8
ROUTER_LOGIN_TIMEOUT_SEC = 5
ROUTER_GET_TIMEOUT_SEC   = 5
ROUTER_POST_TIMEOUT_SEC  = 10

# ---------------- FRONTEND TIMING ----------------

WEB_CACHE_REFRESH_MS = 1000
WEB_CLOCK_REFRESH_MS = 1000

# ---------------- WAN / REBOOT POLICY ----------------

WAN_FAIL_THRESHOLD        = 3     # consecutive WAN failures before reboot
WAN_REBOOT_SETTLE_SEC     = 90    # seconds to wait after reboot before rechecking
WAN_MAX_REBOOT_ATTEMPTS   = 3     # hard stop after this many reboot cycles

# ---------------- 5G RECONNECT POLICY ----------------

RECONNECT_MAX_ATTEMPTS    = 3     # max Force5G attempts per episode
RECONNECT_INTERVAL_SEC    = 1200  # 20 minutes between attempts
N78_LOG_LOOKBACK_HOURS    = 24    # how far back to scan log for N78

# ============================================================
# 2. SHARED CACHE + LOCKS
# ============================================================

state_lock     = threading.Lock()
console_lock   = threading.Lock()
interrupt_lock = threading.Lock()
router_lock    = threading.Lock()

shared_state = {
    "running": True,

    "router_ip": "192.168.0.1",
    "username":  "user",
    "password":  "",

    # Feature toggles (persisted)
    "logging_enabled":        False,
    "auto_logging_on_start":  False,
    "auto_5g_reconnect":      False,
    "auto_reboot_on_wan_drop": False,

    # Telemetry
    "formatted_stats":   "Waiting for first reading...",
    "last_sample_time":  "No samples yet",
    "current_5g_band":   "",        # e.g. "N78", "N1", or ""

    # WAN state
    "wan_alive":               False,
    "wan_latency":             None,
    "wan_timestamp":           0,
    "wan_consecutive_failures": 0,
    "wan_reboot_attempts":     0,
    "wan_post_reboot_settle":  False,
    "wan_settle_until":        0,
    "wan_hard_stop":           False,

    # 5G reconnect state
    "reconnect_attempts":      0,
    "reconnect_last_attempt":  0,
    "reconnect_active":        False,
    "reconnect_gave_up":       False,

    # Router busy
    "router_busy":  False,
    "active_task":  "Idle",

    "console_buffer": [],
}

MAX_CONSOLE_LINES = 30

# ============================================================
# 3. SCHEDULER STATE
# ============================================================

INTERRUPT_NONE          = 0
INTERRUPT_FORCE_4G      = 1
INTERRUPT_FORCE_5G      = 2
INTERRUPT_REBOOT_ROUTER = 3

pending_interrupt = INTERRUPT_NONE

scheduler_state = {
    "next_telemetry_time":     0,
    "next_wan_time":           0,
    "next_console_flush_time": 0,
}

# ============================================================
# 4. CONSOLE / LOGGING
# ============================================================

def safe_print(*args, **kwargs):
    msg = " ".join(str(arg) for arg in args)
    with console_lock:
        print(msg, **kwargs)
        timestamp = datetime.now().strftime("%H:%M:%S")
        with state_lock:
            shared_state["console_buffer"].append(f"[{timestamp}] {msg}")
            if len(shared_state["console_buffer"]) > MAX_CONSOLE_LINES:
                shared_state["console_buffer"].pop(0)


def get_state_snapshot():
    with state_lock:
        return deepcopy(shared_state)


def update_state(**kwargs):
    with state_lock:
        for k, v in kwargs.items():
            shared_state[k] = v

# ============================================================
# 5. CONFIG LOAD / SAVE
# ============================================================

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f.readlines()]

        update_data = {}
        if len(lines) >= 1: update_data["router_ip"]              = lines[0]
        if len(lines) >= 2: update_data["username"]               = lines[1]
        if len(lines) >= 3: update_data["password"]               = lines[2]
        if len(lines) >= 4: update_data["logging_enabled"]        = lines[3].strip() == "1"
        if len(lines) >= 5: update_data["auto_logging_on_start"]  = lines[4].strip() == "1"
        if len(lines) >= 6: update_data["auto_5g_reconnect"]      = lines[5].strip() == "1"
        if len(lines) >= 7: update_data["auto_reboot_on_wan_drop"]= lines[6].strip() == "1"

        update_state(**update_data)

    except Exception:
        pass


def save_config():
    state = get_state_snapshot()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(f"{state['router_ip']}\n")
            f.write(f"{state['username']}\n")
            f.write(f"{state['password']}\n")
            f.write("1\n" if state["logging_enabled"]         else "0\n")
            f.write("1\n" if state["auto_logging_on_start"]   else "0\n")
            f.write("1\n" if state["auto_5g_reconnect"]       else "0\n")
            f.write("1\n" if state["auto_reboot_on_wan_drop"] else "0\n")
    except Exception as e:
        safe_print(f"[CONFIG SAVE ERROR] {e}")

# ============================================================
# 6. ROUTER TRANSPORT
# ============================================================

def build_base_url(router_ip):
    return f"http://{router_ip}/goform/goform_get_cmd_process"


def build_headers(router_ip):
    return {
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          f"http://{router_ip}/",
        "User-Agent":       "Mozilla/5.0",
        "Accept":           "application/json, text/javascript, */*; q=0.01",
    }


params = {
    "isTest": "false",
    "cmd": (
        "network_type,rssi,rscp,lte_rsrp,Z5g_snr,Z5g_rsrp,ZCELLINFO_band,"
        "Z5g_dlEarfcn,lte_ca_pcell_arfcn,lte_ca_pcell_band,lte_ca_scell_band,"
        "lte_ca_pcell_bandwidth,lte_ca_scell_info,lte_ca_scell_bandwidth,"
        "wan_lte_ca,lte_pci,Z5g_CELL_ID,Z5g_SINR,cell_id,lte_ca_pcell_arfcn,"
        "lte_ca_scell_arfcn,lte_multi_ca_scell_info,wan_active_band,nr5g_pci,"
        "nr5g_action_band,nr5g_cell_id,lte_snr,ecio,wan_active_channel,nr5g_action_channel"
    ),
    "multi_data": "1",
}


def mc888_login(router_ip, username="user", password=""):
    s = requests.Session()
    safe_print("[LOGIN] Starting authentication...")

    r0 = s.get(f"http://{router_ip}/index.html", timeout=ROUTER_LOGIN_TIMEOUT_SEC)
    if r0.status_code != 200:
        safe_print("[LOGIN] FAILED: Handshake unreachable.")
        raise RuntimeError("Handshake failed")

    r_ld = s.get(
        build_base_url(router_ip),
        params={"isTest": "false", "cmd": "LD"},
        headers=build_headers(router_ip),
        timeout=ROUTER_GET_TIMEOUT_SEC,
    )
    try:
        ld = r_ld.json().get("LD")
    except Exception:
        safe_print("[LOGIN] FAILED: Invalid LD token response.")
        raise RuntimeError("LD token invalid")

    if not ld:
        safe_print("[LOGIN] FAILED: Missing LD token.")
        raise RuntimeError("LD token missing")

    try:
        P      = hashlib.sha256(password.encode()).hexdigest().upper()
        hashed = hashlib.sha256((P + ld).encode()).hexdigest().upper()
    except Exception as e:
        safe_print(f"[LOGIN] FAILED: Hashing error ({e}).")
        raise RuntimeError("Hashing failed")

    r = s.post(
        f"http://{router_ip}/goform/goform_set_cmd_process",
        data={"isTest": "false", "goformId": "LOGIN",
              "user": username, "password": hashed},
        headers=build_headers(router_ip),
        timeout=ROUTER_POST_TIMEOUT_SEC,
    )
    try:
        result = r.json().get("result")
    except Exception:
        safe_print("[LOGIN] FAILED: Invalid login JSON.")
        raise RuntimeError("Login JSON invalid")

    if result != "0":
        safe_print("[LOGIN] FAILED: Credentials rejected.")
        raise RuntimeError("Login failed")

    if not s.cookies.get_dict():
        safe_print("[LOGIN] FAILED: No session cookie.")
        raise RuntimeError("No session cookie")

    safe_print("[LOGIN] Success.")
    return s


def mc888_logout(session, router_ip):
    if not session or not hasattr(session, "cookies") or not session.cookies:
        safe_print("[LOGOUT] No active session. Already clean.")
        return True
    safe_print("[LOGOUT] Terminating session...")
    try:
        headers = build_headers(router_ip).copy()
        headers["Origin"]       = f"http://{router_ip}"
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        resp = session.post(
            f"http://{router_ip}/goform/goform_set_cmd_process",
            data={"isTest": "false", "goformId": "LOGOUT"},
            headers=headers,
            timeout=ROUTER_POST_TIMEOUT_SEC,
        )
    except Exception as e:
        safe_print(f"[LOGOUT] FAILED: {e}")
        return False
    if resp.status_code == 200:
        safe_print("[LOGOUT] Success.")
        return True
    safe_print(f"[LOGOUT] FAILED: HTTP {resp.status_code}.")
    return False


def get_rd_token(session, router_ip):
    try:
        safe_print(" -> [Sub-Task] Requesting RD token...")
        r     = session.get(
            build_base_url(router_ip),
            params={"isTest": "false", "cmd": "RD"},
            headers=build_headers(router_ip),
            timeout=ROUTER_POST_TIMEOUT_SEC,
        )
        token = r.json().get("RD")
        safe_print(f"   -> RD Token captured: {token[:6]}...")
        return token
    except Exception as e:
        safe_print(f"   -> ERROR: RD extraction failed: {e}")
        return None


def generate_ad(rd):
    rd0   = "BD_H3GUKMC888V1.0.0B09"
    step1 = hashlib.sha256(rd0.encode()).hexdigest().upper()
    return hashlib.sha256((step1 + rd).encode()).hexdigest().upper()


def data_off(session, router_ip):
    safe_print(" -> [Sub-Task] Dropping WAN cellular link (DISCONNECT)...")
    rd = get_rd_token(session, router_ip)
    if not rd:
        return False
    ad      = generate_ad(rd)
    headers = build_headers(router_ip).copy()
    headers["Origin"]       = f"http://{router_ip}"
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    resp = session.post(
        f"http://{router_ip}/goform/goform_set_cmd_process",
        data={"isTest": "false", "notCallback": "true",
              "goformId": "DISCONNECT_NETWORK", "AD": ad},
        headers=headers, timeout=ROUTER_POST_TIMEOUT_SEC,
    )
    success = '"success"' in resp.text
    safe_print(f"   -> Disconnect: {'SUCCESS' if success else 'FAILURE'}")
    return success


def data_on(session, router_ip):
    safe_print(" -> [Sub-Task] Restoring WAN cellular link (CONNECT)...")
    rd = get_rd_token(session, router_ip)
    if not rd:
        return False
    ad      = generate_ad(rd)
    headers = build_headers(router_ip).copy()
    headers["Origin"]       = f"http://{router_ip}"
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    resp = session.post(
        f"http://{router_ip}/goform/goform_set_cmd_process",
        data={"isTest": "false", "notCallback": "true",
              "goformId": "CONNECT_NETWORK", "AD": ad},
        headers=headers, timeout=ROUTER_POST_TIMEOUT_SEC,
    )
    success = '"success"' in resp.text
    safe_print(f"   -> Connect: {'SUCCESS' if success else 'FAILURE'}")
    return success


def set_bearer_preference(session, router_ip, mode):
    safe_print(f" -> [Sub-Task] Setting bearer preference to [{mode}]...")
    rd = get_rd_token(session, router_ip)
    if not rd:
        return False
    ad      = generate_ad(rd)
    headers = build_headers(router_ip).copy()
    headers["Origin"]       = f"http://{router_ip}"
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    resp = session.post(
        f"http://{router_ip}/goform/goform_set_cmd_process",
        data={"isTest": "false", "notCallback": "true",
              "goformId": "SET_BEARER_PREFERENCE",
              "BearerPreference": mode, "AD": ad},
        headers=headers, timeout=ROUTER_POST_TIMEOUT_SEC,
    )
    success = '"success"' in resp.text
    safe_print(f"   -> Preference: {'SUCCESS' if success else 'FAILURE'}")
    return success

# ============================================================
# 7. MODEM DECODE / FORMATTER
# ============================================================

def safe(v):
    return v if v not in ("", None) else "-"


def format_stats(data):
    lte_freq = safe(data.get("wan_active_channel"))
    lte_rsrp = safe(data.get("lte_rsrp"))
    lte_sinr = safe(data.get("lte_snr")) or safe(data.get("ecio"))

    raw_lte_pci = safe(data.get("lte_pci"))
    try:
        lte_pci = str(int(raw_lte_pci, 16)) if raw_lte_pci and raw_lte_pci != "-" else "-"
    except Exception:
        lte_pci = raw_lte_pci or "-"

    lte_cell = safe(data.get("cell_id"))
    nr_freq  = safe(data.get("nr5g_action_channel"))
    nr_band  = safe(data.get("nr5g_action_band"))
    nr_rsrp  = safe(data.get("Z5g_rsrp"))
    nr_sinr  = safe(data.get("Z5g_SINR"))

    raw_nr_pci = safe(data.get("nr5g_pci"))
    try:
        nr_pci = str(int(raw_nr_pci, 16)) if raw_nr_pci and raw_nr_pci != "-" else "-"
    except Exception:
        nr_pci = raw_nr_pci or "-"

    nr_cell = safe(data.get("nr5g_cell_id"))

    try:
        lte_cell_dec = str(int(lte_cell, 16))
    except Exception:
        lte_cell_dec = "-"

    nr_cell_disp = nr_cell if nr_cell not in ("", None) else "-"

    # Update the current 5G band in shared state so the scheduler can inspect it
    band_str = (nr_band or "").strip().upper()
    update_state(current_5g_band=band_str)

    def build_ca_list():
        out   = []
        pband = safe(data.get("lte_ca_pcell_band"))
        pbw   = safe(data.get("lte_ca_pcell_bandwidth"))
        if pband and pbw and pband != "-":
            if pband == "3":   out.append(f"{pbw}MHz@1800(B3)")
            elif pband == "7": out.append(f"{pbw}MHz@2600(B7)")
            else:              out.append(f"{pbw}MHz@Band{pband}")
        ca_raw = safe(data.get("lte_multi_ca_scell_info"))
        if ca_raw and ca_raw != "-":
            for p in ca_raw.strip(";").split(";"):
                f = p.split(",")
                if len(f) >= 6:
                    bw, band = f[5], f[3]
                    if band == "3":   out.append(f"{bw}MHz@1800(B3)")
                    elif band == "7": out.append(f"{bw}MHz@2600(B7)")
                    else:             out.append(f"{bw}MHz@Band{band}")
        return out

    ca_list     = build_ca_list()
    LABEL_WIDTH = 20
    SPACER      = " " * (LABEL_WIDTH + 2)

    if not ca_list:
        ca_str = "-"
    elif len(ca_list) == 1:
        ca_str = ca_list[0]
    else:
        ca_str = ca_list[0] + "\n" + "\n".join(f"{SPACER}{c}" for c in ca_list[1:])

    nr_band_disp = nr_band.upper() if nr_band and nr_band != "-" else "-"

    def line(label, value):
        return f"{label:<20}: {value}"

    lines = [
        line("4G Frequency",       lte_freq),
        line("4G Connected Band",  ca_str),
        line("4G Signal Strength", f"{lte_rsrp} dBm"),
        line("4G ECIO/SINR",       f"{lte_sinr} dB"),
        line("4G PCI",             lte_pci),
        line("4G Cell ID",         lte_cell_dec),
        "",
        line("5G Frequency",       nr_freq),
        line("5G Connected Band",  nr_band_disp),
        line("5G Signal Strength", f"{nr_rsrp} dBm"),
        line("5G SINR",            f"{nr_sinr} dB"),
        line("5G PCI",             nr_pci),
        line("5G (SA) Cell ID",    nr_cell_disp),
    ]
    return "\n".join(lines)

# ============================================================
# 8. LOG SCANNER — check if N78 seen in last N hours
# ============================================================

def n78_seen_in_log(logfile="mc888_log.txt", hours=N78_LOG_LOOKBACK_HOURS):
    """Return True if '5G Connected Band : N78' appears in the log within the last N hours."""
    cutoff = datetime.now() - timedelta(hours=hours)
    try:
        with open(logfile, "r", encoding="utf-8") as f:
            for line in f:
                if "N78" not in line:
                    continue
                # Extract timestamp from start of line: [2026-05-20 19:54:28]
                try:
                    ts_str = line[1:20]
                    ts     = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if ts >= cutoff:
                        return True
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return False

# ============================================================
# 9. INTERRUPT TRANSACTIONS
# ============================================================

def _mode_switch_transaction(mode_label, bearer_mode):
    """Shared logic for Force4G and Force5G."""
    state     = get_state_snapshot()
    router_ip = state["router_ip"]
    username  = state["username"]
    password  = state["password"]

    safe_print(f"\n[HTTP CONTROL CALL] Executing {mode_label} Configuration Pipeline...")
    update_state(router_busy=True, active_task=mode_label)

    with router_lock:
        session = None
        try:
            session = mc888_login(router_ip, username, password)
            data_off(session, router_ip)
            time.sleep(ROUTER_SETTLE_DELAY_SEC)
            set_bearer_preference(session, router_ip, bearer_mode)
            time.sleep(ROUTER_SETTLE_DELAY_SEC)
            data_on(session, router_ip)
            time.sleep(ROUTER_SETTLE_DELAY_SEC)
            refresh_live_cache(session, router_ip)
            safe_print(f"[COMPLETED] {mode_label} pipeline finished.\n")
        except Exception as e:
            safe_print(f"[FATAL FAILURE] {mode_label} pipeline broken: {e}\n")
        finally:
            if session:
                mc888_logout(session, router_ip)
            update_state(router_busy=False, active_task="Idle")


def execute_force4g_transaction():
    _mode_switch_transaction("Force4G", "Only_LTE")


def execute_force5g_transaction():
    _mode_switch_transaction("Force5G", "NETWORK_auto")


def execute_reboot_router_transaction():
    state     = get_state_snapshot()
    router_ip = state["router_ip"]
    username  = state["username"]
    password  = state["password"]

    safe_print("[REBOOT ROUTER] Initiating hard remote hardware power cycle...")
    update_state(router_busy=True, active_task="RebootRouter")

    with router_lock:
        session = None
        try:
            session = mc888_login(router_ip, username, password)
            safe_print(" -> [Sub-Task] Issuing REBOOT_DEVICE command...")
            rd = get_rd_token(session, router_ip)
            if rd:
                ad      = generate_ad(rd)
                headers = build_headers(router_ip).copy()
                headers["Origin"]       = f"http://{router_ip}"
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                session.post(
                    f"http://{router_ip}/goform/goform_set_cmd_process",
                    data={"isTest": "false", "goformId": "REBOOT_DEVICE", "AD": ad},
                    headers=headers, timeout=5,
                )
                safe_print("[REBOOT ROUTER] Command dispatched.")
        except Exception as e:
            safe_print(f"[REBOOT ERROR] {e}")
        finally:
            update_state(router_busy=False, active_task="Idle")

# ============================================================
# 10. PERIODIC TASKS
# ============================================================

def refresh_live_cache(session, router_ip):
    safe_print(" -> Reading live telemetry from modem...")
    resp = session.get(
        build_base_url(router_ip),
        params=params,
        headers=build_headers(router_ip),
        timeout=ROUTER_GET_TIMEOUT_SEC,
    )
    data      = resp.json()
    formatted = format_stats(data)
    update_state(
        formatted_stats=formatted,
        last_sample_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    safe_print(" -> Live telemetry cache updated.")


def log_event(formatted_text, logfile="mc888_log.txt"):
    MAX_LINES = 2160
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] " + " ".join(formatted_text.split()) + "\n"
    try:
        with open(logfile, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        lines = []
    lines.append(log_entry)
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    try:
        with open(logfile, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass


def run_telemetry_poll_task():
    state = get_state_snapshot()
    if not state["logging_enabled"]:
        return
    if state["router_busy"]:
        return

    update_state(router_busy=True, active_task="TelemetryPoll")
    with router_lock:
        session = None
        try:
            session = mc888_login(state["router_ip"], state["username"], state["password"])
            refresh_live_cache(session, state["router_ip"])
            latest = get_state_snapshot()["formatted_stats"]
            log_event(latest)
        except Exception as e:
            safe_print(f"[POLL ERROR] {e}")
        finally:
            if session:
                mc888_logout(session, state["router_ip"])
            update_state(router_busy=False, active_task="Idle")


def check_wan_alive():
    """Check WAN by connecting to Google DNS on port 53."""
    import socket
    host    = "8.8.8.8"
    port    = 53
    timeout = 5

    try:
        start = time.monotonic()
        sock  = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        latency = time.monotonic() - start
        alive   = True
    except Exception:
        latency = None
        alive   = False

    update_state(
        wan_alive=alive,
        wan_latency=latency,
        wan_timestamp=int(time.time()),
    )

    if latency is not None:
        safe_print(f"[WANLIVE] WANUP: {alive}, Latency: {latency * 1000:.1f} ms")
    else:
        safe_print(f"[WANLIVE] WANUP: {alive}, Latency: unavailable")

    return alive

# ============================================================
# 11. AUTO-LOGIC: WAN DROP REBOOT
# ============================================================

def handle_wan_drop_logic(alive):
    """
    Called after every WAN check.
    Tracks consecutive failures and triggers reboots up to WAN_MAX_REBOOT_ATTEMPTS.
    After that, hard stop with a loud console message.
    """
    state = get_state_snapshot()

    if not state["auto_reboot_on_wan_drop"]:
        return

    if state["wan_hard_stop"]:
        return  # already given up, don't loop

    # If we're in post-reboot settle period, skip until settle expires
    if state["wan_post_reboot_settle"]:
        if time.monotonic() < state["wan_settle_until"]:
            safe_print("[WAN GUARD] Post-reboot settle period active. Skipping WAN logic.")
            return
        else:
            update_state(wan_post_reboot_settle=False)

    if alive:
        # WAN recovered — reset counters
        if state["wan_consecutive_failures"] > 0 or state["wan_reboot_attempts"] > 0:
            safe_print("[WAN GUARD] WAN recovered. Resetting failure counters.")
        update_state(wan_consecutive_failures=0, wan_reboot_attempts=0)
        return

    # WAN is down
    new_failures = state["wan_consecutive_failures"] + 1
    update_state(wan_consecutive_failures=new_failures)
    safe_print(f"[WAN GUARD] WAN failure {new_failures}/{WAN_FAIL_THRESHOLD}")

    if new_failures < WAN_FAIL_THRESHOLD:
        return  # not yet at threshold

    # Threshold reached
    reboot_count = state["wan_reboot_attempts"]

    if reboot_count >= WAN_MAX_REBOOT_ATTEMPTS:
        safe_print("=" * 60)
        safe_print("!!! BAD WAN - 3 REBOOT ATTEMPTS FAILED - MANUAL INTERVENTION REQUIRED !!!")
        safe_print("=" * 60)
        update_state(wan_hard_stop=True)
        return

    # Trigger a reboot
    new_reboot_count = reboot_count + 1
    safe_print(f"[WAN GUARD] Triggering auto-reboot attempt {new_reboot_count}/{WAN_MAX_REBOOT_ATTEMPTS}...")
    update_state(
        wan_consecutive_failures=0,
        wan_reboot_attempts=new_reboot_count,
        wan_post_reboot_settle=True,
        wan_settle_until=time.monotonic() + WAN_REBOOT_SETTLE_SEC,
    )
    execute_reboot_router_transaction()
    safe_print(f"[WAN GUARD] Reboot dispatched. Settling for {WAN_REBOOT_SETTLE_SEC}s...")

# ============================================================
# 12. AUTO-LOGIC: 5G RECONNECT
# ============================================================

def handle_5g_reconnect_logic():
    """
    Called after every telemetry poll.
    If not on N78, checks log history and attempts Force5G up to RECONNECT_MAX_ATTEMPTS.
    Backs off between attempts. Gives up cleanly after max attempts.
    """
    state = get_state_snapshot()

    if not state["auto_5g_reconnect"]:
        return

    if state["reconnect_gave_up"]:
        return

    if state["router_busy"]:
        return

    band = state["current_5g_band"]

    if band == "N78":
        # On N78 — reset reconnect state
        if state["reconnect_attempts"] > 0 or state["reconnect_active"]:
            safe_print("[5G GUARD] N78 confirmed. Resetting reconnect state.")
        update_state(reconnect_attempts=0, reconnect_active=False, reconnect_gave_up=False)
        return

    # Not on N78 — could be N1 or 4G only
    if band == "N1":
        safe_print(f"[5G GUARD] On 5G fallback N1. Checking if N78 is worth trying...")
    elif band in ("", "-"):
        safe_print(f"[5G GUARD] No 5G band detected (4G only). Checking if N78 is worth trying...")
    else:
        safe_print(f"[5G GUARD] On band {band}. Checking if N78 is worth trying...")

    # Check if N78 has been seen recently in the log
    if not n78_seen_in_log():
        safe_print(f"[5G GUARD] N78 not seen in last {N78_LOG_LOOKBACK_HOURS}hrs. Not attempting reconnect.")
        return

    attempts = state["reconnect_attempts"]

    if attempts >= RECONNECT_MAX_ATTEMPTS:
        safe_print(f"[5G GUARD] Max reconnect attempts ({RECONNECT_MAX_ATTEMPTS}) reached. Giving up this episode.")
        update_state(reconnect_gave_up=True)
        return

    # Check interval between attempts
    last_attempt = state["reconnect_last_attempt"]
    now          = time.monotonic()
    if last_attempt > 0 and (now - last_attempt) < RECONNECT_INTERVAL_SEC:
        remaining = int(RECONNECT_INTERVAL_SEC - (now - last_attempt))
        safe_print(f"[5G GUARD] Next attempt in {remaining}s.")
        return

    # Fire the attempt
    new_attempts = attempts + 1
    safe_print(f"[5G GUARD] Attempting Force5G reconnect ({new_attempts}/{RECONNECT_MAX_ATTEMPTS})...")
    update_state(
        reconnect_attempts=new_attempts,
        reconnect_last_attempt=now,
        reconnect_active=True,
    )

    # Queue the interrupt
    global pending_interrupt
    with interrupt_lock:
        pending_interrupt = INTERRUPT_FORCE_5G

# ============================================================
# 13. SCHEDULER LOOP
# ============================================================

def scheduler_loop():
    global pending_interrupt

    now = time.monotonic()
    scheduler_state["next_telemetry_time"]     = now
    scheduler_state["next_wan_time"]           = now
    scheduler_state["next_console_flush_time"] = now

    safe_print("[Scheduler Started]")

    # Boot poll — get initial reading
    state = get_state_snapshot()
    with router_lock:
        session = None
        try:
            session = mc888_login(state["router_ip"], state["username"], state["password"])
            refresh_live_cache(session, state["router_ip"])
        except Exception as e:
            safe_print(f"[BOOT POLL ERROR] Initial telemetry skipped: {e}")
        finally:
            if session:
                mc888_logout(session, state["router_ip"])

    while get_state_snapshot()["running"]:

        now = time.monotonic()

        # --- Interrupts ---
        with interrupt_lock:
            interrupt         = pending_interrupt
            pending_interrupt = INTERRUPT_NONE

        if interrupt != INTERRUPT_NONE:
            if interrupt == INTERRUPT_FORCE_4G:
                execute_force4g_transaction()
            elif interrupt == INTERRUPT_FORCE_5G:
                execute_force5g_transaction()
            elif interrupt == INTERRUPT_REBOOT_ROUTER:
                execute_reboot_router_transaction()

            now = time.monotonic()
            scheduler_state["next_telemetry_time"] = now + TELEMETRY_POLL_INTERVAL_SEC
            scheduler_state["next_wan_time"]       = now + WAN_ALIVE_INTERVAL_SEC

        # --- Periodic tasks ---
        state = get_state_snapshot()

        if not state["router_busy"]:

            if now >= scheduler_state["next_telemetry_time"]:
                run_telemetry_poll_task()
                handle_5g_reconnect_logic()
                scheduler_state["next_telemetry_time"] = now + TELEMETRY_POLL_INTERVAL_SEC

            if now >= scheduler_state["next_wan_time"]:
                alive = check_wan_alive()
                handle_wan_drop_logic(alive)
                scheduler_state["next_wan_time"] = now + WAN_ALIVE_INTERVAL_SEC

        time.sleep(SCHEDULER_TICK_SEC)

# ============================================================
# 14. FLASK APP + ROUTES
# ============================================================

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>ZTE MC888 - Router Link</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            background-color: #1e1e1e;
            color: #eaeaea;
            font-family: Arial, Helvetica, sans-serif;
            font-size: 100%;
            text-align: center;
            padding: 0; margin: 0;
        }
        .header-banner {
            background-color: #141414;
            color: #ffffff;
            padding: 10px 0;
            font-size: 1.4em;
            font-weight: bold;
            border-bottom: 2px solid #222222;
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 12px;
        }
        .wan-indicator {
            width: 14px; height: 14px;
            border-radius: 50%;
            display: inline-block;
            background-color: #555555;
            flex-shrink: 0;
        }
        .wan-indicator.up   { background-color: #00cc44; box-shadow: 0 0 6px #00cc44; }
        .wan-indicator.down { background-color: #cc2200; box-shadow: 0 0 6px #cc2200; }
        .container {
            max-width: 440px;
            margin: 0 auto;
            background: #1e1e1e;
            padding: 0 15px;
            box-sizing: border-box;
        }
        .time-header {
            font-size: 14px;
            font-weight: normal;
            margin-bottom: 15px;
            color: #eaeaea;
        }
        .pre-wrapper {
            background: #141414;
            border: 1px solid #2f2f2f;
            border-radius: 6px;
            padding: 12px;
            margin-bottom: 15px;
        }
        pre {
            text-align: left;
            font-family: 'Courier New', Courier, monospace;
            font-size: 14px;
            font-weight: bold;
            white-space: pre-wrap;
            overflow-x: auto;
            color: #ffffff;
            line-height: 1.4;
            margin: 0;
        }
        .btn-container { margin-bottom: 15px; }
        .btn {
            display: block;
            background-color: #0094ce;
            color: #ffffff;
            border: 1px solid #111111;
            padding: 12px 0;
            margin: 10px auto;
            font-size: 16px;
            font-weight: normal;
            border-radius: 4px;
            cursor: pointer;
            width: 95%;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.2), 0 1px 2px rgba(0,0,0,0.2);
            box-sizing: border-box;
        }
        .btn:hover      { background-color: #007bb3; }
        .btn-danger     { background-color: #9b1c1c; }
        .btn-danger:hover { background-color: #bb2a2a; }
        .sample-footer {
            font-size: 1.1em;
            font-weight: bold;
            color: #ffffff;
            margin-top: 15px;
            margin-bottom: 20px;
        }
        .config-panel {
            background: #141414;
            border: 1px solid #2f2f2f;
            border-radius: 6px;
            padding: 12px;
            margin-bottom: 15px;
            text-align: left;
        }
        .config-panel label {
            display: block;
            margin-top: 8px;
            margin-bottom: 4px;
            color: #cccccc;
            font-size: 13px;
        }
        .config-input {
            width: 100%;
            box-sizing: border-box;
            background: #2b2b2b;
            color: white;
            border: 1px solid #444;
            border-radius: 4px;
            padding: 8px;
            margin-bottom: 6px;
        }
        .checkbox-row {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 10px;
            color: #cccccc;
            font-size: 13px;
        }
        .checkbox-row input[type=checkbox] {
            width: 16px; height: 16px;
            cursor: pointer;
        }
        .save-btn {
            width: 100%;
            margin-top: 12px;
            background-color: #3a3a3a;
            color: white;
            border: 1px solid #111;
            padding: 10px;
            border-radius: 4px;
            cursor: pointer;
        }
        .save-btn:hover { background-color: #4a4a4a; }
        .console-heading {
            text-align: left;
            font-size: 12px;
            font-weight: bold;
            color: #888888;
            margin-top: 20px;
            margin-bottom: 4px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .console-wrapper {
            background: #0a0a0a;
            border: 1px solid #222222;
            border-radius: 4px;
            padding: 8px;
            margin-bottom: 15px;
            height: 110px;
            overflow-y: auto;
        }
        .console-log {
            text-align: left;
            font-family: 'Courier New', Courier, monospace;
            font-size: 11px;
            color: #00ff00;
            white-space: pre-wrap;
            margin: 0;
            line-height: 1.3;
        }
        .footer-banner {
            color: #888888;
            padding: 6px 0;
            font-size: 0.75em;
            margin-top: 20px;
            border-top: 1px solid #2f2f2f;
        }
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background-color: rgba(0,0,0,0.75);
            z-index: 1000;
            justify-content: center;
            align-items: center;
            padding: 20px;
            box-sizing: border-box;
        }
        .modal-card {
            background-color: #141414;
            border: 2px solid #222222;
            border-radius: 8px;
            padding: 20px;
            max-width: 360px;
            width: 100%;
            box-shadow: 0 4px 15px rgba(0,0,0,0.6);
            text-align: center;
            box-sizing: border-box;
        }
        .modal-title  { font-size: 1.2em; font-weight: bold; color: #ffffff; margin-bottom: 10px; }
        .modal-text   { font-size: 14px; color: #eaeaea; margin-bottom: 20px; line-height: 1.4; }
        .modal-actions { display: flex; justify-content: space-around; gap: 10px; }
        .modal-btn    { flex: 1; padding: 10px 0; font-size: 14px; font-weight: bold; border-radius: 4px; cursor: pointer; border: 1px solid #111111; }
        .btn-confirm  { background-color: #0094ce; color: #fff; }
        .btn-cancel   { background-color: #444444; color: #fff; }
        .btn-close    { background-color: #333333; color: #fff; width: 100%; }
    </style>
    <script>
        let pendingEndpoint = '';

        function showConfirm(endpoint, actionName) {
            pendingEndpoint = endpoint;
            document.getElementById('confirm-msg').innerText =
                "Are you sure you want to execute a physical band cycle to shift connection rules to " + actionName + "?";
            document.getElementById('confirm-modal').style.display = 'flex';
        }
        function closeConfirm() {
            document.getElementById('confirm-modal').style.display = 'none';
        }
        function executeConfirmedAction() {
            let targetUrl = pendingEndpoint;
            closeConfirm();
            showStatus("Executing Command", "Connecting to driver and sending payloads... Please wait.");
            fetch(targetUrl, { method: 'POST' })
                .then(response => {
                    if (!response.ok) throw new Error("HTTP status " + response.status);
                    return response.json();
                })
                .then(data => { showStatus("Sequence Output", data.status); })
                .catch(err => { showStatus("Network Error", "Failed to talk to gateway proxy: " + err); });
        }
        function showStatus(title, text) {
            document.getElementById('status-title').innerText = title;
            document.getElementById('status-text').innerText  = text;
            document.getElementById('status-modal').style.display = 'flex';
        }
        function closeStatus() {
            document.getElementById('status-modal').style.display = 'none';
        }

        // Live clock
        setInterval(() => {
            const now = new Date();
            const pad = n => String(n).padStart(2,'0');
            document.getElementById('live-clock').innerText =
                `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())} ` +
                `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
        }, 1000);

        // Stats + WAN indicator poll
        setInterval(() => {
            fetch('/api/stats')
                .then(res => { if (!res.ok) throw new Error(); return res.json(); })
                .then(data => {
                    document.getElementById('stats-box').innerText = data.formatted;

                    // WAN indicator
                    const dot = document.getElementById('wan-dot');
                    if (data.wan_alive) {
                        dot.className = 'wan-indicator up';
                        dot.title = 'WAN UP' + (data.wan_latency ? ' — ' + (data.wan_latency * 1000).toFixed(0) + 'ms' : '');
                    } else {
                        dot.className = 'wan-indicator down';
                        dot.title = 'WAN DOWN';
                    }

                    if (data.console_logs) {
                        const box = document.getElementById('console-box');
                        const atBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 5;
                        box.innerText = data.console_logs.join('\\n');
                        if (atBottom) box.scrollTop = box.scrollHeight;
                    }
                    if (data.last_log && data.last_log !== "No samples yet") {
                        const parts = data.last_log.split(" ");
                        document.getElementById('sample-box').innerText =
                            "Last Sample Time: " + (parts.length === 2 ? parts[1] : data.last_log);
                    }
                }).catch(() => {});
        }, 1000);

        async function saveConfig() {
            const payload = {
                router_ip:               document.getElementById('router-ip').value,
                username:                document.getElementById('router-username').value,
                password:                document.getElementById('router-password').value,
                logging_enabled:         document.getElementById('cb-logging').checked,
                auto_logging_on_start:   document.getElementById('cb-auto-log').checked,
                auto_5g_reconnect:       document.getElementById('cb-5g-reconnect').checked,
                auto_reboot_on_wan_drop: document.getElementById('cb-auto-reboot').checked,
            };
            const response = await fetch('/api/save_config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await response.json();
            alert(result.status);
        }
    </script>
</head>
<body>

<div class="header-banner">
    <span id="wan-dot" class="wan-indicator"></span>
    ZTE MC888 B09 Router Link
</div>

<div class="container">

    <div id="live-clock" class="time-header">Loading live clock...</div>

    <div class="config-panel">
        <label>Router IP</label>
        <input id="router-ip" class="config-input" type="text" value="{{ router_ip }}">

        <label>Username</label>
        <input id="router-username" class="config-input" type="text" value="{{ username }}">

        <label>Password</label>
        <input id="router-password" class="config-input" type="password">

        <div class="checkbox-row">
            <input type="checkbox" id="cb-logging" {% if logging_enabled %}checked{% endif %}>
            <label for="cb-logging">Enable logging</label>
        </div>
        <div class="checkbox-row">
            <input type="checkbox" id="cb-auto-log" {% if auto_logging_on_start %}checked{% endif %}>
            <label for="cb-auto-log">Auto-start logging on launch</label>
        </div>
        <div class="checkbox-row">
            <input type="checkbox" id="cb-5g-reconnect" {% if auto_5g_reconnect %}checked{% endif %}>
            <label for="cb-5g-reconnect">Auto 5G reconnect (N78)</label>
        </div>
        <div class="checkbox-row">
            <input type="checkbox" id="cb-auto-reboot" {% if auto_reboot_on_wan_drop %}checked{% endif %}>
            <label for="cb-auto-reboot">Auto reboot on WAN drop</label>
        </div>

        <button class="save-btn" onclick="saveConfig()">Save Configuration</button>
    </div>

    <div class="pre-wrapper">
        <pre id="stats-box">{{ stats_text }}</pre>
    </div>

    <div class="btn-container">
        <button class="btn" onclick="showConfirm('/api/force4g', '4G Only')">Force 4G</button>
        <button class="btn" onclick="showConfirm('/api/force5g', '5G Preferred')">Force 5G</button>
        <button class="btn btn-danger" onclick="showConfirm('/api/reboot_router', 'Router Reboot')">Reboot Router</button>
    </div>

    <div id="sample-box" class="sample-footer">Last Sample Time: Waiting...</div>

    <div class="console-heading">Device Console Log</div>
    <div class="console-wrapper" id="console-box-wrapper">
        <pre class="console-log" id="console-box">System initialized. Awaiting pipeline telemetry...</pre>
    </div>

</div>

<div class="footer-banner">ZTE MC888 v1.0 B09 Link — ChrisTheWizard</div>

<div id="confirm-modal" class="modal-overlay">
    <div class="modal-card">
        <div class="modal-title">Confirm Connection Cycle</div>
        <div id="confirm-msg" class="modal-text">Are you sure?</div>
        <div class="modal-actions">
            <button class="modal-btn btn-confirm" onclick="executeConfirmedAction()">Confirm</button>
            <button class="modal-btn btn-cancel"  onclick="closeConfirm()">Cancel</button>
        </div>
    </div>
</div>

<div id="status-modal" class="modal-overlay">
    <div class="modal-card">
        <div id="status-title" class="modal-title">Status</div>
        <div id="status-text"  class="modal-text">Processing...</div>
        <button class="modal-btn btn-close" onclick="closeStatus()">OK</button>
    </div>
</div>

</body>
</html>
"""


@app.route("/")
def index():
    state = get_state_snapshot()
    return render_template_string(
        HTML_TEMPLATE,
        stats_text=state["formatted_stats"],
        router_ip=state["router_ip"],
        username=state["username"],
        logging_enabled=state["logging_enabled"],
        auto_logging_on_start=state["auto_logging_on_start"],
        auto_5g_reconnect=state["auto_5g_reconnect"],
        auto_reboot_on_wan_drop=state["auto_reboot_on_wan_drop"],
    )


@app.route("/api/stats", methods=["GET"])
def api_stats():
    state = get_state_snapshot()
    return jsonify({
        "last_log":    state["last_sample_time"],
        "formatted":   state["formatted_stats"],
        "console_logs": state["console_buffer"],
        "wan_alive":   state["wan_alive"],
        "wan_latency": state["wan_latency"],
        "router_busy": state["router_busy"],
        "active_task": state["active_task"],
        "current_5g_band": state["current_5g_band"],
    })


@app.route("/api/save_config", methods=["POST"])
def save_config_api():
    data = request.get_json()
    update_state(
        router_ip=               data.get("router_ip", "192.168.0.1").strip(),
        username=                data.get("username",  "user").strip(),
        password=                data.get("password",  ""),
        logging_enabled=         bool(data.get("logging_enabled",         False)),
        auto_logging_on_start=   bool(data.get("auto_logging_on_start",   False)),
        auto_5g_reconnect=       bool(data.get("auto_5g_reconnect",       False)),
        auto_reboot_on_wan_drop= bool(data.get("auto_reboot_on_wan_drop", False)),
    )
    save_config()
    safe_print("[CONFIG] Router configuration updated.")
    return jsonify({"status": "Configuration Saved"})


@app.route("/api/force4g", methods=["POST"])
def web_force_4g():
    global pending_interrupt
    with interrupt_lock:
        pending_interrupt = INTERRUPT_FORCE_4G
    safe_print("[INTERRUPT REQUEST] Force4G queued.")
    return jsonify({"status": "Force4G request accepted"})


@app.route("/api/force5g", methods=["POST"])
def web_force_5g():
    global pending_interrupt
    with interrupt_lock:
        pending_interrupt = INTERRUPT_FORCE_5G
    safe_print("[INTERRUPT REQUEST] Force5G queued.")
    return jsonify({"status": "Force5G request accepted"})


@app.route("/api/reboot_router", methods=["POST"])
def web_reboot_router():
    global pending_interrupt
    with interrupt_lock:
        pending_interrupt = INTERRUPT_REBOOT_ROUTER
    safe_print("[INTERRUPT REQUEST] RebootRouter queued.")
    return jsonify({"status": "RebootRouter request accepted"})


# ============================================================
# 15. STARTUP
# ============================================================

if __name__ == "__main__":

    load_config()

    # Apply auto-logging-on-start if configured
    state = get_state_snapshot()
    if state["auto_logging_on_start"] and not state["logging_enabled"]:
        update_state(logging_enabled=True)
        safe_print("[CONFIG] Auto-logging enabled on start.")

    scheduler_thread = threading.Thread(
        target=scheduler_loop,
        daemon=True,
    )
    scheduler_thread.start()

    safe_print("[MC888 Router Node active] Listening on port 5000")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True,
    )
