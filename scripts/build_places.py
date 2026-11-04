#!/usr/bin/env python3
"""Which counties and municipalities each Pennsylvania district covers.

Overlays U.S. Census Bureau TIGER/Line 2024 boundaries: state Senate (SLDU), state House
(SLDL) and congressional (CD119) districts against county subdivisions (Pennsylvania's
cities, boroughs, townships and towns). Each municipality's 2020 Census population is
split by the share of its land area inside the district, which ranks the main places first.
In Philadelphia, which is a single municipality, neighborhoods are added when a public
neighborhood boundary file is available.

Output: reference/district_places.json
  {"source": ..., "districts": {"HD-44": {"counties": [["Allegheny", 1.0]],
     "places": [{"n": "Moon", "t": "township", "c": "Allegheny", "share": 1.0, "pop": 27000}, ...]}}}
"""
import json
import os
import sys
import urllib.request

import geopandas as gpd
import pandas as pd

W = 'work'
OUT = 'reference/district_places.json'
EQ_AREA = 5070  # NAD83 / Conus Albers, for area math
UA = {'User-Agent': 'PA-Election-Desk-data-build/1.0 (GitHub Actions)'}


def get(url, path):
    if os.path.exists(path):
        return path
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r, open(path, 'wb') as f:
        f.write(r.read())
    return path


