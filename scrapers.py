"""
AML Radar — Registry scrapers.
Each scraper returns list[dict] with keys:
  entry_id (str), name (str), status (str), url (str)
Returns [] on failure (logged to stderr).
"""

import csv
import io
import sys
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from config import REQUEST_TIMEOUT, REQUEST_HEADERS, REGISTRY_URLS


def _get(url, *, params=None, json_mode=False, extra_headers=None, retries=2, backoff=4):
    headers = {**REQUEST_HEADERS, **(extra_headers or {})}
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json() if json_mode else r
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_exc


def _slug(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()[:16]


def _err(registry: str, exc: Exception) -> list:
    print(f"[{registry}] scrape error: {exc}", file=sys.stderr)
    return []


def _csv_rows(text: str) -> list[dict]:
    """Parse CSV text, trying comma then semicolon delimiter."""
    for delim in (',', ';', '\t'):
        try:
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            rows = list(reader)
            if rows and len(rows[0]) > 1:
                return rows
        except Exception:
            continue
    return []


def _html_table(soup, selectors=('table tbody tr',)) -> list:
    for sel in selectors:
        rows = soup.select(sel)
        if rows:
            return rows
    return []


# ── EBA ───────────────────────────────────────────────────────────────────────

# EBA entity-type codes (PSD2 register)
_EBA_KEEP_TYPES = frozenset({'PSD_PI', 'PSD_EMI', 'PSD_AISP', 'PSD_ENL'})
_EBA_DROP_TYPES = frozenset({'PSD_AG', 'PSD_EXC', 'PSD_EPI', 'PSD_EEMI', 'PSD_BR'})
# Retail/noise keywords for name-based fallback when EntityType is unknown
_EBA_NOISE_KW = (
    'AGENZIA VIAGGI', 'TOUR OPERATOR', 'TABACCHI', 'TABACCHERIA', 'RIVENDITA',
    'EDICOLA', 'ALIMENTARI', 'MINIMARKET', 'SUPERMERCATO', 'AUTOSCUOLA',
    'SCUOLA NAUTICA', 'SCUOLA GUIDA', 'CARTOLERIA', 'FERRAMENTA', 'CARTOLIBRERIA',
    'PIZZERIA', 'RISTORANTE', 'TRATTORIA', 'OSTERIA', ' BAR ', 'CAFFE', 'CAFFÈ',
    'GELATERIA', 'PASTICCERIA', 'MACELLERIA', 'SALUMERIA', 'PANIFICIO', 'FORNO',
    'LAVANDERIA', 'TINTORIA', 'PARRUCCHIERE', 'ESTETICA', 'GIOIELLERIA',
    'OTTICA', 'FARMACIA', 'PARAFARMACIA', 'COMPRO ORO', 'BOUTIQUE', 'NEGOZIO DI',
    'HOTEL', 'ALBERGO', 'B&B', 'BED AND BREAKFAST',
)


def _eba_keep(entity_type: str, name: str) -> bool:
    if entity_type in _EBA_KEEP_TYPES:
        return True
    if entity_type in _EBA_DROP_TYPES:
        return False
    # Unknown type: keep and flag as UNKNOWN rather than risk dropping a real fintech
    return True


def scrape_EBA() -> list[dict]:
    """EBA EUCLID — Payment/EMI institutions (PSDMD bulk JSON via public filemetadata API).
    Filters: keeps PI/EMI/AISP/ENL; drops 315K agents, branches, exempted entities."""
    import io as _io, zipfile, json as _json
    try:
        meta = _get('https://euclid.eba.europa.eu/register/api/filemetadata',
                    extra_headers={'Referer': 'https://euclid.eba.europa.eu/'}).json()
        zip_url = meta['golden_copy_path_context'] + meta['latest_version_relative_zip_path']
        r = _get(zip_url, extra_headers={'Referer': 'https://euclid.eba.europa.eu/'})
        zf = zipfile.ZipFile(_io.BytesIO(r.content))
        # data = [disclaimer_wrapper_list, entity_rows_list]
        data = _json.loads(zf.read(zf.namelist()[0]).decode('utf-8'))
        rows = data[1]
        out: list[dict] = []
        seen: set = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            eid         = row.get('EntityCode', '').strip()
            entity_type = row.get('EntityType', '').strip()
            if not eid or eid in seen:
                continue
            seen.add(eid)
            name = country = reg_date = ''
            for prop in row.get('Properties', []):
                if not isinstance(prop, dict):
                    continue
                n = prop.get('ENT_NAM')
                c = prop.get('ENT_COU_RES')
                # Registration/authorisation date — try known EBA EUCLID field names
                d = prop.get('ENT_DT_AUT') or prop.get('ENT_DT_REG') or prop.get('ENT_AUTH_DATE')
                if n and not name:
                    name = (n[0] if isinstance(n, list) else n)
                if c and not country:
                    country = (c[0] if isinstance(c, list) else c)
                if d and not reg_date:
                    reg_date = (d[0] if isinstance(d, list) else d)
            if not name:
                continue
            if not _eba_keep(entity_type, name):
                continue
            etype = entity_type or 'UNKNOWN'
            entry = {'entry_id': eid, 'name': name, 'status': 'Authorised',
                     'country': country, 'entity_type': etype,
                     'url': 'https://euclid.eba.europa.eu/register/pir/search'}
            if reg_date:
                entry['reg_date'] = reg_date
            if etype == 'UNKNOWN':
                entry['score'] = 3
            out.append(entry)
        return out if out else _err('EBA', Exception('no entries parsed'))
    except Exception as exc:
        return _err('EBA', exc)


# ── ESMA ──────────────────────────────────────────────────────────────────────

def scrape_ESMA() -> list[dict]:
    """ESMA — Designated Publishing Entities (MiFID II transparency, DPE register CSV)."""
    import io as _io
    dpe_url = 'https://www.esma.europa.eu/sites/default/files/2024-09/DPE_Register.csv'
    try:
        r = _get(dpe_url, extra_headers={'Referer': 'https://www.esma.europa.eu/'})
        reader = csv.DictReader(_io.StringIO(r.text))
        out  = []
        seen: set = set()
        for row in reader:
            lei    = row.get('ae_lei', '').strip()
            name   = row.get('ae_lei_name', '').strip()
            status = row.get('ae_status', 'Active').strip() or 'Active'
            if not name or not lei:
                continue
            if lei in seen:
                continue
            seen.add(lei)
            out.append({'entry_id': lei, 'name': name, 'status': status,
                        'url': dpe_url})
        return out if out else _err('ESMA', Exception('no entries in DPE CSV'))
    except Exception as exc:
        return _err('ESMA', exc)


def scrape_ESMA_ELTIF() -> list[dict]:
    """ESMA — European Long-Term Investment Funds register (ELTIF xlsx)."""
    import io as _io
    import openpyxl
    xlsx_url = 'https://www.esma.europa.eu/sites/default/files/library/esma34-46-101_esma_register_eltif_art33.xlsx'
    try:
        r   = _get(xlsx_url, extra_headers={'Referer': 'https://www.esma.europa.eu/'})
        wb  = openpyxl.load_workbook(_io.BytesIO(r.content), read_only=True, data_only=True)
        ws  = wb['ELTIFRG']
        rows_iter = iter(ws.rows)
        header_row = next(rows_iter, None)
        if not header_row:
            return _err('ESMA-ELTIF', Exception('empty sheet'))
        hdrs = [str(c.value or '').strip() for c in header_row]
        out  = []
        for row in rows_iter:
            vals = {hdrs[i]: str(c.value or '').strip() for i, c in enumerate(row) if i < len(hdrs)}
            name = vals.get('Name of the ELTIF', '').strip()
            lei  = vals.get('LEI of the ELTIF (where available)', '').strip()
            code = vals.get('National code of the ELTIF (where available)', '').strip()
            if len(name) > 2:
                eid = lei or code or _slug(name)
                out.append({'entry_id': eid, 'name': name, 'status': 'Authorised',
                            'url': xlsx_url})
        wb.close()
        return out if out else _err('ESMA-ELTIF', Exception('no entries'))
    except Exception as exc:
        return _err('ESMA-ELTIF', exc)


def scrape_ESMA_MiCA() -> list[dict]:
    """ESMA — MiCA Crypto-Asset Service Providers + EMT issuers (interim register CSVs)."""
    import io as _io
    base = 'https://www.esma.europa.eu/sites/default/files/2024-12/'
    # CASPS = authorised crypto-asset service providers, EMTWP = EMT issuers
    files = ['CASPS.csv', 'EMTWP.csv']
    out  = []
    seen: set = set()
    for fname in files:
        url = base + fname
        try:
            r = _get(url, extra_headers={'Referer': 'https://www.esma.europa.eu/'})
            # Strip UTF-8 BOM if present
            text = r.content.decode('utf-8-sig')
            reader = csv.DictReader(_io.StringIO(text))
            for row in reader:
                name = row.get('ae_lei_name', '').strip()
                lei  = row.get('ae_lei', '').strip()
                if not name:
                    continue
                eid = lei or _slug(name)
                if eid in seen:
                    continue
                seen.add(eid)
                out.append({'entry_id': eid, 'name': name,
                            'status': 'Authorised', 'url': url})
        except Exception as exc:
            _err('ESMA-MiCA', exc)
    return out if out else _err('ESMA-MiCA', Exception('no entries'))


# ── FCA ───────────────────────────────────────────────────────────────────────

def scrape_FCA() -> list[dict]:
    """FCA Financial Services Register — Playwright network-intercept approach.
    NOTE: Cloudflare Turnstile v0 blocks all headless browsers; returns [] until resolved."""
    try:
        from playwright.sync_api import sync_playwright
        intercepted: dict = {}

        def _on_response(resp):
            ct = resp.headers.get('content-type', '')
            if 'json' in ct and resp.status == 200:
                try:
                    body = resp.json()
                    if isinstance(body, dict) and any(k in body for k in
                                                      ('Results', 'results', 'data', 'firms')):
                        intercepted[resp.url] = body
                except Exception:
                    pass

        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            pg = br.new_page()
            pg.on('response', _on_response)
            pg.goto('https://register.fca.org.uk/s/firm-search',
                    timeout=30000, wait_until='domcontentloaded')
            pg.wait_for_timeout(5000)
            br.close()

        if not intercepted:
            raise Exception('Cloudflare Turnstile blocks headless — no data intercepted')

        out = []
        for url, body in intercepted.items():
            firms = body.get('Results') or body.get('results') or body.get('data') or []
            for f in firms:
                name   = f.get('Name') or f.get('name', '')
                frn    = str(f.get('FRN') or f.get('frn') or _slug(name))
                status = f.get('Status') or f.get('status', 'Authorised')
                if name:
                    out.append({'entry_id': frn, 'name': name, 'status': status,
                                'url': f'https://register.fca.org.uk/s/firm?id={frn}'})
        return out if out else _err('FCA', Exception('Cloudflare Turnstile blocks headless — no data'))
    except Exception as exc:
        return _err('FCA', exc)


# ── BaFin ─────────────────────────────────────────────────────────────────────

def scrape_BaFin() -> list[dict]:
    """BaFin — Crypto-asset service providers via Playwright (portal.mvp.bafin.de).
    Categories: Kryptowertpapierregisterführer (160), Kryptoverwahrer (170),
    Kryptowerte-Dienstleister (175)."""
    import re as _re
    PORTAL = 'https://portal.mvp.bafin.de/database/InstInfo/'
    CATS   = ['160', '170', '175']
    try:
        from playwright.sync_api import sync_playwright
        out:  list[dict] = []
        seen: set        = set()

        with sync_playwright() as pw:
            br  = pw.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=REQUEST_HEADERS['User-Agent'])
            pg  = ctx.new_page()

            for cat_id in CATS:
                pg.goto(PORTAL + 'institutList.do?cmd=prepareKategoriensuche&typ=I',
                        timeout=30000, wait_until='networkidle')
                pg.select_option('select[name=kategorieId]', cat_id)
                pg.click('input[name=sucheButtonInstitut]')
                pg.wait_for_load_state('networkidle', timeout=20000)

                while True:
                    soup = BeautifulSoup(pg.content(), 'lxml')
                    for row in soup.select('table tbody tr'):
                        cells = row.find_all('td')
                        if not cells:
                            continue
                        name = cells[0].get_text(strip=True)
                        kind = cells[1].get_text(strip=True) if len(cells) > 1 else 'Authorised'
                        link = cells[0].find('a', href=True)
                        # Extract BaFin institutId from detail href
                        href  = link['href'] if link else ''
                        id_m  = _re.search(r'institutId=(\d+)', href)
                        eid   = id_m.group(1) if id_m else _slug(name)
                        detail = (PORTAL + href) if (href and not href.startswith('http')) else href
                        if name and eid not in seen:
                            seen.add(eid)
                            out.append({'entry_id': eid, 'name': name, 'status': kind,
                                        'url': detail or PORTAL})

                    # Follow next-page link if present
                    cur_p_m = _re.search(r'd-4012550-p=(\d+)', pg.url)
                    cur_p   = int(cur_p_m.group(1)) if cur_p_m else 1
                    nxt_a   = pg.query_selector(f'a[href*="d-4012550-p={cur_p + 1}"]')
                    if not nxt_a:
                        break
                    nxt_href = nxt_a.get_attribute('href')
                    pg.goto((PORTAL + nxt_href) if not nxt_href.startswith('http') else nxt_href,
                            timeout=20000, wait_until='networkidle')

            br.close()

        return out if out else _err('BaFin', Exception('no entries'))
    except Exception as exc:
        return _err('BaFin', exc)


