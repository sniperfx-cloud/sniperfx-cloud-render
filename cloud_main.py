#!/usr/bin/env python3
"""SniperFx v2.1 cloud worker (no website, no MT5 order execution).
Built atop the user's previous experimental v1.1 signal/lot engine.
Require mounted persistent volume at /data on Render/Railway. No secret is baked into source.
"""
import datetime as dt
import json
import math
import os
import secrets
import sys
import threading
import time
import socket
import urllib.error
from pathlib import Path

# Render Blueprint explicitly sets SNIPERFX_DATA_DIR=/data. Railway may also use this script.
# Stop rather than silently lose account state if the persistent disk is missing.
DATA_DIR = (os.environ.get('SNIPERFX_DATA_DIR') or os.environ.get('RAILWAY_VOLUME_MOUNT_PATH') or '').strip()
DEV_MODE = os.environ.get('SNIPERFX_DEV_MODE', '') == 'yes'
if DEV_MODE:
    DATA_DIR = os.environ.get('SNIPERFX_DEV_DATA_DIR', '')
if not DATA_DIR or not Path(DATA_DIR).is_absolute() or (not DEV_MODE and DATA_DIR != '/data'):
    print('SETUP ERROR: Attach a persistent disk at /data and set SNIPERFX_DATA_DIR=/data.', flush=True)
    sys.exit(2)
if not DEV_MODE and not os.path.ismount(DATA_DIR):
    print('SETUP ERROR: /data is not a mounted persistent disk; refusing ephemeral state.', flush=True)
    sys.exit(2)
os.environ['APPDATA'] = DATA_DIR  # Earlier engine stores settings at APPDATA/SniperIntelligence
# Never reset equity, pairing or 5+ signal history from a corrupt state file.
settings_file = Path(DATA_DIR) / 'SniperIntelligence' / 'settings.json'
if settings_file.exists():
    try:
        saved_settings = json.loads(settings_file.read_text(encoding='utf-8'))
        if not isinstance(saved_settings, dict):
            raise ValueError('invalid settings type')
    except (OSError, ValueError, TypeError):
        print('SETUP ERROR: Existing settings file is corrupt. STOPPED to protect capital/pairing.', flush=True)
        sys.exit(2)

import strategy_engine as core
from reliability import Outbox

STATE_FILE = core.APP_DIR / 'cloud_state.json'
STATE_DEFAULTS = {
    'active': False,
    'equity_confirmed': False,
    'account_confirmed': False,
    'risk_confirmed': False,
    'last_processed_update_id': -1,
}


def cloud_state_load():
    state = dict(STATE_DEFAULTS)
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text(encoding='utf-8'))
            for key, default in STATE_DEFAULTS.items():
                if key in saved and type(saved[key]) is type(default):
                    state[key] = saved[key]
        except Exception as exc:
            # Important: fail-closed, never reset & silently emit signals on corrupt state.
            raise RuntimeError('Cannot load persistent cloud state; paused for safety') from exc
    return state


STATE = cloud_state_load()

# Durable outbox keeps signal candidates on the mounted /data disk across restarts.
# No Telegram credentials or API keys are saved in these alert records.

def _send_alert_telegram(message):
    with core.LOCK:
        chat = core.CFG.get('owner_chat_id')
    if not chat:
        return False, 'Telegram pairing missing', 60
    try:
        response = core.telegram_call('sendMessage', {'chat_id': chat, 'text': message[:3900]}, timeout=17)
        if response.get('ok'):
            return True, '', 0
        return False, 'Telegram rejected send: '+str(response.get('error_code', 'unknown')), (response.get('parameters') or {}).get('retry_after', 0)
    except urllib.error.HTTPError as ex:
        retry = 0
        if ex.code == 429:
            try:
                payload = json.loads(ex.read().decode('utf-8'))
                retry = (payload.get('parameters') or {}).get('retry_after', 0)
            except (OSError, ValueError, AttributeError): pass
        return False, 'Telegram HTTP '+str(ex.code), retry
    except (urllib.error.URLError, TimeoutError, socket.timeout) as ex:
        return False, type(ex).__name__, 0
    except Exception as ex:
        return False, type(ex).__name__, 0

