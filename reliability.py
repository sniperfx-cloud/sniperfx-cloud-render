"""Crash-resilient *best-effort* Telegram signal outbox.

No network or dependency is perfectly reliable. An uncertain Telegram response can
create a duplicate on retry, so every alert carries a stable ID. Never deliver a
stale actionable trade; deliver an informational expiry warning instead.
"""
import datetime as dt
import json
import os
import threading
from pathlib import Path

UTC = dt.timezone.utc


class Outbox:
    def __init__(self, filename, send_fn, clock=None):
        self.path = Path(filename)
        self.send_fn = send_fn
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.lock = threading.RLock()
        self.items = {}
        self.last_error = ''
        self.success_count = 0
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text('utf-8'))
                if not isinstance(raw, dict) or not isinstance(raw.get('items'), dict):
                    raise ValueError('bad schema')
                self.items = raw['items']
            except (OSError, ValueError, TypeError) as ex:
                raise RuntimeError('OUTBOX ERROR: Cannot read persisted alerts. Refusing startup.') from ex

    def now(self):
        return self.clock().astimezone(UTC)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        with open(temp, 'w', encoding='utf-8') as fh:
            json.dump({'version': 1, 'items': self.items}, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, self.path)
        try: os.chmod(self.path, 0o600)
        except OSError: pass

    def enqueue(self, signal_id, symbol, candle_open, text):
        with self.lock:
            if signal_id in self.items:
                return True
            opened = dt.datetime.fromisoformat(candle_open)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            # Stale signals should never become live entries after a disconnect.
            deadline = opened + dt.timedelta(minutes=25)  # 15M candle + 10M max delivery delay
            created = self.now()
            if created >= deadline:
                return False
            self.items[signal_id] = dict(
                signal_id=signal_id, symbol=symbol, created=created.isoformat(),
                deadline=deadline.isoformat(), status='pending', attempts=0,
                next_try=0, text=text + f'\nSignal ID: {signal_id}\n⚠️ একই ID আবার এলে একই trade; দ্বিতীয়বার entry নয়।',
                last_error='')
            self._save()  # persist first, network later
            return True

    def pending(self):
        with self.lock:
            return sum(row['status'] in ('pending', 'expired_notice') for row in self.items.values())

    def totals(self):
        with self.lock:
            result = {'pending': 0, 'delivered': 0, 'expired': 0, 'failed': 0, 'cancelled': 0}
            for row in self.items.values():
                status = row['status']
                if status in ('pending', 'expired_notice'):
                    result['pending'] += 1
                elif status == 'delivered': result['delivered'] += 1
                elif status == 'expired': result['expired'] += 1
                elif status == 'failed': result['failed'] += 1
                elif status == 'cancelled': result['cancelled'] += 1
            return result

    def process_one(self):
        """Process one pending signal; call from one dedicated sender thread only."""
        with self.lock:
            now = self.now()
            pending = sorted((v for v in self.items.values() if v['status'] in ('pending','expired_notice')),
                             key=lambda v: v['created'])
            ready = [r for r in pending if float(r['next_try']) <= now.timestamp()]
            if not ready:
                return None
            row = ready[0]
            if row['status'] == 'pending' and now >= dt.datetime.fromisoformat(row['deadline']):
                row['status'] = 'expired_notice'
                row['text'] = (f'⚠️ EXPIRED SNIPER SIGNAL — DO NOT ENTER\n'
                               f'{row["symbol"]} | Signal ID: {row["signal_id"]}\n'
                               'Telegram/network outage delayed this signal beyond its entry window. '
                               'For study only; do not enter using old price/SL/lot.')
                self._save()
            text = row['text']
            ident = row['signal_id']
        # Do NOT keep lock while doing network I/O.
        ok, reason, retry_after = self.send_fn(text)
        with self.lock:
            row = self.items[ident]
            row['attempts'] += 1
            if ok:
                row['status'] = 'expired' if row['status'] == 'expired_notice' else 'delivered'
                row['delivered'] = self.now().isoformat()
                row['last_error'] = ''
                self.success_count += 1
                self.last_error = ''
            else:
                row['last_error'] = str(reason)[:160]
                self.last_error = row['last_error']
                if row['attempts'] >= 50:
                    row['status']='failed'
                else:
                    delay = min(120, 5*2**min(row['attempts']-1, 5))
                    row['next_try'] = self.now().timestamp() + max(float(retry_after or 0), delay)
            self._save()
        return ident, row['status']

    def cancel_pending(self, reason='alerts paused'):
        """Do not deliver a candidate after the user paused or a risk lock triggered."""
        with self.lock:
            changed=False
            for row in self.items.values():
                if row['status'] in ('pending','expired_notice'):
                    row['status']='cancelled'
                    row['last_error']=str(reason)[:100]
                    changed=True
            if changed: self._save()
            return changed

    def prune(self):
        with self.lock:
            cutoff = self.now() - dt.timedelta(days=20)
            ids = [k for k,v in self.items.items() if v['status'] in ('delivered','expired','failed','cancelled')
                   and dt.datetime.fromisoformat(v.get('delivered',v['created'])) < cutoff]
            if ids:
                for key in ids: self.items.pop(key, None)
                self._save()