# ── OAM ───────────────────────────────────────────────────────────────────────

def scrape_OAM() -> list[dict]:
    """OAM Italy — VASP register via Playwright (direct POST blocked with 403 since May 2026).
    Parses data-* attributes from the jQuery Mobile listview rendered on the public register page."""
    import re as _re
    page_url = 'https://www.organismo-am.it/elenchi-registri/operatori_valute_virtuali/index.html'
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            pg = br.new_page()
            pg.goto(page_url, timeout=30000, wait_until='networkidle')
            pg.wait_for_timeout(3000)
            soup = BeautifulSoup(pg.content(), 'lxml')
            br.close()

        out  = []
        seen: set = set()
        # Each entry is a <li> containing an <a> with data-idsoggetto, data-nome attributes.
        # The registration number is in the <h2> text: "NAME | NUMERO ISCRIZIONE: PSV## | ..."
        psv_pat = _re.compile(r'NUMERO ISCRIZIONE:\s*([A-Z0-9]+)', _re.IGNORECASE)
        status_pat = _re.compile(r'\|\s*([A-ZÀÈÉÌÍÎÒÓÙÚ]+)\s*$')
        for li in soup.find_all('li'):
            a = li.find('a', attrs={'data-idsoggetto': True})
            if not a:
                continue
            name = a.get('data-nome', '').strip()
            uid  = a.get('data-idsoggetto', '').strip()
            if not name or not uid or uid in seen:
                continue
            seen.add(uid)
            h2_text = li.find('h2').get_text(strip=True) if li.find('h2') else ''
            m_num = psv_pat.search(h2_text)
            entry_id = m_num.group(1) if m_num else uid[:16]
            # Status: last word after final "|" in h2 (e.g. "ISCRITTO", "SOSPESO", "CANCELLATO")
            m_stat = status_pat.search(h2_text.upper())
            # Fall back to last pipe-separated token from full li text
            li_text = li.get_text(separator='|', strip=True).upper()
            status_tokens = [t.strip() for t in li_text.split('|') if t.strip()]
            status = 'ISCRITTO'
            for tok in reversed(status_tokens):
                if tok in ('ISCRITTO', 'SOSPESO', 'CANCELLATO', 'REVOCATO'):
                    status = tok
                    break
            out.append({'entry_id': entry_id, 'name': name,
                        'status': status, 'url': page_url})
        return out if out else _err('OAM', Exception('no entries parsed from Playwright render'))
    except Exception as exc:
        return _err('OAM', exc)


