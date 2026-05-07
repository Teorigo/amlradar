"""
AML Radar — entity enrichment (website, email, phone).
Strategy: domain guessing (no search engine API needed) + contact page scraping.
Search engines block automated requests, so we infer domain from company name + country.
"""

import re
import json
import time
import sys
import requests
from bs4 import BeautifulSoup
from config import (REQUEST_TIMEOUT, REQUEST_HEADERS, COUNTRY_NAMES,
                    ANTHROPIC_API_KEY, AI_ENRICH_MODEL,
                    AI_ENRICH_MAX_SEARCHES_PER_ENTITY)

_SESSION = requests.Session()
_SESSION.headers.update({**REQUEST_HEADERS, 'Accept-Encoding': 'gzip, deflate'})

# Remove legal entity form suffixes before domain guessing
_LEGAL_RE = re.compile(
    r'\b(GmbH|AG|S\.?A\.?|SAS|S\.?R\.?L\.?|Ltd\.?|Limited|PLC|LLC|LLP'
    r'|B\.?V\.?|N\.?V\.?|SpA|S\.p\.A\.?|DAC|A/S|ApS|AB|AS|OÜ|UAB|SIA'
    r'|JSC|PJSC|Inc\.?|Corp\.?|Group|Holding|Holdings|Bank|Finance'
    r'|Financial|Services|Payments|Payment|Solutions|Technologies|Tech'
    r'|Digital|Capital|Investments|Management)\b',
    re.I,
)

_COUNTRY_TLD: dict[str, str] = {
    'AT': '.at',   'DE': '.de',   'FR': '.fr',  'IT': '.it',   'ES': '.es',
    'NL': '.nl',   'BE': '.be',   'PL': '.pl',  'CZ': '.cz',   'SK': '.sk',
    'HU': '.hu',   'RO': '.ro',   'BG': '.bg',  'HR': '.hr',   'SI': '.si',
    'PT': '.pt',   'GR': '.gr',   'SE': '.se',  'DK': '.dk',   'FI': '.fi',
    'MT': '.mt',   'CY': '.cy',   'LU': '.lu',  'IE': '.ie',   'GB': '.co.uk',
    'EE': '.ee',   'LT': '.lt',   'LV': '.lv',  'CH': '.ch',   'NO': '.no',
    'IS': '.is',   'LI': '.li',   'TR': '.com.tr', 'AE': '.ae', 'MU': '.mu',
    'GE': '.ge',
}

_FREE_EMAIL = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
               'icloud.com', 'protonmail.com', 'example.com', 'test.com'}
_FILE_EXTS  = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'pdf', 'ico', 'webp',
               'mp4', 'mp3', 'css', 'js', 'ts', 'tsx', 'jsx', 'woff', 'woff2'}
_PREFER_PREFIX = ('info', 'contact', 'compliance', 'office',
                  'legal', 'mlro', 'aml', 'admin', 'hello', 'support')

_EMAIL_RE = re.compile(r'[\w\.\+\-]+@[\w\.\-]+\.[a-z]{2,}', re.I)


def _slug(text: str) -> str:
    s = re.sub(r'[^a-z0-9]', '-', text.lower())
    return re.sub(r'-+', '-', s).strip('-')