OUTBOX = Outbox(core.APP_DIR / 'pending_signals.json', _send_alert_telegram)

def _queue_signal(ident, symbol, bar_time, message):
    return OUTBOX.enqueue(ident, symbol, bar_time, message)

core.emit_alert = _queue_signal

def signal_sender():
    while True:
        try:
            # Fail closed: no pending BUY/SELL may arrive after /pause or risk lock.
            with core.LOCK:
                enabled = STATE['active']
            if not enabled or onboarding_status() or core.risk_lock_reason():
                OUTBOX.cancel_pending('alerts paused / setup missing / risk lock')
            else:
                OUTBOX.process_one()
        except Exception as ex:
            print('Alert queue issue:', type(ex).__name__, flush=True)
        time.sleep(3)


def state_save():
    with core.LOCK:
        temp = STATE_FILE.with_suffix('.tmp')
        temp.write_text(json.dumps(STATE, indent=2), encoding='utf-8')
        os.replace(temp, STATE_FILE)
        try: os.chmod(STATE_FILE, 0o600)
        except OSError: pass


def bootstrap():
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    api = os.environ.get('TWELVE_DATA_API_KEY', '').strip()
    pin = os.environ.get('PAIRING_PIN', '').strip()
    if not token or ':' not in token or len(token) > 200:
        raise ValueError('Set TELEGRAM_BOT_TOKEN in Render Environment (do NOT upload token to GitHub).')
    if not api or len(api)>150:
        raise ValueError('Set TWELVE_DATA_API_KEY in Render Environment (not in GitHub).')
    if not pin.isascii() or len(pin) != 6 or not pin.isdecimal():
        raise ValueError('Set a new 6-digit PAIRING_PIN in Render Environment (not old screenshot PIN).')
    with core.LOCK:
        if core.CFG.get('telegram_token') and core.CFG['telegram_token'] != token:
            # Token changed: don't let previous bot owner or alerts carry over.
            core.CFG['owner_chat_id'] = None
            core.CFG['last_alert_id'] = {}
            STATE.update(active=False, equity_confirmed=False, account_confirmed=False, risk_confirmed=False,
                         last_processed_update_id=-1)
        core.CFG['telegram_token'] = token
        core.CFG['api_key'] = api
        if not core.CFG.get('owner_chat_id'):
            core.CFG['pairing_pin'] = pin
        core.save_config()
        state_save()


def onboarding_status():
    with core.LOCK:
        steps=[]
        if not core.CFG.get('owner_chat_id'): steps.append('Telegram pairing')
        if not STATE['equity_confirmed']: steps.append('/capital আপনার_ইকুইটি')
        if not STATE['account_confirmed']: steps.append('/account raw অথবা /account cent')
        if not STATE['risk_confirmed']: steps.append('/risk 0.5')
        return steps


def status_text():
    with core.LOCK:
        equity=core.CFG['capital_usd']; account=core.CFG['account']; risk=core.CFG['risk_pct']
        active=STATE['active']
    lock = core.risk_lock_reason()
    steps=onboarding_status()
    errors=core.STATUS['data_errors']
    warnings='; '.join(errors[:2]) if errors else 'none'
    queue=OUTBOX.totals()
    per=core.STATUS.get('per_symbol') or {}
    details='; '.join(f'{name}:{info["state"]}' for name,info in per.items()) or 'not scanned yet'
    return (f'☁️ SNIPERFX CLOUD v2.1 • 5 PAIRS\n'
            f'Cloud: RUNNING | Alerts: {"ON" if active and not steps and not lock else "PAUSED"}\n'
            f'Equity: ${equity:.2f} | Account: {account.upper()} | Risk: {risk}%\n'
            f'Risk lock: {lock or "none"}\n'
            f'Last scan UTC: {core.STATUS["last_scan_utc"]}\n'
            f'Scanner: {core.STATUS["scan_result"]}\n'
            f'Data warnings: {warnings}\n'
            f'Pairs: {details}\n'
            f'Outbox: pending {queue["pending"]}, delivered {queue["delivered"]}, expired {queue["expired"]}, failed {queue["failed"]}, cancelled {queue["cancelled"]}\n'
            f'Setup left: {", ".join(steps) if steps else "none"}')