# ── FINMA ─────────────────────────────────────────────────────────────────────

def scrape_FINMA() -> list[dict]:
    """FINMA — banks and securities firms xlsx download (verified working)."""
    import io as _io
    import openpyxl
    try:
        # beh.xlsx = List of banks and securities firms authorised by FINMA
        xlsx_url = 'https://www.finma.ch/en/~/media/finma/dokumente/bewilligungstraeger/xlsx/beh.xlsx'
        r   = _get(xlsx_url, extra_headers={'Referer': REGISTRY_URLS['FINMA']})
        wb  = openpyxl.load_workbook(_io.BytesIO(r.content), read_only=True, data_only=True)
        ws  = wb.active
        rows_iter = iter(ws.rows)
        # First row = headers
        header_row = next(rows_iter, None)
        if header_row is None:
            return []
        headers = [str(c.value or '').strip() for c in header_row]
        out = []
        for row in rows_iter:
            vals = [str(c.value or '').strip() for c in row]
            if not any(vals):
                continue
            rd = dict(zip(headers, vals))
            name   = (rd.get('Name') or rd.get('Firma') or rd.get('Raison sociale') or
                      vals[0] if vals else '').strip()
            status = (rd.get('Status') or rd.get('Bewilligungsstatus') or 'Authorised').strip()
            if len(name) > 2:
                out.append({'entry_id': _slug(name), 'name': name,
                            'status': status, 'url': xlsx_url})
        wb.close()
        return out
    except Exception as exc:
        return _err('FINMA', exc)


