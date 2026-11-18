import json
from pathlib import Path
import pandas as pd
import requests
import re
import io
import zipfile
from datetime import datetime, timezone

REQUIRED = ["object_id", "name", "address", "latitude", "longitude", "object_type", "heritage_category", "dating", "protection_status", "area", "building_info", "land_info", "technical_condition", "subject_of_protection", "legal_restrictions", "current_use", "urban_context", "transport_access", "available_premises", "owner_info", "source", "publication_date", "acquired_at"]
OPTIONAL = ["photo_url", "verification_status", "source_date", "style_or_function", "function", "photo_search_url"]
GROUPS = {
    "technical": ["technical_condition", "building_info", "area"],
    "legal": ["protection_status", "subject_of_protection", "legal_restrictions"],
    "urban": ["urban_context", "transport_access", "land_info"],
    "investment": ["current_use", "available_premises", "owner_info"],
    "spatial": ["latitude", "longitude", "address"],
    "provenance": ["source", "publication_date", "acquired_at"],
}
DEFAULT_WEIGHTS = {"technical": .25, "legal": .2, "urban": .15, "investment": .15, "spatial": .1, "provenance": .15}


def empty_frame():
    return pd.DataFrame(columns=REQUIRED)


def load_file(uploaded_file):
    name = uploaded_file.name.lower()
    if name.endswith('.csv'):
        return pd.read_csv(uploaded_file)
    if name.endswith(('.xlsx', '.xls')):
        return pd.read_excel(uploaded_file)
    if name.endswith('.json'):
        raw = json.load(uploaded_file)
        return pd.DataFrame(raw if isinstance(raw, list) else raw.get('records', raw))
    raise ValueError('�������������� CSV, JSON � Excel')


def normalize(df):
    out = df.copy()
    for col in REQUIRED:
        if col not in out.columns: out[col] = pd.NA
    for col in OPTIONAL:
        if col not in out.columns: out[col] = pd.NA
    return out[REQUIRED + OPTIONAL].copy()


def fetch_overpass(bbox=(57.75, 28.15, 57.90, 28.35), endpoint='https://overpass-api.de/api/interpreter', timeout=60):
    south, west, north, east = bbox
    query = f'''[out:json][timeout:50];(nwr["historic"="monument"]({south},{west},{north},{east});nwr["heritage"]({south},{west},{north},{east}););out center tags;'''
    response = requests.post(endpoint, data=query, timeout=timeout)
    response.raise_for_status()
    rows = []
    for item in response.json().get('elements', []):
        tags = item.get('tags', {})
        center = item.get('center', item)
        rows.append({'object_id': f"osm:{item.get('type')}:{item.get('id')}", 'name': tags.get('name'),
                     'address': tags.get('addr:street'), 'latitude': center.get('lat'), 'longitude': center.get('lon'),
                     'object_type': tags.get('historic') or tags.get('heritage'), 'source': 'OpenStreetMap',
                     'acquired_at': datetime.now(timezone.utc).isoformat()})
    return normalize(pd.DataFrame(rows))


def fetch_json_source(url, timeout=60):
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return normalize(pd.DataFrame(payload if isinstance(payload, list) else payload.get('records', payload)))


def save_snapshot(df, directory='data/raw', prefix='snapshot'):
    Path(directory).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = Path(directory) / f'{prefix}_{stamp}.csv'
    normalize(df).to_csv(path, index=False)
    return path


def fetch_mkrf_dataset(page_url='https://opendata.mkrf.ru/opendata/7705851331-egrkn/', region='Псковская область', city='Псков', timeout=120):
    """Автоматически находит актуальную выгрузку на странице ЕГРОКН и оставляет Псков."""
    page = requests.get(page_url, timeout=timeout)
    page.raise_for_status()
    names = re.findall(r'data-name="(data-\d+-structure-\d+)"', page.text)
    if not names:
        raise ValueError('На странице ЕГРОКН не найдена ссылка на выгрузку')
    base = page_url.rstrip('/') + '/'
    last_error = None
    for name in dict.fromkeys(reversed(names)):
        for ext in ('json.zip', 'csv.zip', 'json', 'csv'):
            try:
                response = requests.get(base + name + '.' + ext, timeout=timeout)
                if response.status_code != 200 or not response.content: continue
                if ext.endswith('.zip'):
                    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                        member = next((x for x in archive.namelist() if x.lower().endswith(('.json', '.csv'))), None)
                        if not member: continue
                        raw = archive.read(member)
                        frame = pd.read_json(io.BytesIO(raw)) if member.lower().endswith('json') else pd.read_csv(io.BytesIO(raw), encoding='cp1251')
                elif ext == 'json': frame = pd.DataFrame(response.json())
                else: frame = pd.read_csv(io.BytesIO(response.content), encoding='cp1251')
                frame = frame.astype(object)
                text = frame.fillna('').astype(str).agg(' '.join, axis=1)
                selected = frame[text.str.contains(f'{region}|{city}', case=False, regex=True, na=False)]
                return normalize(selected), name + '.' + ext, len(frame)
            except Exception as exc: last_error = exc
    raise RuntimeError(f'Не удалось скачать выгрузку ЕГРОКН: {last_error}')


