"""
AML Radar — HTML email report builder.
"""

import smtplib
import datetime
from collections import Counter
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import (EMAIL_FROM, EMAIL_TO, EMAIL_SUBJECT, GMAIL_APP_PASSWORD,
                    REGISTRY_URLS, PRIORITY_COUNTRIES, COUNTRY_NAMES)


CHANGE_COLORS = {
    'added':   ('#e6f4ea', '#1e7e34', '+ ADDED'),
    'removed': ('#fce8e6', '#c62828', '− REMOVED'),
    'changed': ('#fff8e1', '#f57f17', '~ CHANGED'),
}


def _flag(code: str) -> str:
    try:
        return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code.upper()[:2])
    except Exception:
        return ''


def _country_label(code: str) -> str:
    name = COUNTRY_NAMES.get(code, code)
    return f'{_flag(code)} {name}' if code else 'Unknown'


def _delta_rows(deltas: list[dict]) -> str:
    """Render a flat list of deltas as HTML table rows (no country header)."""
    by_registry: dict[str, list] = {}
    for d in deltas:
        by_registry.setdefault(d['registry'], []).append(d)

    rows = []
    for registry, items in sorted(by_registry.items()):
        reg_url = REGISTRY_URLS.get(registry, '#')
        # UNKNOWN-type entities sort to bottom within each registry block
        items = sorted(items, key=lambda d: d.get('entity_type') == 'UNKNOWN')
        batch_count = sum(1 for d in items if d.get('batch_flag'))
        rows.append(f'''
        <tr>
          <td colspan="6" style="padding:10px 0 3px;font-weight:700;font-size:12px;
              letter-spacing:.07em;color:#555;border-top:1px solid #e8e8e8;">
            <a href="{reg_url}" style="color:#1a237e;text-decoration:none;">{registry}</a>
            <span style="color:#bbb;font-weight:400;font-size:11px;margin-left:6px;">
              {len(items)} change{"s" if len(items) != 1 else ""}
            </span>
          </td>
        </tr>''')
        if batch_count:
            rows.append(f'''
        <tr>
          <td colspan="6" style="padding:4px 8px 4px;background:#fff8e1;
              border-left:3px solid #f0ad4e;font-size:11px;color:#856404;">
            ⚠️ Batch update detected ({batch_count} entries) — likely a registry bulk upload,
            not {batch_count} new individual leads.
          </td>
        </tr>''')
        for d in items:
            bg, fg, label = CHANGE_COLORS.get(d['change_type'], ('#f5f5f5', '#333', '?'))
            name   = d.get('entry_name') or d.get('new_name') or d.get('old_name') or '(unknown)'
            unclassified = (
                ' <span style="background:#e0e0e0;color:#757575;font-size:10px;font-weight:600;'
                'padding:1px 6px;border-radius:8px;margin-left:4px;">Unclassified</span>'
                if d.get('entity_type') == 'UNKNOWN' else ''
            )
            detail = ''
            if d['change_type'] == 'changed':
                parts = []
                if d.get('old_status') != d.get('new_status'):
                    parts.append(f"status: <s style='color:#999'>{d['old_status']}</s>"
                                 f" → <b>{d['new_status']}</b>")
                if d.get('old_name') != d.get('new_name') and d.get('old_name'):
                    parts.append(f"name: <s style='color:#999'>{d['old_name']}</s>"
                                 f" → <b>{d['new_name']}</b>")
                detail = ' · '.join(parts)
            elif d['change_type'] == 'added':
                detail = f"status: <b>{d.get('new_status', '—')}</b>"
            elif d['change_type'] == 'removed':
                detail = f"was: {d.get('old_status', '—')}"

            # enrichment columns
            website = d.get('website', '')
            email   = d.get('email', '')
            phone   = d.get('phone', '')
            w_cell  = (f'<a href="{website}" style="color:#1a237e;font-size:11px;">'
                       f'{website[:30]}{"…" if len(website)>30 else ""}</a>') if website else \
                      '<span style="color:#ccc;">—</span>'
            e_cell  = (f'<a href="mailto:{email}" style="color:#1a237e;font-size:11px;">'
                       f'{email}</a>') if email else '<span style="color:#ccc;">—</span>'
            p_cell  = (f'<a href="tel:{phone}" style="color:#555;font-size:11px;">'
                       f'{phone}</a>') if phone else '<span style="color:#ccc;">—</span>'

            rows.append(f'''
            <tr style="background:{bg}">
              <td style="padding:6px 8px;font-size:11px;color:{fg};font-weight:700;
                  white-space:nowrap;">{label}</td>
              <td style="padding:6px 8px;font-size:13px;color:#111;font-weight:500;">{name}{unclassified}</td>
              <td style="padding:6px 8px;font-size:11px;color:#555;">{detail}</td>
              <td style="padding:6px 8px;">{w_cell}</td>
              <td style="padding:6px 8px;">{e_cell}</td>
              <td style="padding:6px 8px;">{p_cell}</td>
            </tr>''')
    return ''.join(rows)


