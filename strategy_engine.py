#!/usr/bin/env python3
"""Sniper Intelligence v1 - educational alert bot, no brokerage execution.
Python standard library only. Twelve Data candle feed + Telegram Bot API.
"""
import datetime as dt
import html
import json
import math
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(os.environ.get('APPDATA', str(Path.home() / '.sniper_intelligence'))) / 'SniperIntelligence'
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / 'settings.json'
UTC = dt.timezone.utc
LOCK = threading.RLock()
SYMBOLS = {'XAUUSD': 'XAU/USD', 'BTCUSD': 'BTC/USD', 'EURUSD': 'EUR/USD',
           'USDJPY': 'USD/JPY', 'AUDUSD': 'AUD/USD'}
DEFAULT_COSTS = {
    'XAUUSD': {'spread': 0.50, 'slippage': 0.30, 'raw_commission': 7.0},
    'BTCUSD': {'spread': 25.0, 'slippage': 15.0, 'raw_commission': 4.0},
    'EURUSD': {'spread': 0.00015, 'slippage': 0.00008, 'raw_commission': 7.0},
    # Assumed reserve, NOT broker live spread or actual commission. Verify Exness specs.
    'USDJPY': {'spread': 0.020, 'slippage': 0.010, 'raw_commission': 7.0},
    'AUDUSD': {'spread': 0.00015, 'slippage': 0.00008, 'raw_commission': 7.0},
}
DEFAULTS = dict(api_key='', telegram_token='', owner_chat_id=None,
                pairing_pin='', capital_usd=100.0, account='cent', risk_pct=0.5,
                starting_equity_usd=100.0, initial_equity_usd=100.0, baseline_initialized=False,
                daily_start_usd=100.0, weekly_start_usd=100.0,
                daily_date='', week_key='', consecutive_losses=0,
                max_daily_loss_pct=2.0, max_weekly_loss_pct=5.0,
                max_total_drawdown_pct=10.0, costs=DEFAULT_COSTS,
                last_alert_id={}, last_error='')
CACHE = {}
STATUS = {'last_scan_utc': '-', 'scan_result': 'Not running yet', 'data_errors': [], 'alerts_sent': 0, 'per_symbol': {}, 'cycle_errors': 0, 'telegram_poll': 'Waiting for Telegram token', 'telegram_last_ok': '-', 'telegram_error': ''}
API_RATE_LOCK = threading.Lock()
LAST_API_CALL = [0.0]


def load_config():
    data = dict(DEFAULTS)
    data['costs'] = {k: dict(v) for k, v in DEFAULT_COSTS.items()}
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            for k, v in saved.items():
                if k in data and k != 'costs':
                    data[k] = v
            for k, v in saved.get('costs', {}).items():
                if k in data['costs'] and isinstance(v, dict):
                    for name in ('spread', 'slippage', 'raw_commission'):
                        if name in v:
                            data['costs'][k][name] = float(v[name])
        except Exception as exc:
            print('Could not read settings:', exc)
    if not data.get('pairing_pin'):
        data['pairing_pin'] = str(secrets.randbelow(900000) + 100000)
    return data


CFG = load_config()


def save_config():
    with LOCK:
        tmp = CONFIG_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(CFG, indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, CONFIG_FILE)
        if os.name == 'posix':
            try: os.chmod(CONFIG_FILE, 0o600)
            except OSError: pass


save_config()


def http_json(url, params=None, timeout=25):
    if params:
        url = url + ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'SniperIntelligence/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def telegram_call(method, params=None, timeout=30):
    with LOCK:
        token = CFG.get('telegram_token', '')
    if not token:
        raise RuntimeError('Telegram token not configured')
    url = 'https://api.telegram.org/bot' + token + '/' + method
    return http_json(url, params, timeout=timeout)


def telegram_reply(chat_id, msg):
    """Respond to a Telegram chat before pairing, without exposing keys or the PIN."""
    try:
        result=telegram_call('sendMessage', {'chat_id': chat_id, 'text': msg[:3900]})
        return bool(result.get('ok'))
    except Exception as exc:
        print('Telegram reply error:', type(exc).__name__)
        return False


def tg_send(msg):
    with LOCK:
        chat_id = CFG.get('owner_chat_id')
    if not chat_id:
        return False
    try:
        result = telegram_call('sendMessage', {'chat_id': chat_id, 'text': msg[:3900]})
        return bool(result.get('ok'))
    except Exception as exc:
        print('Telegram send error:', str(exc)[:180])
        return False


def ema(values, period):
    if len(values) < period: raise ValueError('not enough EMA values')
    current = sum(values[:period]) / period
    factor = 2 / (period + 1)
    for price in values[period:]:
        current = price * factor + current * (1-factor)
    return current


def rsi(values, period=14):
    if len(values) < period+1: raise ValueError('not enough RSI values')
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_up = sum(gains[:period])/period
    avg_down = sum(losses[:period])/period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_up = (avg_up*(period-1)+gain)/period
        avg_down = (avg_down*(period-1)+loss)/period
    if avg_down == 0: return 100 if avg_up > 0 else 50
    return 100 - (100/(1+(avg_up/avg_down)))