# ── DFSA ──────────────────────────────────────────────────────────────────────

def scrape_DFSA() -> list[dict]:
    """DFSA — disabled: 403 Cloudflare bot protection on all endpoints."""
    return []


def _scrape_DFSA_disabled() -> list[dict]:
    """DFSA (Dubai DIFC) — public register of authorised firms (paginated HTML)."""
    out  = []
    page = 1
    while page <= 20:
        try:
            url = REGISTRY_URLS['DFSA']
            r   = _get(url, params={'page': page} if page > 1 else None,
                       extra_headers={'Referer': 'https://www.dfsa.ae/'})
            soup = BeautifulSoup(r.text, 'lxml')
            rows = soup.select('table tbody tr, .register-item, .firms-list tr')
            if not rows:
                break
            found = 0
            for row in rows:
                cells = row.find_all('td')
                if not cells:
                    continue
                name   = cells[0].get_text(strip=True)
                status = cells[1].get_text(strip=True) if len(cells) > 1 else 'Authorised'
                link   = row.find('a', href=True)
                url_e  = link['href'] if link else REGISTRY_URLS['DFSA']
                if url_e and not url_e.startswith('http'):
                    url_e = 'https://www.dfsa.ae' + url_e
                if name:
                    out.append({'entry_id': _slug(name), 'name': name,
                                'status': status, 'url': url_e})
                    found += 1
            if found == 0:
                break
            # Check for next page link
            next_link = soup.find('a', string=lambda s: s and ('Next' in s or '›' in s))
            if not next_link:
                break
            page += 1
        except Exception as exc:
            if not out:
                return _err('DFSA', exc)
            break
    return out