CLOUD_HELP = ('☁️ SNIPERFX CLOUD v2.1 — 5 PAIRS (DEMO)\n'
              '/status — Cloud + market scan status\n'
              '/capital 1000 — বর্তমান USD equity সেট করুন\n'
              '/account raw  অথবা /account cent\n'
              '/risk 0.5 — প্রতি ট্রেডে ঝুঁকি %\n'
              '/activate — সব তথ্য ঠিক থাকলে signal alerts চালু\n'
              '/pause — alert বন্ধ করুন\n'
              '/pnl -5 — বন্ধ ট্রেডের NET USD লাভ/ক্ষতি যোগ করুন\n'
              '/plan EURUSD 1.15000 1.14800 — শুধু lot/SL/TP হিসাব\n'
              '/symbols /help — ৫টি Market ও তথ্য\n'
              '/diag — প্রতি symbol-এর শেষ পরীক্ষা ও queue\n'
              '⚠️ MT5 auto trade নয়; Exness live quotes আলাদা হতে পারে।')


def command(textmsg, chat_id):
    parts=textmsg.strip().split()
    if not parts: return
    cmd=parts[0].split('@')[0].lower()
    if cmd == '/start':
        core.handle_command(textmsg, chat_id)
        with core.LOCK:
            paired = core.CFG.get('owner_chat_id') == chat_id
        if paired:
            core.tg_send('\n☁️ Cloud commands:\n'+CLOUD_HELP)
        return
    with core.LOCK:
        if chat_id != core.CFG.get('owner_chat_id'):
            return
    if cmd in ('/status', '/health'):
        return core.tg_send(status_text())
    if cmd in ('/diag', '/diagnostics'):
        lines=['🔎 SniperFx per-symbol diagnostics (UTC)']
        for sym in core.SYMBOLS:
            info=(core.STATUS.get('per_symbol') or {}).get(sym, {})
            lines.append(f'{sym}: {info.get("state","not scanned yet")} | last closed 15M bar: {info.get("bar","-")}')
            if info.get('error'):
                lines.append('  ⚠ '+info['error'][:125])
        lines.append('Durable alert queue: '+str(OUTBOX.totals()))
        lines.append('⚠️ Data API and broker fills are not guaranteed identical.')
        return core.tg_send('\n'.join(lines))
    if cmd == '/help':
        return core.tg_send(CLOUD_HELP)
    if cmd == '/activate':
        steps=onboarding_status()
        lock=core.risk_lock_reason()
        if steps: return core.tg_send('🛑 আগে সেটআপ শেষ করুন: '+', '.join(steps))
        if lock: return core.tg_send('🛑 Risk lock active; signals বন্ধ: '+lock)
        with core.LOCK:
            STATE['active']=True
            state_save()
        return core.tg_send('✅ DEMO alert scanning ON. বাজারে শর্ত না মিললে কোনো সিগন্যাল আসবে না।')
    if cmd == '/pause':
        with core.LOCK:
            STATE['active']=False
            state_save()
        OUTBOX.cancel_pending('/pause')
        return core.tg_send('⏸️ Alerts PAUSED. Pending candidates cancelled. আবার চালু করতে /activate পাঠান।')
    if cmd == '/capital' and len(parts)==2:
        try:
            equity=float(parts[1]); assert math.isfinite(equity) and 1<=equity<=1e8
        except (ValueError,AssertionError):
            return core.tg_send('ভুল equity. উদাহরণ: /capital 1000 (USD, USC নয়)')
        core.handle_command(textmsg, chat_id)
        with core.LOCK:
            STATE['equity_confirmed']=True
            state_save()
        return
    if cmd == '/account' and len(parts)==2 and parts[1].lower() in ('cent','raw'):
        core.handle_command(textmsg, chat_id)
        with core.LOCK:
            STATE['account_confirmed']=True
            state_save()
        return
    if cmd == '/risk' and len(parts)==2:
        try:
            risk=float(parts[1]); assert math.isfinite(risk) and 0.25<=risk<=1
        except (ValueError,AssertionError):
            return core.tg_send('ভুল risk. ব্যবহার করুন: /risk 0.5')
        core.handle_command(textmsg, chat_id)
        with core.LOCK:
            STATE['risk_confirmed']=True
            state_save()
        return
    # Existing /pnl,/plan,/symbols etc. Restarts persist equity in volume.
    return core.handle_command(textmsg, chat_id)