def main():
    os.makedirs(W, exist_ok=True)
    base = 'https://www2.census.gov/geo/tiger/TIGER2024/'
    files = {
        'cousub': get(base + 'COUSUB/tl_2024_42_cousub.zip', f'{W}/cousub.zip'),
        'SD': get(base + 'SLDU/tl_2024_42_sldu.zip', f'{W}/sldu.zip'),
        'HD': get(base + 'SLDL/tl_2024_42_sldl.zip', f'{W}/sldl.zip'),
        'PA': get(base + 'CD/tl_2024_42_cd119.zip', f'{W}/cd119.zip'),
        'county': get(base + 'COUNTY/tl_2024_us_county.zip', f'{W}/county.zip'),
    }
    cty = gpd.read_file('zip://' + files['county'])
    cty = cty[cty.STATEFP == '42'][['COUNTYFP', 'NAME']].rename(columns={'NAME': 'county'})

    cs = gpd.read_file('zip://' + files['cousub'])
    cs = cs[cs.ALAND > 0].merge(cty, on='COUNTYFP', how='left')
    # 2020 Census population by county subdivision (public API, no key needed for one call)
    pop = {}
    try:  # Census population estimates for every municipality (static file, no key)
        import csv, io
        url = 'https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv'
        req = urllib.request.Request(url, headers=UA)
        raw = urllib.request.urlopen(req, timeout=180).read().decode('latin-1')
        for r in csv.DictReader(io.StringIO(raw)):
            if r.get('STATE') == '42' and r.get('SUMLEV') == '061':
                v = r.get('POPESTIMATE2024') or r.get('ESTIMATESBASE2020') or ''
                if v.isdigit():
                    pop[r['COUNTY'] + r['COUSUB']] = int(v)
        print('population rows (popest 2024):', len(pop))
    except Exception as e:
        print('population estimates failed:', e)
    if not pop:
        try:
            url = ('https://api.census.gov/data/2020/dec/pl?get=P1_001N&for=county%20subdivision:*'
                   '&in=state:42%20county:*')
            req = urllib.request.Request(url, headers=UA)
            body = urllib.request.urlopen(req, timeout=120).read().decode('utf-8', 'replace')
            rows = json.loads(body)
            h = rows[0]
            for r in rows[1:]:
                d = dict(zip(h, r))
                pop[d['county'] + d['county subdivision']] = int(d['P1_001N'])
            print('population rows (2020 Census):', len(pop))
        except Exception as e:
            print('population lookup failed, ranking by area:', e)
    cs['key'] = cs.COUNTYFP + cs.COUSUBFP
    cs['pop'] = cs.key.map(pop)
    cs['t'] = [str(full).replace(str(short), '', 1).strip() for full, short in zip(cs.NAMELSAD, cs.NAME)]
    cs = cs.to_crs(EQ_AREA)
    cs['area'] = cs.geometry.area

    # City neighborhoods (optional): Philadelphia and Pittsburgh are each one municipality
    def load_hoods(path_or_url, local):
        g = gpd.read_file(get(path_or_url, local) if path_or_url.startswith('http') else path_or_url).to_crs(EQ_AREA)
        cols = [c for c in g.columns if c != 'geometry' and g[c].dtype == object]
        name_col = next((c for c in cols if c.lower() in ('mapname', 'listname', 'hood', 'neighborhood', 'name')), None) or \
            next((c for c in cols if 'hood' in c.lower() or 'name' in c.lower()), None)
        g = g[[name_col, 'geometry']].rename(columns={name_col: 'hood'})
        g['harea'] = g.geometry.area
        return g
    hoods = {}
    try:
        hoods['Philadelphia'] = load_hoods('https://raw.githubusercontent.com/opendataphilly/odp-data-storage/master/philadelphia-neighborhoods/philadelphia-neighborhoods.geojson', f'{W}/phl_hoods.geojson')
        print('Philadelphia neighborhoods:', len(hoods['Philadelphia']))
    except Exception as e:
        print('Philadelphia neighborhoods unavailable:', e)
    try:
        base_url = 'https://services1.arcgis.com/YZCmUqbcsUpOKfj7/ArcGIS/rest/services'
        svc = json.load(urllib.request.urlopen(urllib.request.Request(base_url + '?f=json', headers=UA), timeout=60))
        names = [x['name'] for x in svc.get('services', []) if 'eighborhood' in x['name'] and x.get('type') == 'FeatureServer']
        print('Pittsburgh neighborhood services:', names[:8])
        for nm in names:
            try:
                url = f'{base_url}/{nm}/FeatureServer/0/query?where=1%3D1&outFields=*&outSR=4326&f=geojson'
                g = load_hoods(url, f'{W}/pgh_{nm}.geojson')
                if 60 <= len(g) <= 120:  # Pittsburgh has about 90 neighborhoods
                    hoods['Pittsburgh'] = g
                    print('Pittsburgh neighborhoods:', nm, len(g))
                    break
            except Exception as e:
                print('  skip', nm, e)
    except Exception as e:
        print('Pittsburgh neighborhoods unavailable:', e)

    out = {}
    for kind, col in (('SD', 'SLDUST'), ('HD', 'SLDLST'), ('PA', 'CD119FP')):
        d = gpd.read_file('zip://' + files[kind]).to_crs(EQ_AREA)
        d = d[d[col].str.isdigit()][[col, 'geometry']]
        ov = gpd.overlay(d, cs[['NAME', 't', 'county', 'pop', 'area', 'geometry']], how='intersection', keep_geom_type=True)
        ov['part_area'] = ov.geometry.area
        ov['share'] = ov['part_area'] / ov['area']  # ov.area would be the geometry's area, not the column
        for dist, g in ov.groupby(col):
            rid = f'{kind}-{int(dist)}'
            tot = g.part_area.sum()
            counties = g.groupby('county').part_area.sum().sort_values(ascending=False)
            counties = [[c, round(float(a / tot), 4)] for c, a in counties.items() if a / tot >= 0.002]
            places = []
            for _, r in g.iterrows():
                # drop boundary slivers: under 3% of the municipality and under 0.5 sq km
                if r.share < 0.03 and r.part_area < 500_000:
                    continue
                est = int(round(r['pop'] * r.share)) if pd.notna(r['pop']) else None
                places.append({'n': r.NAME, 't': r.t, 'c': r.county, 'share': round(float(min(r.share, 1.0)), 3), 'pop': est})
            places.sort(key=lambda p: (-(p['pop'] or 0), -p['share']))
            entry = {'counties': counties, 'places': places}
            for city, hg in hoods.items():
                if not any(p['n'] == city for p in places):
                    continue
                geom = d[d[col] == dist].geometry.iloc[0]
                hh = hg[hg.intersects(geom)].copy()
                hh['in'] = hh.geometry.intersection(geom).area / hh['harea']
                hh = hh[hh['in'] >= 0.25].sort_values('in', ascending=False)
                names = [str(x).strip() for x in hh.hood.tolist() if str(x).strip()]
                if names:
                    entry.setdefault('neighborhoods', {})[city] = names
            out[rid] = entry
        print(kind, 'districts:', sum(1 for k in out if k.startswith(kind + '-')))

    os.makedirs('reference', exist_ok=True)
    json.dump({'source': 'U.S. Census Bureau TIGER/Line 2024 (state legislative districts, 119th Congress districts, '
                         'county subdivisions) and 2020 Census population; Philadelphia neighborhoods from the '
                         'azavea/geo-data public dataset when available. Shares are by land area; populations are estimates.',
               'districts': out}, open(OUT, 'w'), ensure_ascii=False, indent=0)
    print('wrote', OUT, len(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