# ── FSRA ──────────────────────────────────────────────────────────────────────

def scrape_FSRA() -> list[dict]:
    """FSRA (Abu Dhabi ADGM) — public register at adgm.com."""
    try:
        r    = _get(REGISTRY_URLS['FSRA'],
                    extra_headers={'Referer': 'https://www.adgm.com/',
                                   'Accept': 'text/html,*/*'})
        soup = BeautifulSoup(r.text, 'lxml')
        rows = _html_table(soup, ('table tbody tr', '.register-list tr',
                                  '.public-register tr', '.entity-list li'))
        out  = []
        for row in rows:
            cells = row.find_all('td')
            if cells:
                name   = cells[0].get_text(strip=True)
                status = cells[1].get_text(strip=True) if len(cells) > 1 else 'Authorised'
                link   = row.find('a', href=True)
                url_e  = link['href'] if link else REGISTRY_URLS['FSRA']
            else:
                link = row.find('a', href=True)
                name = link.get_text(strip=True) if link else row.get_text(strip=True)[:120]
                status = 'Authorised'
                url_e  = link['href'] if link else REGISTRY_URLS['FSRA']
            if url_e and not url_e.startswith('http'):
                url_e = 'https://www.adgm.com' + url_e
            if len(name) > 2:
                out.append({'entry_id': _slug(name), 'name': name,
                            'status': status, 'url': url_e})
        return out
    except Exception as exc:
        return _err('FSRA', exc)


# ── VARA ──────────────────────────────────────────────────────────────────────

