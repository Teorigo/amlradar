import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Country config ────────────────────────────────────────────────────────────
PRIORITY_COUNTRIES = os.environ.get(
    'AML_PRIORITY_COUNTRIES',
    'PL,GE,TR,IT,DE,FR,ES,NL,LU,MT,CY,IE,EE,LT,CH,AE,MU,AT,BE,SE,DK,FI,PT,GR,HR,SI,SK,CZ,HU,RO,BG',
).split(',')

# ISO 3166-1 alpha-2 → country name (used in report + enrichment search)
COUNTRY_NAMES: dict[str, str] = {
    'AT': 'Austria',       'BE': 'Belgium',        'BG': 'Bulgaria',
    'CH': 'Switzerland',   'CY': 'Cyprus',          'CZ': 'Czech Republic',
    'DE': 'Germany',       'DK': 'Denmark',         'EE': 'Estonia',
    'ES': 'Spain',         'FI': 'Finland',         'FR': 'France',
    'GB': 'United Kingdom','GE': 'Georgia',          'GR': 'Greece',
    'HR': 'Croatia',       'HU': 'Hungary',          'IE': 'Ireland',
    'IT': 'Italy',         'LT': 'Lithuania',        'LU': 'Luxembourg',
    'LV': 'Latvia',        'MT': 'Malta',            'MU': 'Mauritius',
    'NL': 'Netherlands',   'PL': 'Poland',           'PT': 'Portugal',
    'RO': 'Romania',       'SE': 'Sweden',           'SI': 'Slovenia',
    'SK': 'Slovakia',      'TR': 'Turkey',           'AE': 'UAE',
    'IS': 'Iceland',       'LI': 'Liechtenstein',    'NO': 'Norway',
}

# Default country per registry (for registries without per-entity country)
REGISTRY_COUNTRY: dict[str, str] = {
    'FCA':    'GB',  'BaFin':  'DE',  'OAM':    'IT',
    'FINMA':  'CH',  'DFSA':   'AE',  'FSRA':   'AE',
    'VARA':   'AE',  'CBUAE':  'AE',  'FSC-MU': 'MU',
    'BOM-MU': 'MU',  'BDDK':   'TR',  'CBI':    'IE',
    'NBG-GE': 'GE',
}

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(BASE_DIR, 'amlradar.db')

# ── Email ─────────────────────────────────────────────────────────────────────
EMAIL_FROM         = os.environ.get('AML_EMAIL_FROM', 'you@gmail.com')
EMAIL_TO           = os.environ.get('AML_EMAIL_TO',   'you@gmail.com').split(',')
EMAIL_SUBJECT      = 'AML Radar — Daily Δ Report'
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')

# ── AI enrichment (Anthropic Claude w/ web_search tool) ───────────────────────
ANTHROPIC_API_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
AI_ENRICH_MODEL    = os.environ.get('AML_AI_MODEL', 'claude-haiku-4-5-20251001')
AI_ENRICH_MAX_SEARCHES_PER_ENTITY = int(os.environ.get('AML_AI_MAX_SEARCHES', '3'))

# ── HTTP ──────────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 30
REQUEST_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Referer': 'https://www.google.com/',
}

# ── Registries ────────────────────────────────────────────────────────────────
REGISTRY_URLS = {
    'EBA':    'https://euclid.eba.europa.eu/register/pir/search',
    'ESMA':   'https://registers.esma.europa.eu/publication/searchRegister/doDownload',
    'FCA':    'https://register.fca.org.uk/services/V0.1/Firm/search',
    'BaFin':  'https://portal.mvp.bafin.de/database/InstInfo/institutList.do?cmd=prepareKategoriensuche&typ=I',
    'OAM':    'https://www.organismo-am.it/elenchi-registri/operatori_valute_virtuali/index.html',
    'FINMA':  'https://www.finma.ch/en/finma-public/authorised-institutions-individuals-and-products/',
    'DFSA':   'https://www.dfsa.ae/public-register/firms',
    'FSRA':   'https://www.adgm.com/public-registers/fsra',
    'VARA':   'https://www.vara.ae/en/licenses-and-register/public-register/',
    'CBUAE':  'https://www.centralbank.ae/en/our-functions/financial-system-regulation/licensing/list-of-licensed-entities/',
    'FSC-MU': 'https://www.fscmauritius.org/en/supervision/register-of-licensees',
    'BOM-MU': 'https://www.bom.mu/financial-stability/banking-sector/list-of-licensed-banks',
    'BDDK':   'https://www.bddk.org.tr/Kurulus/Liste/90',
    'CBI':        'http://registers.centralbank.ie/downloadspage.aspx',
    'ESMA-ELTIF': 'https://www.esma.europa.eu/sites/default/files/library/esma34-46-101_esma_register_eltif_art33.xlsx',
    'ESMA-MiCA':  'https://www.esma.europa.eu/sites/default/files/2024-12/CASPS.csv',
    'NBG-GE': 'https://nbg.gov.ge/en/licensed-commercial-banks',
}
