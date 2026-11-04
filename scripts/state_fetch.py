#!/usr/bin/env python3
"""Read unofficial returns from the Pennsylvania Department of State results site.

Runs on GitHub Actions (.github/workflows/state-feed.yml). The state site
(electionreturns.pa.gov) is a JavaScript app, so this opens it in headless
Chromium, records the JSON data the page loads, asks the site's own API for
its summary, visits the legislative, congressional and governor pages, and
pulls out the races. Output: data/state.json, read by the PA Election Desk
site and by the hourly news monitor.

A short discovery log goes to state/discovery.json on every run, and full
copies of the data the page loaded go to state/raw/ on the first run, when
something fails, or when the workflow is started with "save raw" turned on.
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

BASE = os.environ.get('STATE_BASE', 'https://www.electionreturns.pa.gov').rstrip('/')
ROOT = Path(os.environ.get('REPO_ROOT', Path(__file__).resolve().parent.parent))
OUT = ROOT / 'data' / 'state.json'
DBG = ROOT / 'state'
SAVE_RAW = os.environ.get('SAVE_RAW', '') in ('1', 'true', 'yes')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36')
MAX_PAGES = 14            # office pages to open per run
MAX_RAW_BYTES = 4_000_000  # per saved response

# The site's summary call as used by its own pages ("undefined" = the active election).
OFFICES = [('Senator in the General Assembly', 12), ('Representative in the General Assembly', 13), ('Representative in Congress', 11)]
ELECTION_ID = os.environ.get('STATE_ELECTION_ID', '117')  # 2026 General Election on electionreturns.pa.gov
OFFICE_RX = re.compile(r'(general assembly|representative in congress|governor|state senat|state house|congress)', re.I)
BLOCK_RX = re.compile(r'(access denied|request blocked|unusual traffic|captcha|forbidden|not authorized)', re.I)
COUNTIES = set('''adams allegheny armstrong beaver bedford berks blair bradford bucks butler cambria cameron carbon
centre chester clarion clearfield clinton columbia crawford cumberland dauphin delaware elk erie fayette forest
franklin fulton greene huntingdon indiana jefferson juniata lackawanna lancaster lawrence lebanon lehigh luzerne
lycoming mckean mercer mifflin monroe montgomery montour northampton northumberland perry philadelphia pike
potter schuylkill snyder somerset sullivan susquehanna tioga union venango warren washington wayne westmoreland
wyoming york'''.split())

KEYS = {
    'name': ('candidatename', 'candidate', 'ballotname', 'fullname', 'name'),
    'votes': ('votes', 'totalvotes', 'votecount', 'candidatevotes', 'total'),
    'party': ('partyname', 'party', 'partycode', 'partyabbreviation', 'partyabbr'),
    'pct': ('percentage', 'percent', 'votepercentage', 'pct'),
    'office': ('officename', 'office', 'contestname', 'racename', 'officetitle'),
    'district': ('districtname', 'district', 'districtnumber', 'districtno', 'districtid'),
    'county': ('countyname', 'county'),
}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def decode(x):
    """Some APIs return JSON inside a JSON string; unwrap up to three layers."""
    for _ in range(3):
        if isinstance(x, str):
            s = x.strip()
            if s[:1] in '[{"' and len(s) > 1:
                try:
                    x = json.loads(s)
                    continue
                except ValueError:
                    return x
        break
    return x


def pick(d, field):
    low = {str(k).lower().replace('_', '').replace(' ', ''): k for k in d.keys()}
    for want in KEYS[field]:
        if want in low:
            v = d[low[want]]
            if v not in (None, ''):
                return v
    return None


def to_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.replace(',', '').strip()
        if re.fullmatch(r'\d+(\.0+)?', s):
            return int(float(s))
    return None


def to_pct(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        m = re.search(r'\d+(\.\d+)?', v)
        if m:
            return float(m.group(0))
    return None


def label_kind(k):
    s = str(k)
    if OFFICE_RX.search(s) or re.search(r'\b(senator|representative|lieutenant|attorney general|auditor|treasurer|judge|justice)\b', s, re.I):
        return 'office'
    if re.search(r'district', s, re.I):
        return 'district'
    if s.strip().lower().replace(' county', '') in COUNTIES:
        return 'county'
    return None


def walk(x, info, out):
    """Collect candidate-like records with the office/district/county they sit under."""
    x = decode(x)
    if isinstance(x, list):
        for v in x:
            walk(v, info, out)
        return
    if not isinstance(x, dict):
        return
    info = dict(info)
    for f in ('office', 'district', 'county'):
        v = pick(x, f)
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            info[f] = str(v)
    name, votes = pick(x, 'name'), to_int(pick(x, 'votes'))
    name_kind = label_kind(name) if isinstance(name, str) else None
    if name_kind and pick(x, name_kind) is None:
        info[name_kind] = name  # e.g. {"Name": "Governor", "Candidates": [...]}
    if isinstance(name, str) and votes is not None and name.strip() and not name_kind:
        out.append({**info, 'name': name.strip(), 'votes': votes,
                    'party': str(pick(x, 'party') or ''), 'pct': to_pct(pick(x, 'pct'))})
    for k, v in x.items():
        v = decode(v)
        if isinstance(v, (list, dict)):
            sub = dict(info)
            kind = label_kind(k)
            if kind:
                sub[kind] = str(k)
            walk(v, sub, out)


def party_code(p):
    s = (p or '').strip().upper()
    if s.startswith('DEM') or s == 'D':
        return 'D'
    if s.startswith('REP') or s == 'R':
        return 'R'
    if s.startswith('GR') or s == 'G':
        return 'G'
    if s.startswith('LIB') or s == 'L':
        return 'L'
    return 'I' if s else ''


def race_id(office, district):
    o = (office or '').lower()
    num = None
    for src in (district or '', office or ''):
        m = re.search(r'(\d{1,3})', src)
        if m:
            num = int(m.group(1))
            break
    if 'governor' in o and 'lieutenant' not in o:
        return 'GOV'
    if num is None:
        return None
    if ('senat' in o and ('general assembly' in o or 'state' in o)) or 'senatorial' in (district or '').lower():
        return f'SD-{num}' if 1 <= num <= 50 else None
    if ('representative in the general assembly' in o or 'state house' in o or 'state representative' in o
            or 'legislative' in (district or '').lower()):
        return f'HD-{num}' if 1 <= num <= 203 else None
    if 'congress' in o:
        return f'PA-{num}' if 1 <= num <= 17 else None
    return None


def build_races(records):
    totals, by_county = {}, {}
    for r in records:
        rid = race_id(r.get('office'), r.get('district'))
        if not rid:
            continue
        key = (rid, re.sub(r'\s+', ' ', r['name']).upper())
        county = (r.get('county') or '').strip().lower().replace(' county', '')
        bucket = by_county.setdefault(key, {}) if county in COUNTIES else None
        if bucket is not None:
            bucket[county] = max(bucket.get(county, 0), r['votes'])
            totals.setdefault(key, {'rec': r, 'votes': None})
        else:
            cur = totals.get(key)
            if not cur or cur['votes'] is None or r['votes'] >= cur['votes']:
                totals[key] = {'rec': r, 'votes': r['votes'], 'pct': r.get('pct')}
    races = {}
    for (rid, _), t in totals.items():
        r = t['rec']
        votes = t['votes'] if t['votes'] is not None else sum(by_county.get((rid, _), {}).values())
        race = races.setdefault(rid, {'office': r.get('office') or '', 'district': r.get('district') or '',
                                      'candidates': [], 'counties': 0})
        race['counties'] = max(race['counties'], len(by_county.get((rid, _), {})))
        race['candidates'].append({'name': r['name'], 'party': party_code(r.get('party')),
                                   'party_name': r.get('party') or '', 'votes': votes,
                                   'site_pct': t.get('pct') if t['votes'] is not None else None})
    for race in races.values():
        tot = sum(c['votes'] for c in race['candidates'])
        race['total_votes'] = tot
        use_site = race['counties'] == 0 and all(c['site_pct'] is not None for c in race['candidates'])
        for c in race['candidates']:
            c['pct'] = c['site_pct'] if use_site else (round(100 * c['votes'] / tot, 2) if tot else None)
            del c['site_pct']
        race['candidates'].sort(key=lambda c: -c['votes'])
        if len(race['candidates']) > 1:
            race['margin_votes'] = race['candidates'][0]['votes'] - race['candidates'][1]['votes']
        race['leader'] = race['candidates'][0]['name'] if tot else None
        race['leader_party'] = race['candidates'][0]['party'] if tot else None
    return dict(sorted(races.items(), key=lambda kv: (kv[0].split('-')[0], int(kv[0].split('-')[1]) if '-' in kv[0] else 0)))


def fetch():
    from playwright.sync_api import sync_playwright
    caps, log, pages = [], [], {}
    status, detail = 'ok', ''
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale='en-US', timezone_id='America/New_York',
                                  viewport={'width': 1366, 'height': 900})
        page = ctx.new_page()

        def on_response(resp):
            try:
                url = resp.url
                if not url.startswith(BASE):
                    return
                ctype = (resp.headers or {}).get('content-type', '')
                if 'json' not in ctype and '/api/' not in url.lower():
                    return
                body = resp.text()
                caps.append({'url': url, 'status': resp.status, 'ctype': ctype, 'body': body})
            except Exception as e:  # body may be unavailable for redirects
                log.append(f'capture error {resp.url}: {e}')

        page.on('response', on_response)
        try:
            resp = page.goto(BASE + '/', wait_until='networkidle', timeout=90_000)
            home_status = resp.status if resp else None
        except Exception as e:
            browser.close()
            return [], {}, 'error', f'Could not open the state site: {e}', log
        page.wait_for_timeout(2500)
        text = page.evaluate('document.body ? document.body.innerText : ""')
        pages['home'] = {'status': home_status, 'url': page.url, 'text': text[:20000]}
        if (home_status and home_status >= 400) or (BLOCK_RX.search(text[:3000]) and len(text) < 3000):
            status, detail = 'blocked', f'The state site answered {home_status} and showed: {text[:200]!r}'

        if status == 'ok':
            links = page.evaluate('''() => [...document.querySelectorAll('a[href]')].map(a => ({t: (a.innerText || a.textContent || '').trim().slice(0, 120), h: a.href}))''')
            pages['home']['links'] = links[:300]
            # The office links live in the site's "Offices" menu and run JavaScript instead of
            # opening a URL, so click them inside the page the way a visitor would. No direct
            # requests to the site's data calls: its bot protection refuses those (HTTP 403).
            # Office results pages have their own addresses. Open them like a visitor following a
            # link and give them time to load. This does not hide the browser or work around the
            # site's bot protection: if the site declines to serve a page, the run records that.
            for name, oid in OFFICES:
                try:
                    url = f'{BASE}/General/OfficeResults?officeId={oid}&ElectionID={ELECTION_ID}&ElectionType=G&IsActive=1'
                    r = page.goto(url, wait_until='networkidle', timeout=60_000)
                    try:
                        page.wait_for_function('document.body && document.body.innerText.length > 800', timeout=25_000)
                    except Exception:
                        log.append(f'{name}: page stayed blank after 25 s (likely refused by the site\'s bot protection)')
                    page.wait_for_timeout(2000)
                    t = page.evaluate('document.body ? document.body.innerText : ""')
                    sel = page.evaluate('''() => [...document.querySelectorAll('select')].map(s => ({id: s.id, name: s.name, n: s.options.length, first: [...s.options].slice(0, 8).map(o => o.text + '=' + o.value)}))''')
                    pages[name] = {'status': r.status if r else None, 'url': page.url, 'text': t[:20000], 'selects': sel}
                except Exception as e:
                    log.append(f'office page failed {name}: {type(e).__name__}: {e}'[:300])
        browser.close()
    return caps, pages, status, detail, log


def banner(pages):
    text = (pages.get('home') or {}).get('text', '')
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    flag = next((ln for ln in lines if re.search(r'\b(un)?official\b', ln, re.I)), '')
    upd = next((ln for ln in lines if re.search(r'last\s*updated|as of', ln, re.I)), '')
    return flag[:200], upd[:200]


def strip_volatile(d):
    d = dict(d)
    d.pop('checked_at', None)
    return d


def main():
    checked = now_iso()
    try:
        caps, pages, status, detail, log = fetch()
    except Exception as e:  # never fail silently: record the error in the output
        caps, pages, status, detail, log = [], {}, 'error', f'{type(e).__name__}: {e}', []
    records = []
    for c in caps:
        try:
            walk(json.loads(c['body']), {}, records)
        except Exception:
            continue
    races = build_races(records)
    if status == 'ok' and not races:
        status = 'no_races_found'
        detail = (f'The site loaded ({len(caps)} data responses) but no legislative, congressional or governor '
                  'results were recognized. See state/discovery.json.')
    flag, upd = banner(pages)
    reporting = None
    for c in caps:
        if 'GetDashboardMessage' in c['url']:
            try:
                m = decode(json.loads(c['body']))
                if isinstance(m, dict):
                    reporting = {'message': (m.get('DistrictMessage') or '').strip(), 'reporting': to_int(m.get('ReportingDistrict')),
                                 'total': to_int(m.get('VotingDistrict')), 'pct': to_pct(m.get('Percentage'))}
            except Exception:
                pass
    out = {
        'source': 'Pennsylvania Department of State, electionreturns.pa.gov (unofficial returns until certified)',
        'source_url': BASE + '/',
        'checked_at': checked,
        'status': status,
        'status_detail': detail,
        'site_status_text': flag,
        'site_updated_text': upd,
        'precincts': reporting,
        'race_count': len(races),
        'races': races,
    }
    old = None
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text())
        except ValueError:
            old = None
    changed = old is None or strip_volatile(old) != strip_volatile(out)
    stale = True
    if old and old.get('checked_at'):
        try:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(old['checked_at'].replace('Z', '+00:00'))
            stale = age > dt.timedelta(minutes=55)
        except ValueError:
            pass
    # Keep git history small: write when something changed, or hourly as a heartbeat.
    if changed or stale:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n')

    DBG.mkdir(parents=True, exist_ok=True)
    disc = {
        'status': status,
        'detail': detail,
        'responses': [{'url': c['url'], 'status': c['status'], 'ctype': c['ctype'], 'bytes': len(c['body'] or '')} for c in caps],
        'pages': {k: {'status': v.get('status'), 'url': v.get('url'), 'selects': v.get('selects'), 'text_start': (v.get('text') or '')[:3000]} for k, v in pages.items()},
        'office_links': [ln for ln in (pages.get('home') or {}).get('links', []) if OFFICE_RX.search(ln.get('t') or '')][:60],
        'records_found': len(records),
        'races_found': sorted(races.keys()),
        'log': log[:50],
    }
    (DBG / 'discovery.json').write_text(json.dumps(disc, indent=1, ensure_ascii=False) + '\n')
    raw_dir = DBG / 'raw'
    if SAVE_RAW or status != 'ok' or not raw_dir.exists():
        raw_dir.mkdir(parents=True, exist_ok=True)
        for f in raw_dir.glob('*'):
            f.unlink()
        for i, c in enumerate(caps[:60]):
            (raw_dir / f'{i:02d}.json').write_text(json.dumps({'url': c['url'], 'status': c['status'], 'body': (c['body'] or '')[:MAX_RAW_BYTES]}, ensure_ascii=False))
        for i, (k, v) in enumerate(pages.items()):
            (raw_dir / f'page-{i:02d}.txt').write_text(f"{k}\n{v.get('url')}\n\n{v.get('text') or ''}")
    print(json.dumps({'status': status, 'races': len(races), 'responses': len(caps), 'written': changed or stale}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