def atr(candles, period=14):
    if len(candles) < period+1: raise ValueError('not enough ATR candles')
    ranges = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i-1]
        ranges.append(max(c['high']-c['low'], abs(c['high']-p['close']), abs(c['low']-p['close'])))
    avg = sum(ranges[:period])/period
    for tr in ranges[period:]: avg = ((period-1)*avg+tr)/period
    return avg


def fetch_candles(symbol, interval, now=None):
    if now is None: now = dt.datetime.now(UTC)
    with LOCK: key = CFG['api_key']
    if not key: raise RuntimeError('Twelve Data API key is missing')
    # Twelve Data Basic allows 8 API credits/minute. Pace requests globally.
    with API_RATE_LOCK:
        delay = 9.0 - (time.monotonic() - LAST_API_CALL[0])
        if delay > 0: time.sleep(delay)
        LAST_API_CALL[0] = time.monotonic()
    request_args = {
        'symbol': SYMBOLS[symbol], 'interval': interval,
        'outputsize': 235 if interval in ('1h','4h') else 125,
        'timezone': 'UTC', 'apikey': key}
    # Retry transient failures with pacing; never log API keys.
    for attempt in range(3):
        try:
            if attempt:
                with API_RATE_LOCK:
                    delay = 9.0 - (time.monotonic() - LAST_API_CALL[0])
                    if delay > 0: time.sleep(delay)
                    LAST_API_CALL[0] = time.monotonic()
            result = http_json('https://api.twelvedata.com/time_series', request_args, timeout=12)
            if result.get('status') == 'error':
                code = result.get('code')
                if code in (429, 500, 502, 503, 504) and attempt<2:
                    time.sleep(3*(attempt+1))
                    continue
            break
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt == 2:
                raise RuntimeError(f'{symbol} {interval}: {type(exc).__name__} after 3 tries') from None
            time.sleep(3*(attempt+1))
    if result.get('status') == 'error' or 'values' not in result:
        raise RuntimeError(str(result.get('message', 'No candles returned'))[:180])
    period_sec = {'15min': 900, '1h':3600, '4h':14400}[interval]
    candles = []
    for bar in result['values']:
        try:
            opened = dt.datetime.fromisoformat(bar['datetime']).replace(tzinfo=UTC)
            # Twelve Data timestamps are candle OPEN time. Exclude still-open candles.
            if opened + dt.timedelta(seconds=period_sec + 90) > now:
                continue
            candles.append({
                't': opened.isoformat(), 'open': float(bar['open']), 'high': float(bar['high']),
                'low': float(bar['low']), 'close': float(bar['close'])})
        except (KeyError, ValueError, TypeError):
            continue
    candles.sort(key=lambda x:x['t'])
    if len(candles) < (210 if interval in ('1h','4h') else 40):
        raise RuntimeError(f'Insufficient closed {interval} candles ({len(candles)})')
    allowed_gap = {'15min':dt.timedelta(minutes=65), '1h':dt.timedelta(hours=4), '4h':dt.timedelta(hours=13)}[interval]
    last = dt.datetime.fromisoformat(candles[-1]['t'])
    if now-last > allowed_gap:
        raise RuntimeError(f'{interval} data stale (latest: {last.isoformat()})')
    return candles


def is_market_hours(symbol, now):
    if symbol == 'BTCUSD': return True
    # Conservative generic session exclusion. Exact broker holiday hours may differ.
    if now.weekday() in (5,6): return False
    if now.weekday() == 4 and now.hour >= 20: return False
    if now.weekday() == 0 and now.hour < 2: return False
    return True


def signal_strategy(symbol, c15, c1h, c4h):
    """Conservative unvalidated sweep-in-trend pattern; only closed bars."""
    closes4 = [c['close'] for c in c4h]
    closes1 = [c['close'] for c in c1h]
    closes15 = [c['close'] for c in c15]
    long_bias = closes4[-1] > ema(closes4, 200) and ema(closes4, 50) > ema(closes4, 200) and closes1[-1] > ema(closes1, 50) and ema(closes1, 20) > ema(closes1, 50)
    short_bias = closes4[-1] < ema(closes4, 200) and ema(closes4, 50) < ema(closes4, 200) and closes1[-1] < ema(closes1, 50) and ema(closes1, 20) < ema(closes1, 50)
    last = c15[-1]
    previous = c15[-13:-1]
    a = atr(c15)
    if a <= 0: return None
    candle_range = last['high'] - last['low']
    if not (0.5*a <= candle_range <= 2.5*a): return None
    min_low = min(c['low'] for c in previous)
    max_high = max(c['high'] for c in previous)
    current_rsi = rsi(closes15)
    body = abs(last['close']-last['open'])
    if body < 0.15*candle_range: return None
    buy = (long_bias and last['low'] < min_low and last['close'] > min_low
           and last['close'] > last['open'] and 45 <= current_rsi <= 70
           and (min(last['open'],last['close'])-last['low']) >= 0.20*candle_range)
    sell = (short_bias and last['high'] > max_high and last['close'] < max_high
            and last['close'] < last['open'] and 30 <= current_rsi <= 55
            and (last['high']-max(last['open'],last['close'])) >= 0.20*candle_range)
    if not buy and not sell: return None
    direction = 'BUY' if buy else 'SELL'
    entry = last['close']
    sl = last['low']-0.2*a if buy else last['high']+0.2*a
    distance = abs(entry-sl)
    if not (0.45*a <= distance <= 2.8*a): return None
    return {'symbol':symbol, 'side':direction, 'entry':entry, 'sl':sl,
            'atr':a, 'bar_time':last['t'], 'rsi':round(current_rsi,1),
            'logic':'4H+1H trend aligned; 15M liquidity sweep + rejection candle'}