def build_html(deltas: list[dict], entries_total: int, errors: list[str]) -> str:
    date_str = datetime.datetime.utcnow().strftime('%A %d %B %Y, %H:%M UTC')

    if not deltas:
        delta_block = ('<p style="color:#555;font-size:15px;">'
                       'No changes detected in the last 25 hours.</p>')
    else:
        # Group deltas by country
        by_country: dict[str, list] = {}
        for d in deltas:
            by_country.setdefault(d.get('country') or '', []).append(d)

        priority_set = set(PRIORITY_COUNTRIES)
        ordered_countries = (
            [c for c in PRIORITY_COUNTRIES if c in by_country] +
            sorted(c for c in by_country if c not in priority_set)
        )

        sections = []

        # Column header row
        sections.append('''
        <tr>
          <td colspan="6" style="padding:4px 0 8px;">
            <table width="100%" style="font-size:10px;color:#aaa;border-collapse:collapse;">
              <tr>
                <td width="60">TYPE</td><td>NAME</td><td>DETAIL</td>
                <td width="120">WEBSITE</td><td width="140">EMAIL</td><td width="100">PHONE</td>
              </tr>
            </table>
          </td>
        </tr>''')

        # Priority countries
        priority_rows = []
        other_rows    = []

        for country in ordered_countries:
            items       = by_country[country]
            label       = _country_label(country)
            count       = len(items)
            rows_html   = _delta_rows(items)
            section_html = f'''
            <tr>
              <td colspan="6" style="padding:16px 0 4px 0;">
                <div style="font-size:14px;font-weight:700;color:#222;">
                  {label}
                  <span style="background:#e8eaf6;color:#3949ab;font-size:11px;font-weight:600;
                      padding:2px 8px;border-radius:10px;margin-left:8px;">{count}</span>
                </div>
              </td>
            </tr>
            {rows_html}'''
            if country in priority_set:
                priority_rows.append(section_html)
            else:
                other_rows.append(section_html)

        sections.extend(priority_rows)

        if other_rows:
            other_block = f'''
            <tr>
              <td colspan="6" style="padding:16px 0 4px;">
                <details>
                  <summary style="cursor:pointer;font-size:13px;font-weight:600;color:#888;
                      list-style:none;user-select:none;">
                    ▶ Other Countries ({sum(len(by_country[c]) for c in by_country if c not in priority_set)})
                  </summary>
                  <table width="100%" cellpadding="0" cellspacing="0" border="0"
                         style="border-collapse:collapse;margin-top:8px;">
                    {"".join(other_rows)}
                  </table>
                </details>
              </td>
            </tr>'''
            sections.append(other_block)

        delta_block = f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0"
               style="border-collapse:collapse;">
          {"".join(sections)}
        </table>'''

    error_block = ''
    if errors:
        error_items = ''.join(
            f'<li style="margin:3px 0;font-size:12px;">{e}</li>' for e in errors
        )
        error_block = f'''
        <div style="margin-top:28px;padding:16px 20px;background:#fff3cd;
             border-left:4px solid #f0ad4e;border-radius:0 4px 4px 0;">
          <div style="font-size:13px;color:#856404;font-weight:700;margin-bottom:8px;">
            ⚠️ Sources with errors today
          </div>
          <ul style="margin:0;padding-left:18px;color:#856404;">
            {error_items}
          </ul>
          <div style="margin-top:8px;font-size:11px;color:#a07800;">
            Data from these registries may be incomplete or missing.
          </div>
        </div>'''

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f5f5f5;color:#111;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f5f5f5">
    <tr><td align="center" style="padding:32px 16px;">
      <table width="680" cellpadding="0" cellspacing="0" border="0"
             style="max-width:680px;background:#ffffff;border-radius:8px;
                    box-shadow:0 2px 8px rgba(0,0,0,.08);">

        <tr><td style="padding:28px 32px 20px;border-bottom:1px solid #eeeeee;">
          <div style="display:flex;align-items:center;gap:12px;">
            <span style="font-size:22px;font-weight:800;letter-spacing:-.02em;color:#1a237e;">
              AML Radar
            </span>
            <span style="font-size:13px;color:#888;margin-left:12px;">{date_str}</span>
          </div>
          <div style="margin-top:6px;font-size:13px;color:#666;">
            {entries_total:,} entities monitored across {len(REGISTRY_URLS)} registries
            · <b style="color:{'#c62828' if deltas else '#1e7e34'}">
              {len(deltas)} delta{"s" if len(deltas) != 1 else ""}
            </b> detected
          </div>
        </td></tr>

        <tr><td style="padding:24px 32px;">
          {delta_block}
          {error_block}
        </td></tr>

        <tr><td style="padding:16px 32px 24px;border-top:1px solid #eeeeee;">
          <p style="font-size:11px;color:#aaa;margin:0;">
            AML Radar — automated regulatory monitor · data from public registries only ·
            not legal advice
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>'''


_ENTITY_TYPE_SHORT = {
    'PSD_PI':   'PI',
    'PSD_EMI':  'EMI',
    'PSD_AISP': 'AISP',
    'PSD_ENL':  'ENL',
}


def build_subject(deltas: list[dict]) -> str:
    date_str = datetime.date.today().strftime('%-d %b')
    added    = [d for d in deltas if d['change_type'] == 'added']
    n_new    = len(added)

    # Collect distinct known short-form entity types from new entries
    seen_types: list[str] = []
    seen_set: set = set()
    for d in added:
        short = _ENTITY_TYPE_SHORT.get(d.get('entity_type', ''))
        if short and short not in seen_set:
            seen_set.add(short)
            seen_types.append(short)
    type_label = '/'.join(seen_types) if seen_types else ''

    kind = type_label or f"entit{'ies' if n_new != 1 else 'y'}"
    subject = f"AML Radar · {date_str} · {n_new} new {kind}"

    if n_new:
        counts = Counter(
            d.get('country', '?') for d in added if d.get('country')
        )
        top = ', '.join(f'{c}+{n}' for c, n in counts.most_common(4))
        if top:
            subject += f' ({top})'
    return subject


def send_email(html: str, subject: str = EMAIL_SUBJECT):
    if not GMAIL_APP_PASSWORD:
        print('GMAIL_APP_PASSWORD not set — skipping email send', flush=True)
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = ', '.join(EMAIL_TO)
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
        smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f'Email sent to {EMAIL_TO}', flush=True)
    return True