def _guess_domains(name: str, country: str) -> list[str]:
    """Generate plausible domain candidates from company name + country."""
    # Strip legal suffixes and bracketed content
    clean = _LEGAL_RE.sub(' ', name)
    clean = re.sub(r'[\(\)\[\]"\']', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()

    slug  = _slug(clean)
    words = [w for w in slug.split('-') if len(w) > 2]
    if not words:
        return []

    # Build candidates: full slug + first meaningful word
    short = words[0]
    tld   = _COUNTRY_TLD.get(country, '')

    candidates = []
    for base in dict.fromkeys([slug, slug.replace('-', ''), short]):
        if not base:
            continue
        candidates.append(f'https://www.{base}.com')
        if tld and tld != '.com':
            candidates.append(f'https://www.{base}{tld}')
        candidates.append(f'https://{base}.com')
        if tld and tld != '.com':
            candidates.append(f'https://{base}{tld}')

    return list(dict.fromkeys(candidates))[:3]


def _check_url(url: str, timeout: int = 5) -> bool:
    """Return True if URL responds with 2xx/3xx."""
    try:
        r = _SESSION.head(url, timeout=timeout, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False


def _fetch_text(url: str, timeout: int = 7) -> str:
    try:
        r = _SESSION.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ''


def _extract_emails(html: str) -> list[str]:
    decoded = html.replace('&#64;', '@').replace('%40', '@')
    cands   = _EMAIL_RE.findall(decoded)
    valid   = [e.lower() for e in cands
               if '.' in e.split('@')[1]
               and e.split('@')[1].lower() not in _FREE_EMAIL
               and e.split('@')[1].lower().split('.')[-1] not in _FILE_EXTS
               and len(e) < 80]
    preferred = [e for e in valid if e.split('@')[0].lower() in _PREFER_PREFIX]
    return (preferred or valid)[:3]


def _extract_phones(html: str, country_code: str = '') -> list[str]:
    try:
        import phonenumbers
        seen: set = set()
        results:  list[str] = []
        # Find raw phone-like patterns
        for m in re.finditer(r'[\+\(]?[\d][\d\s\-\(\)\.]{5,18}[\d]', html):
            candidate = m.group(0)
            digits    = re.sub(r'\D', '', candidate)
            if not (7 <= len(digits) <= 15) or digits in seen:
                continue
            seen.add(digits)
            for region in ([country_code] if country_code else []) + [None]:
                try:
                    p = phonenumbers.parse(candidate, region)
                    if phonenumbers.is_valid_number(p):
                        results.append(phonenumbers.format_number(
                            p, phonenumbers.PhoneNumberFormat.INTERNATIONAL))
                        break
                except Exception:
                    pass
            if len(results) >= 2:
                break
        return results
    except Exception:
        return []


def enrich(delta: dict, timeout: int = 15) -> dict:
    """
    Enrich one entity. delta must have: entry_id, registry, entry_name, country.
    Returns dict with website, email, phone (empty strings when not found).
    """
    name         = (delta.get('entry_name') or delta.get('new_name') or '').strip()
    country_code = delta.get('country', '')
    result       = {'website': '', 'email': '', 'phone': ''}

    if not name:
        return result

    deadline = time.time() + timeout

    # 1. Find a live domain via guessing
    website = ''
    for candidate in _guess_domains(name, country_code):
        if time.time() > deadline:
            break
        if _check_url(candidate, timeout=min(4, int(deadline - time.time()) + 1)):
            website = candidate
            break

    if not website:
        return result

    result['website'] = website

    # 2. Scrape homepage + contact pages for email / phone
    from urllib.parse import urljoin, urlparse
    base_url   = f"{urlparse(website).scheme}://{urlparse(website).netloc}"
    contact_paths = ('/contact', '/contacts', '/about', '/impressum',
                     '/legal', '/kontakt', '/en/contact', '/en/about')

    all_emails: list[str] = []
    all_phones: list[str] = []

    for page_url in [website] + [urljoin(base_url, p) for p in contact_paths]:
        if time.time() > deadline:
            break
        html = _fetch_text(page_url, timeout=min(5, int(deadline - time.time()) + 1))
        if not html:
            continue
        all_emails.extend(_extract_emails(html))
        all_phones.extend(_extract_phones(html, country_code))
        if all_emails and all_phones:
            break

    if all_emails:
        result['email'] = all_emails[0]
    if all_phones:
        result['phone'] = all_phones[0]

    return result


_AI_PROMPT = """You are looking up public contact details for a regulated financial entity, \
to populate an AML compliance lead-tracking report.

Entity: {name}
Country: {country}

Use web search to find:
1. Official corporate website (root domain, e.g. https://www.example.com)
2. Public contact email — prefer info@, contact@, compliance@, legal@, mlro@, aml@
3. Public phone number in international format (+CC ...)

Rules:
- Only return values you actually find in search results. Never invent or guess.
- Skip free-mail domains (gmail/yahoo/hotmail/outlook/proton).
- The phone must be the company's own number, not a regulator hotline.
- If a value cannot be found, return empty string for that field.

Return a single JSON object on the final line of your reply, nothing else after it:
{{"website": "...", "email": "...", "phone": "..."}}"""


def _extract_final_json(text: str) -> dict:
    """Pull the last JSON object out of a text response."""
    matches = list(re.finditer(r'\{[^{}]*"website"[^{}]*\}', text, re.DOTALL))
    if not matches:
        return {}
    try:
        return json.loads(matches[-1].group(0))
    except Exception:
        return {}


def _ai_validate(value: str, kind: str) -> str:
    """Discard obvious junk values returned by the model."""
    if not value or not isinstance(value, str):
        return ''
    v = value.strip()
    if not v or v.lower() in ('n/a', 'null', 'none', 'unknown', 'not found'):
        return ''
    if kind == 'email':
        if '@' not in v:
            return ''
        domain = v.split('@', 1)[1].lower()
        if domain in _FREE_EMAIL or '.' not in domain:
            return ''
    elif kind == 'phone':
        digits = re.sub(r'\D', '', v)
        if not (7 <= len(digits) <= 15):
            return ''
    elif kind == 'website':
        if not v.startswith(('http://', 'https://')):
            return ''
    return v


def enrich_via_ai(delta: dict, timeout: int = 60) -> dict:
    """Look up website / email / phone via Claude + web_search server tool.
    Returns {website, email, phone} (empty strings on failure or missing key)."""
    result = {'website': '', 'email': '', 'phone': ''}
    if not ANTHROPIC_API_KEY:
        return result

    name         = (delta.get('entry_name') or delta.get('new_name') or '').strip()
    country_code = delta.get('country', '')
    if not name:
        return result
    country_name = COUNTRY_NAMES.get(country_code, country_code or 'unknown')

    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':       AI_ENRICH_MODEL,
                'max_tokens':  600,
                'tools':       [{
                    'type':     'web_search_20250305',
                    'name':     'web_search',
                    'max_uses': AI_ENRICH_MAX_SEARCHES_PER_ENTITY,
                }],
                'messages': [{
                    'role':    'user',
                    'content': _AI_PROMPT.format(name=name, country=country_name),
                }],
            },
            timeout=timeout,
        )
    except Exception as exc:
        print(f'[ai-enrich] API error for "{name}": {exc}', file=sys.stderr)
        return result

    if r.status_code != 200:
        print(f'[ai-enrich] HTTP {r.status_code} for "{name}": {r.text[:200]}',
              file=sys.stderr)
        return result

    data = r.json()
    text = ''
    for block in data.get('content', []):
        if block.get('type') == 'text':
            text += block.get('text', '')

    parsed = _extract_final_json(text)
    return {
        'website': _ai_validate(parsed.get('website', ''), 'website'),
        'email':   _ai_validate(parsed.get('email', ''),   'email'),
        'phone':   _ai_validate(parsed.get('phone', ''),   'phone'),
    }