def calc_lot(symbol, entry, sl, equity, account, risk_pct, cost_config):
    """Cost-budget sizing, USD quote pairs, with 0.01 volume step, NO rounding up."""
    if not all(math.isfinite(v) for v in (entry,sl,equity,risk_pct)) or equity<=0 or entry<=0 or sl<=0 or entry==sl:
        return {'ok':False,'reason':'Invalid entry, stop, balance or risk'}
    if symbol not in SYMBOLS or account not in ('cent','raw') or not (0<risk_pct<=1):
        return {'ok':False,'reason':'Unsupported account or risk >1%'}
    # standard: FX 100,000 base units, gold 100oz, BTC 1 BTC.
    # cent: 1 cent lot = 0.01 standard lot. NOT same lot units as Raw.
    standard = {'EURUSD':100000, 'XAUUSD':100, 'BTCUSD':1,
                'USDJPY':100000, 'AUDUSD':100000}[symbol]
    contract = standard * (0.01 if account == 'cent' else 1)
    cost = cost_config[symbol]
    extra_move = float(cost['spread']) + float(cost['slippage'])
    commission = float(cost['raw_commission']) * (0.01 if account == 'cent' else 1) if account=='raw' else 0
    # commission is round-trip USD per unit of displayed account lot
    # USDJPY P/L is JPY, not USD. Convert conservatively at the lower
    # of entry and SL; all other supported symbols quote in USD.
    quote_to_usd = 1.0 / min(entry, sl) if symbol == 'USDJPY' else 1.0
    per_lot_risk = (abs(entry-sl)+extra_move)*contract*quote_to_usd+commission
    cap_risk = equity*risk_pct/100
    if not math.isfinite(per_lot_risk) or per_lot_risk <=0 or extra_move<0 or commission<0:
        return {'ok':False,'reason':'Invalid cost assumptions'}
    lots = math.floor((cap_risk/per_lot_risk + 1e-10)*100)/100
    if lots < 0.01:
        return {'ok':False, 'reason':f'Minimum 0.01 lot exceeds risk budget (${cap_risk:.2f}); NO TRADE',
                'min_lot_risk':round(0.01*per_lot_risk,4),'budget':round(cap_risk,4)}
    lots = min(200, lots)
    estimated_loss = lots*per_lot_risk
    def target(r):
        # include estimated execution costs in TP required to reach approximate net R.
        dist = (r*per_lot_risk + extra_move*contract*quote_to_usd+commission)/(contract*quote_to_usd)
        return entry+dist if entry>sl else entry-dist
    return {'ok': True, 'lots':round(lots,2), 'risk':round(estimated_loss,4),
            'budget':round(cap_risk,4),'risk_pct_actual':round(100*estimated_loss/equity,4),
            'tp15':target(1.5), 'tp2':target(2), 'tp3':target(3), 'tp4':target(4),
            'contract_per_lot':contract, 'per_lot_loss':per_lot_risk}


def now_calendar():
    now = dt.datetime.now(UTC)
    return now, now.strftime('%Y-%m-%d'), f'{now.isocalendar().year}-W{now.isocalendar().week:02d}'


def ensure_calendar():
    _, day, week = now_calendar()
    with LOCK:
        if CFG.get('daily_date') != day:
            CFG['daily_date'] = day
            CFG['daily_start_usd'] = float(CFG['capital_usd'])
        if CFG.get('week_key') != week:
            CFG['week_key'] = week
            CFG['weekly_start_usd'] = float(CFG['capital_usd'])
        save_config()


def risk_lock_reason():
    ensure_calendar()
    with LOCK:
        equity=float(CFG['capital_usd'])
        dstart=float(CFG['daily_start_usd'])
        wstart=float(CFG['weekly_start_usd'])
        initial=float(CFG['initial_equity_usd'])
        streak=int(CFG['consecutive_losses'])
    reasons=[]
    if dstart>0 and equity <= dstart*0.98: reasons.append('Daily loss reached 2%')
    if wstart>0 and equity <= wstart*0.95: reasons.append('Weekly loss reached 5%')
    if initial>0 and equity <= initial*0.90: reasons.append('Account drawdown from initial equity >=10%')
    if streak>=3: reasons.append('3 consecutive losing trades')
    return '; '.join(reasons)


def format_number(v, symbol):
    return f'{v:.5f}' if symbol in ('EURUSD','AUDUSD') else (f'{v:.3f}' if symbol=='USDJPY' else (f'{v:.2f}' if symbol=='XAUUSD' else f'{v:.1f}'))