def telegram_poll():
    """Persist a high-water mark BEFORE acting to avoid replaying /pnl on restart.
    Crash between marking and acting can skip a command; user can check /status.
    """
    with core.LOCK:
        offset = STATE['last_processed_update_id']+1 if STATE['last_processed_update_id']>=0 else None
    consecutive_errors=0
    while True:
        try:
            params={'timeout':22,'limit':30}
            if offset is not None:params['offset']=offset
            result=core.telegram_call('getUpdates',params,timeout=29)
            if not result.get('ok'): raise RuntimeError('Telegram polling refused: '+str(result.get('description', ''))[:100])
            consecutive_errors=0
            core.STATUS['telegram_poll']='Cloud polling connected'
            core.STATUS['telegram_last_ok']=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
            core.STATUS['telegram_error']=''
            for upd in result.get('result',[]):
                uid=upd.get('update_id')
                if not isinstance(uid,int):continue
                offset=uid+1
                with core.LOCK:
                    if uid <= STATE['last_processed_update_id']:
                        continue
                    STATE['last_processed_update_id']=uid
                    state_save()  # acknowledge locally before effect, prevents replay
                msg=upd.get('message') or upd.get('edited_message') or {}
                chat=msg.get('chat') or {}
                if isinstance(msg.get('text'),str) and isinstance(chat.get('id'),int):
                    try:command(msg['text'], chat['id'])
                    except Exception as exc:
                        print('Command processing failed:',type(exc).__name__,flush=True)
        except Exception as exc:
            consecutive_errors+=1
            core.STATUS['telegram_error']=str(exc)[:130]
            code = f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            print('Telegram polling issue:', code,flush=True)
            # Back off on Telegram 429/502 and disconnects; retry automatically.
            time.sleep(min(90, 5*consecutive_errors))


def scanner():
    """Run on closed 15m checkpoints. Retry failed data cycles inside the slot."""
    last_complete_slot = None
    current_slot = None
    attempts = 0
    next_try = 0.0
    while True:
        now=dt.datetime.now(dt.timezone.utc)
        slot=int((now.timestamp()-95)//900)
        if slot != current_slot:
            current_slot=slot
            attempts=0
            next_try=0
        if slot != last_complete_slot and attempts < 3 and time.monotonic() >= next_try:
            with core.LOCK:
                active=STATE['active']
            if active and not onboarding_status():
                try:
                    result=core.scan_cycle()
                    attempts += 1
                    if result and result['errors']:
                        # Missing candles can be retried while the same closed bar
                        # is still relevant; no rule/indicator thresholds change.
                        next_try=time.monotonic()+65
                    else:
                        last_complete_slot=slot
                except Exception as exc:
                    attempts += 1
                    next_try=time.monotonic()+65
                    core.STATUS['scan_result']='Scanner error; retry scheduled'
                    core.STATUS['data_errors']=[type(exc).__name__]
                    print('Scanner failed:', type(exc).__name__, flush=True)
            else:
                core.STATUS['scan_result']='Paused / waiting for /activate'
                last_complete_slot=slot
        time.sleep(5)


def main():
    bootstrap()
    print('SNIPERFX CLOUD v2.1: ready; no public dashboard, secrets never printed.',flush=True)
    print('Cloud persistent disk: detected. Telegram requires /start <new PIN> on first install.',flush=True)
    threading.Thread(target=telegram_poll,daemon=True).start()
    threading.Thread(target=scanner,daemon=True).start()
    threading.Thread(target=signal_sender,daemon=True).start()
    while True: time.sleep(30)


if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError) as err:
        print('SETUP ERROR:',str(err),flush=True)
        sys.exit(2)