def enrich_batch(deltas: list[dict], max_items: int = 50,
                 delay: float = 0.3) -> list[dict]:
    """Enrich up to max_items deltas. Uses AI (Claude + web_search) when
    ANTHROPIC_API_KEY is set; falls back to domain-guessing scraper otherwise.
    AI result is preferred; if AI returns no website, the legacy scraper is run
    as a secondary fallback for that single entity."""
    use_ai  = bool(ANTHROPIC_API_KEY)
    if use_ai:
        print(f'[enrich] AI mode (model={AI_ENRICH_MODEL}, '
              f'max_searches={AI_ENRICH_MAX_SEARCHES_PER_ENTITY}/entity)',
              file=sys.stderr)
    else:
        print('[enrich] AI disabled (ANTHROPIC_API_KEY not set) — domain-guessing only',
              file=sys.stderr)

    results = []
    for d in deltas[:max_items]:
        enriched = {'website': '', 'email': '', 'phone': ''}
        try:
            if use_ai:
                enriched = enrich_via_ai(d)
            if not enriched.get('website') and not enriched.get('email'):
                # AI found nothing usable (or AI disabled) → try legacy scraper
                fallback = enrich(d)
                # Merge: keep any AI-found field, fill blanks from fallback
                for k in ('website', 'email', 'phone'):
                    if not enriched.get(k) and fallback.get(k):
                        enriched[k] = fallback[k]
        except Exception as exc:
            print(f'[enrich] error on {d.get("entry_name", "?")}: {exc}',
                  file=sys.stderr)
        results.append({**d, **enriched})
        if delay:
            time.sleep(delay)
    return results