def make_alert(s, p):
    sym=s['symbol']
    symbol_suffix='c' if CFG['account']=='cent' else ''
    bdtime = (dt.datetime.fromisoformat(s['bar_time']) + dt.timedelta(hours=6)).strftime('%Y-%m-%d %H:%M')
    return (f'🎯 SNIPER SIGNAL CANDIDATE • {sym}{symbol_suffix} • {s["side"]}\n'
            f'অবস্থা: পরীক্ষামূলক সংকেত • আগে DEMO-তে পরীক্ষা করুন\n'
            f'15M bar: {s["bar_time"]} UTC / {bdtime} BDT (open time)\n'
            f'সম্ভাব্য Entry (reference): {format_number(s["entry"], sym)}\n'
            f'SL reference: {format_number(s["sl"], sym)}\n'
            f'TP1 (≈1:1.5 net): {format_number(p["tp15"], sym)}\n'
            f'TP2 (≈1:2 net): {format_number(p["tp2"], sym)}\n'
            f'TP3 (≈1:3 net): {format_number(p["tp3"], sym)}\n'
            f'TP4 (≈1:4 net): {format_number(p["tp4"], sym)}\n'
            f'পরিকল্পিত Lot: {p["lots"]:.2f} {"CENT" if CFG["account"]=="cent" else "RAW"} lots\n'
            f'আনুমানিক সর্বোচ্চ পরিকল্পিত ক্ষতি: ${p["risk"]:.2f} ({p["risk_pct_actual"]:.3f}% equity)\n'
            f'বর্তমান Equity (ম্যানুয়ালি দেওয়া): ${CFG["capital_usd"]:.2f}\n'
            f'ডেটা: Twelve Data, Exness-এর লাইভ executable price নয়।\n'
            f'⚠️ অর্ডার দেওয়ার আগে Exness bid/ask, spread, margin, commission, stop distance ও news যাচাই করুন। '
            f'Entry price বদলালে পুরোনো lot ব্যবহার নয়। SL-এ slippage হতে পারে; অন্ধভাবে কপি করবেন না।')


def emit_alert(ident, symbol, bar_time, msg):
    # The local legacy runner sends directly; the cloud runner replaces this
    # hook with a durable pending-signal queue. Trading logic is unchanged.
    return tg_send(msg)


