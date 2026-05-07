#!/usr/bin/env python3
"""
AML Radar — main entry point.

Usage:
  python3 main.py          # full run: scrape → diff → email
  python3 main.py --dry    # scrape + diff, print report HTML, no email
  python3 main.py --report # re-send last 25h deltas without scraping
  python3 main.py --status # print DB stats and exit
"""

import sys
import os
import argparse
import datetime

from db         import (init_db, upsert_entries, log_run, get_recent_deltas,
                        get_entry_count, update_enrichment, get_enrichment)
from scrapers   import run_all, SCRAPERS
from report     import build_html, build_subject, send_email
import enrichment as _enrichment

MAX_ENRICH_PER_RUN = 50
_LOCK_PATH = '/tmp/amlradar.lock'


def _acquire_lock() -> bool:
    """Write PID lock. Returns False (exit silently) if another live run holds the lock."""
    if os.path.exists(_LOCK_PATH):
        try:
            pid = int(open(_LOCK_PATH).read().strip())
            os.kill(pid, 0)   # signal 0 = check existence only
            print(f'[lock] run already active (pid {pid}) — exiting silently.', flush=True)
            return False
        except ProcessLookupError:
            print(f'[lock] stale lock (pid {pid}) — removing and continuing.', flush=True)
        except (ValueError, OSError):
            pass
        os.remove(_LOCK_PATH)
    with open(_LOCK_PATH, 'w') as f:
        f.write(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        os.remove(_LOCK_PATH)
    except OSError:
        pass


def _flag_eba_batch_spikes(deltas: list[dict], threshold: int = 15) -> None:
    """Mark EBA-added deltas from the same country as batch if count >= threshold.
    Adds batch_flag=True to each affected delta in-place."""
    from collections import Counter
    eba_added = [d for d in deltas if d['registry'] == 'EBA' and d['change_type'] == 'added']
    counts = Counter(d.get('country', '') for d in eba_added)
    spike_countries = {c for c, n in counts.items() if n >= threshold and c}
    for c in spike_countries:
        print(f'[EBA] ⚠️  Batch spike: {counts[c]} entities for {c} — flagging as batch update.',
              flush=True)
    for d in eba_added:
        if d.get('country', '') in spike_countries:
            d['batch_flag'] = True


def cmd_run(dry: bool = False):
    if not _acquire_lock():
        return
    try:
        _cmd_run_inner(dry)
    finally:
        _release_lock()


def _cmd_run_inner(dry: bool = False):
    init_db()
    print(f'[{datetime.datetime.utcnow().isoformat()}] AML Radar starting', flush=True)

    all_entries, scrape_errors = run_all()

    all_deltas: list[dict] = []
    entries_by_reg: dict[str, list] = {}
    # Build lookup for extra fields not stored in DB (entity_type, score)
    entry_extras: dict[tuple, dict] = {
        (e['registry'], e['entry_id']): {k: e[k] for k in ('entity_type', 'score') if k in e}
        for e in all_entries
    }
    for entry in all_entries:
        entries_by_reg.setdefault(entry['registry'], []).append(entry)

    for registry, entries in entries_by_reg.items():
        deltas = upsert_entries(registry, entries)
        all_deltas.extend(deltas)
        if deltas:
            print(f'[{registry}] {len(deltas)} delta(s)', flush=True)

    # Merge extra fields into deltas so report can render badges
    for d in all_deltas:
        extras = entry_extras.get((d['registry'], d['entry_id']), {})
        if extras:
            d.update(extras)

    # Flag EBA batch spikes before enrichment and reporting
    _flag_eba_batch_spikes(all_deltas)

    log_run(
        entries_total=len(all_entries),
        deltas_found=len(all_deltas),
        errors=', '.join(scrape_errors),
    )
    print(f'Total: {len(all_entries)} entries, {len(all_deltas)} deltas, '
          f'{len(scrape_errors)} errors', flush=True)

    # Enrich new entities (cap at MAX_ENRICH_PER_RUN, skip already-cached)
    new_deltas = [d for d in all_deltas if d['change_type'] == 'added']
    to_enrich  = []
    for d in new_deltas:
        cached = get_enrichment(d['registry'], d['entry_id'])
        if cached:
            d.update(cached)
        else:
            to_enrich.append(d)
        if len(to_enrich) >= MAX_ENRICH_PER_RUN:
            break

    if to_enrich:
        print(f'Enriching {len(to_enrich)} new entities…', flush=True)
        enriched = _enrichment.enrich_batch(to_enrich, max_items=MAX_ENRICH_PER_RUN)
        enrich_map = {(e['registry'], e['entry_id']): e for e in enriched}
        for d in all_deltas:
            key = (d['registry'], d['entry_id'])
            if key in enrich_map:
                e = enrich_map[key]
                d.update({k: e.get(k, '') for k in ('website', 'email', 'phone')})
                # Always persist enriched_at to avoid re-scraping even when empty
                update_enrichment(d['registry'], d['entry_id'], e)

    subject = build_subject(all_deltas)
    html    = build_html(all_deltas, get_entry_count(), scrape_errors)

    if dry:
        out_path = '/tmp/amlradar_report.html'
        with open(out_path, 'w') as f:
            f.write(html)
        print(f'Dry run — report written to {out_path}', flush=True)
        print(f'Subject would be: {subject}', flush=True)
    else:
        send_email(html, subject=subject)


def cmd_report():
    """Re-send yesterday's deltas without re-scraping."""
    init_db()
    deltas  = get_recent_deltas(hours=25)
    subject = build_subject(deltas)
    html    = build_html(deltas, get_entry_count(), [])
    send_email(html, subject=subject)


def cmd_status():
    init_db()
    n = get_entry_count()
    print(f'DB entries: {n}')
    deltas = get_recent_deltas(hours=25)
    print(f'Deltas (last 25h): {len(deltas)}')
    for d in deltas[:20]:
        print(f"  [{d['registry']}] {d['change_type']:8s}  {d['entry_name']}")
    if len(deltas) > 20:
        print(f'  ... and {len(deltas)-20} more')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='AML Radar regulatory monitor')
    parser.add_argument('--dry',    action='store_true', help='Scrape + diff, write HTML, no email')
    parser.add_argument('--report', action='store_true', help='Re-send last 25h deltas')
    parser.add_argument('--status', action='store_true', help='Print DB stats')
    args = parser.parse_args()

    if args.status:
        cmd_status()
    elif args.report:
        cmd_report()
    else:
        cmd_run(dry=args.dry)