def scrape_VARA() -> list[dict]:
    """VARA (Dubai virtual assets) — licensed VASPs via Gatsby static JSON."""
    try:
        # Fetch current Gatsby static query hashes from page-data
        page_data = _get(
            'https://www.vara.ae/page-data/en/licenses-and-register/public-register/page-data.json',
            extra_headers={'Referer': 'https://www.vara.ae/'}
        ).json()
        hashes = page_data.get('staticQueryHashes', ['1078231335'])
        items  = []
        for h in hashes:
            try:
                sq = _get(f'https://www.vara.ae/page-data/sq/d/{h}.json',
                          extra_headers={'Referer': 'https://www.vara.ae/'}).json()
                registry = (sq.get('data', {}).get('umbraco', {})
                              .get('allVaraRegistryAr', {}).get('items'))
                if registry:
                    items = registry
                    break
            except Exception:
                continue
        if not items:
            return _err('VARA', Exception('no registry items found in static queries'))
        out   = []
        for item in items:
            name   = (item.get('name') or item.get('title') or '').strip()
            status = (item.get('vARAStatus') or item.get('licenseType') or 'Licensed').strip()
            ref    = item.get('vASPReference') or item.get('varaReferenceNumber') or _slug(name)
            url_e  = item.get('url') or REGISTRY_URLS['VARA']
            if url_e and not url_e.startswith('http'):
                url_e = 'https://www.vara.ae' + url_e
            if name:
                out.append({'entry_id': str(ref), 'name': name,
                            'status': status, 'url': url_e})
        return out
    except Exception as exc:
        return _err('VARA', exc)


# ── CBUAE ─────────────────────────────────────────────────────────────────────

def scrape_CBUAE() -> list[dict]:
    """CBUAE — disabled: 403 Cloudflare bot protection."""
    return []


def _scrape_CBUAE_disabled() -> list[dict]:
    """CBUAE — licensed exchange houses, banks, payment providers (with retry)."""
    try:
        r    = _get(REGISTRY_URLS['CBUAE'],
                    retries=3, backoff=5,
                    extra_headers={'Referer': 'https://www.centralbank.ae/'})
        soup = BeautifulSoup(r.text, 'lxml')
        rows = _html_table(soup, ('table tbody tr', '.licensed-entities tr'))
        out  = []
        for row in rows:
            cells = row.find_all('td')
            if not cells:
                continue
            name   = cells[0].get_text(strip=True)
            status = cells[1].get_text(strip=True) if len(cells) > 1 else 'Licensed'
            if name:
                out.append({'entry_id': _slug(name), 'name': name,
                            'status': status, 'url': REGISTRY_URLS['CBUAE']})
        return out
    except Exception as exc:
        return _err('CBUAE', exc)


# ── FSC-MU ────────────────────────────────────────────────────────────────────

def scrape_FSC_MU() -> list[dict]:
    """FSC Mauritius — register of licensees (all categories)."""
    try:
        r    = _get(REGISTRY_URLS['FSC-MU'],
                    extra_headers={'Referer': 'https://www.fscmauritius.org/'})
        soup = BeautifulSoup(r.text, 'lxml')
        rows = _html_table(soup, ('table tbody tr', '.licensee-table tr', 'tr'))
        out  = []
        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 2:
                continue
            name   = cells[0].get_text(strip=True)
            status = cells[1].get_text(strip=True) if len(cells) > 1 else 'Licensed'
            lic_no = cells[2].get_text(strip=True) if len(cells) > 2 else ''
            if len(name) > 2:
                out.append({'entry_id': lic_no or _slug(name), 'name': name,
                            'status': status, 'url': REGISTRY_URLS['FSC-MU']})
        return out
    except Exception as exc:
        return _err('FSC-MU', exc)


# ── BOM-MU ────────────────────────────────────────────────────────────────────

def scrape_BOM_MU() -> list[dict]:
    """Bank of Mauritius — disabled: page only serves a forex rate widget in static HTML;
    the actual licensed-banks list requires JS rendering not yet implemented."""
    return []


# ── BDDK ──────────────────────────────────────────────────────────────────────

def scrape_BDDK() -> list[dict]:
    """BDDK Turkey — licensed banks from HTML list (all institution types)."""
    # Type codes: 90=banks, 91=leasing, 92=factoring, 93=finance, 94=saving-finance
    # 95=holding, 96=asset-mgmt, 97=foreign-rep, 98=audit, 99=rating, 100=credit-rating
    type_ids = [90, 93, 94, 96]  # banks, finance cos, saving-finance, asset-mgmt
    out = []
    seen: set = set()
    for tid in type_ids:
        try:
            r    = _get(f'https://www.bddk.org.tr/Kurulus/Liste/{tid}',
                        extra_headers={'Accept-Language': 'tr,en;q=0.5',
                                       'Referer': 'https://www.bddk.org.tr/'})
            soup = BeautifulSoup(r.text, 'lxml')
            for li in soup.select('li.row'):
                baslik = li.find('div', class_='baslikContainer')
                if not baslik:
                    continue
                name = baslik.get_text(strip=True)
                # Strip leading number like "1. " or "12. "
                import re as _re
                name = _re.sub(r'^\d+\.\s*', '', name).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                link = li.find('a', href=True)
                url_e = link['href'] if link else REGISTRY_URLS['BDDK']
                out.append({'entry_id': _slug(name), 'name': name,
                            'status': 'Licensed', 'url': url_e or REGISTRY_URLS['BDDK']})
        except Exception as exc:
            _err('BDDK', exc)
    return out if out else _err('BDDK', Exception('no entries parsed'))