def scan_cycle(force=False):
    now=dt.datetime.now(UTC)
    with LOCK:
        api=CFG['api_key']
        paired=CFG.get('owner_chat_id') is not None
    if not api:
        STATUS['scan_result']='API key needed'; return
    if not paired:
        STATUS['scan_result']='Telegram pairing needed'; return
    lock = risk_lock_reason()
    if lock:
        STATUS['scan_result']='Risk lock: '+lock; return
    alerts=0
    errors=[]
    per_symbol = {}
    for symbol in SYMBOLS:
        if not is_market_hours(symbol,now):
            per_symbol[symbol] = {'state':'market closed', 'bar':'-', 'error':''}
            continue
        bars={}
        for interval, ttl in [('15min',900),('1h',3600),('4h',14400)]:
            key=(symbol,interval)
            # Fetch on launch, or after previous candle could have closed.
            existing=CACHE.get(key)
            # Refresh when a NEW closed candle should exist, not after arbitrary TTL.
            expected_open = int((now.timestamp()-ttl-95)//ttl)*ttl
            if existing and dt.datetime.fromisoformat(existing['bars'][-1]['t']).timestamp() >= expected_open:
                bars[interval]=existing['bars']; continue
            try:
                data=fetch_candles(symbol,interval,now)
                CACHE[key]={'fetched':now.timestamp(),'bars':data}
                bars[interval]=data
            except Exception as exc:
                errors.append(f'{symbol} {interval}: {str(exc)[:100]}')
                # Never use stale data to issue new alerts.
        if len(bars)!=3:
            per_symbol[symbol] = {'state':'data unavailable', 'bar':'-',
                                  'error':'; '.join(e for e in errors if e.startswith(symbol+' '))[-170:]}
            continue
        per_symbol[symbol] = {'state':'evaluated', 'bar':bars['15min'][-1]['t'], 'error':''}
        try:
            s=signal_strategy(symbol,bars['15min'],bars['1h'],bars['4h'])
            if not s: continue
            ident=symbol+'/'+s['bar_time']+'/'+s['side']
            with LOCK:
                if CFG['last_alert_id'].get(symbol)==ident: continue
                p=calc_lot(symbol,s['entry'],s['sl'],float(CFG['capital_usd']),CFG['account'],float(CFG['risk_pct']),CFG['costs'])
                if not p['ok']:
                    STATUS['scan_result']=f'{symbol} pattern found but risk gate says NO TRADE: '+p['reason']
                    continue
                msg=make_alert(s,p)
            if emit_alert(ident, symbol, s['bar_time'], msg):
                alerts+=1
                with LOCK:
                    CFG['last_alert_id'][symbol]=ident
                    save_config()
        except Exception as exc:
            errors.append(f'{symbol}: {str(exc)[:120]}')
            per_symbol[symbol]['state']='evaluation error'
            per_symbol[symbol]['error']=str(exc)[:120]
    STATUS.update(last_scan_utc=now.isoformat(timespec='minutes'),
                  scan_result=f'Evaluated {sum(v["state"]=="evaluated" for v in per_symbol.values())}/{len(SYMBOLS)} active symbol(s); {alerts} new candidate(s) queued',
                  per_symbol=per_symbol, cycle_errors=len(errors),
                  data_errors=errors[-8:])
    if errors: print('Data warnings:', '; '.join(errors[:3]), flush=True)
    return {'evaluated': sum(v['state']=='evaluated' for v in per_symbol.values()),
            'errors': errors, 'candidates': alerts, 'per_symbol': per_symbol}


def scan_loop():
    # Align to 15m closed-candle checkpoints (UTC + 95 sec) to avoid incomplete bars.
    last_slot=None
    while True:
        now=dt.datetime.now(UTC)
        slot=int((now.timestamp()-95)//900)
        if last_slot != slot:
            last_slot=slot
            try:scan_cycle()
            except Exception as exc:print('Scan loop warning:',str(exc)[:150])
        time.sleep(8)


HELP = ('SNIPER INTELLIGENCE 🇧🇩\n'
        '/status - system & safety status\n'
        '/capital 100 - set current USD equity (compounding)\n'
        '/risk 0.5 - risk % per trade (0.25-1)\n'
        '/account cent or /account raw\n'
        '/pnl -2.50 - record NET closed P/L ($), compounding\n'
        '/plan XAUUSD 4060 4055 - custom lot/SL/TP calculation\n'
        '/symbols - supported instruments\n'
        '/help - help\n'
        'No account passwords. Demo only; no auto trading.\n'
        'Actual market quotes in Exness may differ.')


def handle_command(textmsg, chat_id):
    parts=textmsg.strip().split()
    if not parts: return
    cmd=parts[0].split('@')[0].lower()
    if cmd=='/start':
        with LOCK:
            owner=CFG.get('owner_chat_id')
            pin=CFG['pairing_pin']
            if owner is None and len(parts)>1 and secrets.compare_digest(parts[1],pin):
                CFG['owner_chat_id']=chat_id
                CFG['pairing_pin']=str(secrets.randbelow(900000)+100000)
                save_config()
                tg_send('✅ আপনার Telegram সফলভাবে যুক্ত হয়েছে!\n'+HELP)
                return
        if owner==chat_id:
            tg_send(HELP)
        elif owner is not None:
            telegram_reply(chat_id, '⛔ এই Bot আগেই অন্য Telegram chat-এর সঙ্গে যুক্ত আছে।')
        elif len(parts)==1:
            telegram_reply(chat_id, '✅ Bot চালু আছে, কিন্তু আপনার Telegram এখনো যুক্ত হয়নি।\n\nকম্পিউটারে http://127.0.0.1:8765 খুলে ৬ সংখ্যার PIN দেখুন।\nএকই মেসেজে লিখুন: /start 123456\n(123456-এর জায়গায় আপনার PIN দিন)')
        else:
            telegram_reply(chat_id, '❌ PIN মেলেনি। কম্পিউটারে setup page-এর বর্তমান ৬ সংখ্যার PIN দেখে আবার পাঠান: /start PIN')
        return
    with LOCK: owner=CFG.get('owner_chat_id')
    if chat_id!=owner:
        if owner is None and (cmd=='start' or cmd.isdigit()):
            telegram_reply(chat_id, '⚠️ শুধু PIN বা start PIN লিখলে হবে না। একই মেসেজে স্ল্যাশসহ পাঠান: /start 123456\nসঠিক PIN দেখুন কম্পিউটারের setup page-এ।')
        return
    if cmd=='/help': return tg_send(HELP)
    if cmd=='/symbols':return tg_send('XAUUSD (gold), BTCUSD (Bitcoin), EURUSD, USDJPY, AUDUSD.\nBroker: Exness MT5; data: Twelve Data external candle feed.')
    if cmd=='/status':
        with LOCK:
            eq=CFG['capital_usd']; acc=CFG['account']; pct=CFG['risk_pct']
        lock=risk_lock_reason()
        return tg_send(f'📊 Equity ${eq:.2f} | Account {acc.upper()} | Risk {pct}%\n'
                       f'Risk limit: {lock or "Not triggered"}\n'
                       f'Last scan: {STATUS["last_scan_utc"]}\n{STATUS["scan_result"]}\n'
                       f'Warnings: {"; ".join(STATUS["data_errors"][:2]) if STATUS["data_errors"] else "none"}')
    if cmd=='/capital':
        if len(parts)!=2: return tg_send('Example: /capital 100')
        try:
            x=float(parts[1]); assert math.isfinite(x) and 1<=x<=1e8
            with LOCK:
                CFG['capital_usd']=x
                if not CFG.get('baseline_initialized'):
                    now, day, week = now_calendar()
                    CFG.update(initial_equity_usd=x, daily_start_usd=x, weekly_start_usd=x, daily_date=day, week_key=week, baseline_initialized=True)
                save_config()
            return tg_send(f'✅ Current equity updated to ${x:,.2f}. Use real Exness EQUITY in USD, not deposit total.')
        except (ValueError,AssertionError):return tg_send('Enter USD equity 1 to 100,000,000. /capital 100')
    if cmd=='/risk':
        try:
            x=float(parts[1]); assert math.isfinite(x) and 0.25<=x<=1.0
            with LOCK:CFG['risk_pct']=x;save_config()
            return tg_send(f'✅ Planned risk per trade: {x:.2f}%.')
        except (IndexError,ValueError,AssertionError):return tg_send('Allowed 0.25% to 1%. Example: /risk 0.5')
    if cmd=='/account':
        if len(parts)!=2 or parts[1].lower() not in ('cent','raw'):return tg_send('Use /account cent or /account raw')
        with LOCK:CFG['account']=parts[1].lower();save_config()
        return tg_send('✅ Account mode: '+CFG['account'].upper()+' (verify instrument specs in Exness).')
    if cmd=='/pnl':
        try:
            value=float(parts[1]);assert math.isfinite(value) and abs(value)<=1e8
            with LOCK:
                old=float(CFG['capital_usd']);new=old+value
                if new<=0: return tg_send('Invalid P/L: equity must stay positive.')
                CFG['capital_usd']=new
                if not CFG.get('baseline_initialized'):
                    now, day, week = now_calendar()
                    CFG.update(initial_equity_usd=old, daily_start_usd=old, weekly_start_usd=old, daily_date=day, week_key=week, baseline_initialized=True)
                CFG['consecutive_losses']=CFG['consecutive_losses']+1 if value<0 else (0 if value>0 else CFG['consecutive_losses'])
                save_config()
            return tg_send(f'✅ NET realized P/L ${value:+.2f}. New equity ${new:.2f}.\nRisk lock: {risk_lock_reason() or "none"}.')
        except (IndexError,ValueError,AssertionError):return tg_send('Use net profit/loss in USD, e.g. /pnl -2.50')
    if cmd=='/plan':
        lock = risk_lock_reason()
        if lock: return tg_send('🛑 RISK LOCK ACTIVE • NO TRADE: '+lock)
        if len(parts)!=4: return tg_send('Example: /plan XAUUSD 4060 4055')
        try:
            sym=parts[1].upper(); entry=float(parts[2]); stop=float(parts[3])
            with LOCK:
                p=calc_lot(sym,entry,stop,CFG['capital_usd'],CFG['account'],CFG['risk_pct'],CFG['costs'])
            if not p['ok']:return tg_send('🛑 NO TRADE: '+p['reason'])
            side='BUY' if entry>stop else 'SELL'
            return tg_send(f'📏 {sym} {side} MANUAL PLAN\nEntry {format_number(entry,sym)} | SL {format_number(stop,sym)}\n'
                           f'Lot {p["lots"]:.2f} {CFG["account"].upper()} | estimated loss ${p["risk"]:.2f}\n'
                           f'TP1 {format_number(p["tp15"],sym)} | TP2 {format_number(p["tp2"],sym)} | TP3 {format_number(p["tp3"],sym)}\n'
                           'This is sizing only, NOT a market signal. Verify spread & broker specs.')
        except (ValueError,KeyError,TypeError):return tg_send('Example: /plan EURUSD 1.09000 1.08800')
    tg_send('Unknown command. Use /help')


def telegram_loop():
    offset=None
    previous_token=None
    while True:
        with LOCK: token=CFG.get('telegram_token','')
        if token != previous_token:
            offset=None  # Token change means a different bot and different update IDs.
            previous_token=token
        if not token:
            STATUS['telegram_poll']='Token not saved'
            STATUS['telegram_error']=''
            time.sleep(3);continue
        try:
            # getUpdates works only if webhook is not configured.
            params={'timeout':22,'limit':30}
            if offset is not None:params['offset']=offset
            res=telegram_call('getUpdates', params, timeout=29)
            if not res.get('ok'):
                raise RuntimeError(str(res.get('description','Telegram rejected request')))
            STATUS['telegram_poll']='Connected (polling OK)'
            STATUS['telegram_last_ok']=dt.datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')
            STATUS['telegram_error']=''
            for upd in res.get('result',[]):
                offset=max(offset or 0,upd['update_id']+1)
                message=upd.get('message',{})
                body=message.get('text','')
                cid=message.get('chat',{}).get('id')
                if body and cid is not None:
                    try:handle_command(body,cid)
                    except Exception as exc:print('Command problem:',str(exc)[:150])
        except Exception as exc:
            # Do not include the request URL or bot token in displayed diagnostics.
            safe_message=('Telegram HTTP '+str(exc.code)) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            STATUS['telegram_poll']='Connection error'
            STATUS['telegram_error']=safe_message
            print('Telegram polling issue:',safe_message)
            time.sleep(7)


PAGE_STYLE = '''<style>
:root{font-family:system-ui,'Segoe UI',sans-serif;color-scheme:dark;background:#0b1321;color:#e8f1fc}*{box-sizing:border-box}body{max-width:950px;margin:24px auto;padding:0 16px}h1{font-size:clamp(1.5rem,4vw,2.3rem);margin-bottom:6px}h2{font-size:1.18rem;margin-top:0}p{line-height:1.55}small,.muted{color:#9caec2}.box{border:1px solid #34465d;background:#132238;border-radius:17px;padding:20px;margin:16px 0}.split{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}.row{display:flex;justify-content:space-between;gap:8px;margin:7px 0}label{display:block;margin:12px 0 5px;color:#b8c9da}input,select{width:100%;background:#0c1b2a;border:1px solid #57687a;border-radius:9px;color:#fff;padding:12px;font-size:15px}button{cursor:pointer;color:#041a23;background:#6bddc7;border:0;padding:12px 18px;border-radius:9px;font-weight:800;margin-top:14px}.warn{color:#ffd994}.ok{color:#8de0c6}a{color:#8ee0f8}.tag{display:inline-block;background:#263f55;border-radius:10px;padding:5px 9px;margin-right:6px}code{color:#a5dfeb}ul{line-height:1.8}</style>'''


def admin_page(msg=''):
    with LOCK:
        c=dict(CFG)
        c['costs']={k:dict(v) for k,v in CFG['costs'].items()}
    def field(name,label,typ='text',value='',minval=None,maxval=None):
        prop=(' min="'+str(minval)+'"' if minval is not None else '')+(' max="'+str(maxval)+'"' if maxval is not None else '')
        return f'<label>{html.escape(label)}</label><input name="{html.escape(name)}" type="{typ}" value="{html.escape(str(value),quote=True)}" step="any"{prop}>'
    account_opt = ''.join(f'<option value="{x}" {"selected" if c["account"]==x else ""}>{name}</option>' for x,name in [('cent','Exness Standard Cent (USC)'),('raw','Exness Raw Spread (USD)')])
    cost_inputs=''.join(f'<div class="box"><h2>{sym} • Assumptions (NOT live Exness cost)</h2>'+''.join(field(f'{sym}_{k}',label,'number',c['costs'][sym][k],0) for k,label in [('spread','Spread price distance'),('slippage','Extra adverse fill price distance'),('raw_commission','RAW round-trip USD commission per 1 lot')])+'</div>' for sym in SYMBOLS)
    notice=f'<p class="ok">{html.escape(msg)}</p>' if msg else ''
    return f'''<!doctype html><html lang="bn"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sniper Intelligence Setup</title>{PAGE_STYLE}</head><body>
    <h1>🎯 Sniper Intelligence — Local Dashboard</h1><p class="muted">Exness manual trading • Twelve Data candles • Telegram alerts • Account connection NOT required</p>{notice}
    <div class="box"><div class="split"><div><h2>System status</h2><div class="row"><span>Telegram token</span><strong>{'Saved' if c.get('telegram_token') else 'NOT SAVED'}</strong></div><div class="row"><span>Market data key</span><strong>{'Saved' if c.get('api_key') else 'NOT SAVED'}</strong></div><div class="row"><span>Paired with Telegram</span><strong>{'Yes' if c.get('owner_chat_id') else 'Not yet'}</strong></div><div class="row"><span>Telegram connection</span><strong>{html.escape(STATUS['telegram_poll'])}</strong></div><div class="row"><span>Telegram last OK</span><strong>{html.escape(STATUS['telegram_last_ok'])}</strong></div><div class="row"><span>Connection warning</span><strong>{html.escape(STATUS['telegram_error'] or 'none')}</strong></div><div class="row"><span>Last scan UTC</span><strong>{html.escape(STATUS['last_scan_utc'])}</strong></div><div class="row"><span>Signals sent</span><strong>{STATUS['alerts_sent']}</strong></div><div class="row"><span>Scanner</span><strong>{html.escape(STATUS['scan_result'][:180])}</strong></div></div><div><h2>Pair your Telegram</h2><p>1. Open your bot in Telegram. 2. Copy the full command in ONE message: <code>/start {html.escape(str(c['pairing_pin'])) if not c['owner_chat_id'] else '(already paired)'}</code>. 3. Use <code>/status</code>.</p><p>⚠️ A PIN alone or <code>start 123456</code> without the slash WILL NOT PAIR.</p></div></div>
    <p class="warn">⚠️ Closing this black terminal window stops automatic signals. Keep PC running and internet connected. Secrets are stored locally; do not share your bot token.</p></div>
    <form method="post" action="/save"><div class="box"><h2>1. Connect data & Telegram</h2>{field('api_key','Twelve Data API key (leave blank to keep existing)','password','')}{field('telegram_token','Telegram BotFather token (leave blank to keep existing)','password','')}
    <p class="muted">Create a free API key at <a href="https://twelvedata.com" target="_blank">twelvedata.com</a>. Create bot with <a href="https://t.me/BotFather" target="_blank">@BotFather</a> (send /newbot). No Exness password needed.</p></div>
    <div class="box"><h2>2. Risk & compounding</h2><div class="split"><div>{field('capital_usd','Current equity in USD (USC balance ÷ 100)','number',c['capital_usd'],1)}</div><div><label>Exness account type</label><select name="account">{account_opt}</select></div><div>{field('risk_pct','Risk per trade (%) • 0.25–1','number',c['risk_pct'],0.25,1)}</div></div><p class="muted">Default: 0.5% per trade. Update equity after every closed trade using /capital or /pnl (net USD profit/loss). Daily/weekly baseline stays in effect during the period.</p></div>
    <details><summary>Advanced: estimated spread, slippage and commission (open)</summary>{cost_inputs}</details>
    <button type="submit">SAVE SETTINGS</button></form>
    <div class="box"><h2>Telegram commands</h2><p><code>/help</code> • <code>/status</code> • <code>/capital 100</code> • <code>/pnl -2.50</code> • <code>/account raw</code> • <code>/risk 0.5</code> • <code>/plan XAUUSD 4060 4055</code></p><p class="warn">Signal logic is experimental / unbacktested. External quotes are not guaranteed equal to Exness execution prices. NOT financial advice. Demo test before trading.</p></div>
    <p class="muted"><a href="/">Refresh connection status</a> • Local only: http://127.0.0.1:8765 • Press Ctrl+C in terminal to stop.</p></body></html>'''


class LocalServer(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):pass
    def page(self, body, status=200):
        raw=body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path.split('?')[0]=='/':self.page(admin_page())
        else:self.page('Not found',404)
    def do_POST(self):
        if self.path!='/save':return self.page('Not found',404)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<12000:raise ValueError('Settings too large')
            f=urllib.parse.parse_qs(self.rfile.read(size).decode('utf-8'),keep_blank_values=True)
            def value(key):return f.get(key,[''])[0].strip()
            equity=float(value('capital_usd'))
            risk=float(value('risk_pct'))
            acc=value('account')
            if not math.isfinite(equity) or not 1<=equity<=1e8:raise ValueError('Equity must be between $1 and $100,000,000')
            if not math.isfinite(risk) or not 0.25<=risk<=1:raise ValueError('Risk must be between 0.25% and 1%')
            if acc not in ('cent','raw'):raise ValueError('Choose valid account')
            updated_costs={}
            for sym in SYMBOLS:
                updated_costs[sym]={}
                for key in ('spread','slippage','raw_commission'):
                    val=float(value(f'{sym}_{key}'))
                    if not math.isfinite(val) or val<0 or val>1e6:raise ValueError('Invalid trade costs')
                    updated_costs[sym][key]=val
            with LOCK:
                newtoken=value('telegram_token')
                if newtoken and newtoken!=CFG.get('telegram_token'):
                    if ':' not in newtoken or len(newtoken)>200: raise ValueError('Invalid-looking Telegram bot token')
                    CFG['telegram_token']=newtoken
                    CFG['owner_chat_id']=None
                    CFG['pairing_pin']=str(secrets.randbelow(900000)+100000)
                if value('api_key'):
                    if len(value('api_key'))>150:raise ValueError('API key too long')
                    CFG['api_key']=value('api_key')
                CFG['capital_usd']=equity;CFG['risk_pct']=risk;CFG['account']=acc
                if not CFG.get('baseline_initialized'):
                    now, day, week = now_calendar()
                    CFG.update(initial_equity_usd=equity, daily_start_usd=equity, weekly_start_usd=equity, daily_date=day, week_key=week, baseline_initialized=True)
                CFG['costs']=updated_costs
                save_config()
            self.page(admin_page('✅ Saved successfully. Pair Telegram if needed.'))
        except Exception as exc:self.page(admin_page('❌ '+str(exc)[:150]),400)


def main():
    print('='*66+'\nSNIPER INTELLIGENCE v1 — MANUAL TRADING ALERTS, NOT AUTO-TRADES\n'+'='*66)
    print('Local setup: http://127.0.0.1:8765')
    print('Telegram: send /start <PIN> as ONE message to your bot after setup')
    server=ThreadingHTTPServer(('127.0.0.1',8765),LocalServer)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    threading.Thread(target=telegram_loop,daemon=True).start()
    threading.Thread(target=scan_loop,daemon=True).start()
    time.sleep(1);webbrowser.open('http://127.0.0.1:8765')
    print('Keep this window OPEN for signals. Ctrl+C to exit.\n')
    try:
        while True:time.sleep(10)
    except KeyboardInterrupt:
        print('Stopping...')
        server.shutdown()



def diagnose():
    """Beginner-friendly checks. Never print tokens or API keys."""
    print('='*62)
    print('SNIPERFX — CONNECTION CHECK (No passwords shown)')
    print('='*62)
    with LOCK:
        bot_token=bool(CFG.get('telegram_token'))
        data_key=bool(CFG.get('api_key'))
        paired=bool(CFG.get('owner_chat_id'))
    print('Telegram token:', 'SAVED' if bot_token else 'NOT SAVED')
    print('Twelve Data API key:', 'SAVED' if data_key else 'NOT SAVED')
    print('Telegram pairing:', 'DONE' if paired else 'NOT DONE')
    try:
        with socket.create_connection(('127.0.0.1', 8765),timeout=1):
            print('Local dashboard port:', 'OPEN (bot may be running)')
    except OSError:
        print('Local dashboard port:', 'CLOSED (start START_BOT_WINDOWS.bat first)')
    if bot_token:
        try:
            me=telegram_call('getMe', timeout=12)
            if me.get('ok'):
                username=str(me.get('result',{}).get('username','')).replace('\n','')
                print('Telegram API:', 'CONNECTED, @'+username if username else 'CONNECTED')
            else:
                print('Telegram API:', 'REJECTED (recheck token)')
            info=telegram_call('getWebhookInfo', timeout=12)
            wh=info.get('result',{}).get('url') if info.get('ok') else None
            print('Telegram webhook:', 'ACTIVE (may block polling; remove previous webhook)' if wh else 'No active webhook')
        except urllib.error.HTTPError as exc:
            print('Telegram API:', 'HTTP '+str(exc.code)+' (401 = wrong token; 429 = rate limited)')
        except Exception as exc:
            print('Telegram API:', 'ERROR ('+type(exc).__name__+') — check internet/firewall')
    print('\nIf the bot does not reply: keep START_BOT_WINDOWS.bat open,')
    print('open http://127.0.0.1:8765 and send /start PIN in ONE message.')
    print('Please share a screenshot of this report if you still need help.')


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--diagnose':
        diagnose()
    else:
        main()