def audit(df, stale_days=730):
    if df.empty:
        return {"total": 0, "with_id": 0, "with_coordinates": 0, "fill_rate": 0.0, "stale": 0, "contradictions": 0, "unverified": 0, "missing_fields": REQUIRED.copy()}
    x = normalize(df)
    present = x.notna() & x.astype(str).ne('') & x.astype(str).ne('<NA>')
    dates = pd.to_datetime(x['publication_date'], errors='coerce')
    stale = int((dates.notna() & ((pd.Timestamp.now(tz=None)-dates).dt.days > stale_days)).sum())
    contradictions = int(((x['latitude'].notna() ^ x['longitude'].notna())).sum())
    missing = [c for c in REQUIRED if not bool(present[c].all())]
    return {"total": len(x), "with_id": int(present['object_id'].sum()), "with_coordinates": int((present['latitude'] & present['longitude']).sum()), "fill_rate": float(present.mean().mean()), "stale": stale, "contradictions": contradictions, "unverified": int((x['source'].isna() | x['source'].astype(str).eq('')).sum()), "missing_fields": missing}


def calculate(df, weights=None):
    x = normalize(df)
    weights = weights or DEFAULT_WEIGHTS
    rows=[]
    for _, r in x.iterrows():
        group_scores = {g: float(r[fields].notna().mean()) for g, fields in GROUPS.items()}
        completeness = sum(group_scores[g]*weights.get(g,0) for g in GROUPS)
        missing = [c for c in REQUIRED if pd.isna(r[c]) or str(r[c]).strip() in ('','<NA>')]
        stale = False
        if pd.notna(r['publication_date']):
            try: stale = (pd.Timestamp.now()-pd.to_datetime(r['publication_date'])).days > 730
            except Exception: pass
        actuality = 0.35 if stale else (0.7 if pd.isna(r['publication_date']) else 1.0)
        source_quality = 1.0 if r['source'] and str(r['source']).strip() else 0.0
        uncertainty = min(1.0, len(missing)/len(REQUIRED) + (0.15 if stale else 0) + (0.1 if not source_quality else 0))
        potential = None if completeness < .6 else round(100*(.55*completeness + .45*(1-uncertainty)),1)
        source_text = str(r['source'] or '')
        baseline_factor = 0.45 if 'ЕГРОКН' in source_text or 'egrkn' in source_text.lower() else 1.0
        index = round(100 * (0.45*completeness*baseline_factor + 0.2*actuality + 0.2*(1-uncertainty) + 0.15*source_quality), 1)
        rows.append({"object_id":r['object_id'], "name":r['name'], "assessment_index":index, "index_status":'достаточно данных' if index >= 70 else ('предварительно' if index >= 45 else 'недостаточно данных'), "baseline_factor":baseline_factor, "completeness":round(completeness*100,1), "actuality":round(actuality*100,1), "uncertainty":round(uncertainty*100,1), "potential":potential, "missing":', '.join(missing), **{f"score_{g}":round(v*100,1) for g,v in group_scores.items()}})
    return pd.DataFrame(rows)


def apply_updates(df, selected_id, updates):
    out=normalize(df)
    mask=out['object_id'].astype(str).eq(str(selected_id))
    for key, value in updates.items():
        if key in out.columns: out.loc[mask, key] = value
    return out


def priority_table(df, updates=None):
    before=calculate(df)
    after=calculate(apply_updates(df, updates[0], updates[1])) if updates else before
    if updates:
        merged=before[['object_id','name','assessment_index','completeness','potential']].rename(columns={'assessment_index':'before_index','completeness':'before_completeness','potential':'before_potential'}).merge(after[['object_id','assessment_index','completeness','potential']],on='object_id',suffixes=('','_after'))
        merged=merged.rename(columns={'assessment_index':'after_index','completeness':'after_completeness','potential':'after_potential'})
        merged['gain_completeness']=merged['after_completeness']-merged['before_completeness']
        merged['gain_index']=merged['after_index']-merged['before_index']
        return merged.sort_values(['gain_index','gain_completeness'],ascending=False)
    return before.sort_values(['completeness','uncertainty'],ascending=[True,False])