# ── CBI ───────────────────────────────────────────────────────────────────────

def scrape_CBI() -> list[dict]:
    """Central Bank of Ireland — Credit Institutions + EMI registers (PDF via ASP.NET postback)."""
    import io as _io
    import re as _re
    try:
        import pypdf
    except ImportError:
        return _err('CBI', Exception('pypdf not installed'))
    try:
        base_url = 'http://registers.centralbank.ie/downloadspage.aspx'
        headers_cbi = {**REQUEST_HEADERS, 'Accept-Language': 'en-US,en;q=0.5'}
        session = requests.Session()
        r = session.get(base_url, headers=headers_cbi, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(r.text, 'lxml')
        vs  = soup.find('input', {'name': '__VIEWSTATE'})
        ev  = soup.find('input', {'name': '__EVENTVALIDATION'})
        vg  = soup.find('input', {'name': '__VIEWSTATEGENERATOR'})

        def _dl_pdf(ctl: int) -> str:
            pd = {'__VIEWSTATE': vs['value'] if vs else '',
                  '__EVENTTARGET': f'ctl00$cphRegistersMasterPage$ctl{ctl:02d}',
                  '__EVENTARGUMENT': ''}
            if ev: pd['__EVENTVALIDATION'] = ev['value']
            if vg: pd['__VIEWSTATEGENERATOR'] = vg['value']
            r2 = session.post(base_url, data=pd,
                              headers={**headers_cbi,
                                       'Content-Type': 'application/x-www-form-urlencoded',
                                       'Referer': base_url},
                              timeout=60)
            if 'pdf' not in r2.headers.get('Content-Type', '').lower():
                return ''
            reader = pypdf.PdfReader(_io.BytesIO(r2.content))
            return '\n'.join(p.extract_text() or '' for p in reader.pages)

        out  = []
        seen: set = set()
        ref_pat = _re.compile(r'^C\d{4,6}$')

        # ctl83 = EMI (Electronic Money Institutions) — has clean C-ref numbers
        # ctl01 (Credit Institutions) uses unstructured prose PDF, skipped
        for ctl in (83,):
            text = _dl_pdf(ctl)
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            i = 0
            while i < len(lines):
                line = lines[i]
                if ref_pat.match(line) and i + 1 < len(lines):
                    ref  = line
                    name = lines[i + 1].strip()
                    # Skip header/footer lines
                    if name and len(name) > 2 and not name.startswith('Page ') \
                            and not name.startswith('Run Date') \
                            and not name.startswith('Ref No'):
                        key = ref
                        if key not in seen:
                            seen.add(key)
                            out.append({'entry_id': ref, 'name': name,
                                        'status': 'Authorised',
                                        'url': base_url})
                    i += 2
                else:
                    # ctl01 (Credit Institutions) — only lines that look like institution names
                    # Must end in a known company-type suffix or contain Bank/Credit
                    _inst_kw = ('Bank', 'plc', 'p.l.c.', 'DAC', 'Limited', 'Company',
                                'Society', 'Union', 'Institution', 'Credit', 'Mortgage',
                                'Building', 'Savings', 'Finance', 'Capital')
                    if (ctl == 1 and len(line) > 5
                            and any(kw in line for kw in _inst_kw)
                            and not any(line.startswith(x) for x in
                                        ('Page ', 'Run Date', 'Section', 'Register',
                                         'This Register', '(a)', '(b)', '(c)', '(i)',
                                         'Authorisations', 'European Credit', 'Third Country',
                                         'Deposit-Taking', 'Member State', 'Branches',
                                         'Credit Institutions from', 'The following',
                                         'pursuant', 'authorised', 'Freedom of'))):
                        key = _slug(line)
                        if key not in seen:
                            seen.add(key)
                            out.append({'entry_id': key, 'name': line,
                                        'status': 'Authorised',
                                        'url': base_url})
                    i += 1
        return out if out else _err('CBI', Exception('no entries parsed'))
    except Exception as exc:
        return _err('CBI', exc)


# ── NBG Georgia ───────────────────────────────────────────────────────────────

def scrape_NBG_GE() -> list[dict]:
    """National Bank of Georgia — licensed commercial banks (Playwright, JS-rendered)."""
    page_url = 'https://nbg.gov.ge/en/licensed-commercial-banks'
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            pg = br.new_page()
            pg.goto(page_url, timeout=30000, wait_until='networkidle')
            pg.wait_for_timeout(3000)
            soup = BeautifulSoup(pg.content(), 'lxml')
            br.close()

        out  = []
        seen: set = set()
        # Banks are rendered as <h3>/<h4>/<p> elements; filter by known JSC/LLC/Ltd patterns
        import re as _re
        bank_pat = _re.compile(
            r'^(?:JSC|LLC|Ltd\.?|Joint Stock Company|OJSC|SC)\s+.{3,}',
            _re.IGNORECASE)
        for tag in soup.find_all(['h2', 'h3', 'h4', 'p', 'li', 'td', 'span']):
            name = tag.get_text(strip=True)
            if bank_pat.match(name) and len(name) < 150 and name not in seen:
                seen.add(name)
                out.append({'entry_id': _slug(name), 'name': name,
                            'status': 'Licensed', 'url': page_url,
                            'country': 'GE'})
        return out if out else _err('NBG-GE', Exception('no licensed banks found on page'))
    except Exception as exc:
        return _err('NBG-GE', exc)


# ── Dispatcher ────────────────────────────────────────────────────────────────

SCRAPERS = {
    'EBA':    scrape_EBA,
    'ESMA':   scrape_ESMA,
    # 'FCA':  scrape_FCA,    # blocked: Cloudflare/DNS — failing daily since 2026-04-24
    'BaFin':  scrape_BaFin,
    'OAM':    scrape_OAM,
    'FINMA':  scrape_FINMA,
    'DFSA':   scrape_DFSA,
    # 'FSRA': scrape_FSRA,   # blocked: Cloudflare/DNS — failing daily since 2026-04-24
    'VARA':   scrape_VARA,
    'CBUAE':  scrape_CBUAE,
    # 'FSC-MU': scrape_FSC_MU,  # blocked: Cloudflare/DNS — failing daily since 2026-04-24
    'BOM-MU': scrape_BOM_MU,
    'BDDK':       scrape_BDDK,
    'CBI':        scrape_CBI,
    'ESMA-ELTIF': scrape_ESMA_ELTIF,
    'ESMA-MiCA':  scrape_ESMA_MiCA,
    'NBG-GE':     scrape_NBG_GE,
}


# Scrapers that intentionally return [] (bot-blocked, disabled) — not treated as errors
_DISABLED_SCRAPERS = frozenset({
    'DFSA',    # 403 Cloudflare bot protection
    'CBUAE',   # 403 Cloudflare bot protection
    'FCA',     # Cloudflare Turnstile blocks headless
    'FSRA',    # Cloudflare/DNS failing
    'FSC-MU',  # Cloudflare/DNS failing
    'BOM-MU',  # page only serves forex rates widget in static HTML; bank list requires JS
})


def run_all() -> tuple[list[dict], list[str]]:
    """Run all scrapers. Returns (all_entries, error_registries)."""
    all_entries = []
    errors      = []
    for name, fn in SCRAPERS.items():
        try:
            entries = fn()
            # Deduplicate by entry_id within this registry
            seen: set = set()
            unique = []
            for e in entries:
                eid = e.get('entry_id', '')
                if eid and eid not in seen:
                    seen.add(eid)
                    e['registry'] = name
                    unique.append(e)
            all_entries.extend(unique)
            print(f"[{name}] {len(unique)} entries", file=sys.stderr)
            if not unique and name not in _DISABLED_SCRAPERS:
                errors.append(name)
        except Exception as exc:
            errors.append(name)
            print(f"[{name}] FAILED: {exc}", file=sys.stderr)
    return all_entries, errors
