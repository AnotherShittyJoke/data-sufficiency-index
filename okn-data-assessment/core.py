import json
from pathlib import Path
import pandas as pd
import requests
import re
import io
import zipfile
from datetime import datetime, timezone
from math import radians, sin, cos, asin, sqrt
import uuid
import logging

REQUIRED = ["object_id", "name", "historical_name", "address", "region", "city", "latitude", "longitude", "object_type", "heritage_category", "dating", "protection_status", "area", "building_info", "land_info", "technical_condition", "subject_of_protection", "legal_restrictions", "current_use", "urban_context", "transport_access", "available_premises", "owner_info", "source", "publication_date", "acquired_at", "last_verified_at", "record_status", "duplicate_group"]
OPTIONAL = ["photo_url", "verification_status", "source_date", "style_or_function", "function", "photo_search_url", "restoration_status", "restoration_date", "emergency_status", "conservation_required", "ownership_type", "vacancy_status", "distance_to_nearest_stop", "distance_to_main_road", "distance_to_city_center", "count_social_objects_500m", "count_commercial_objects_500m", "count_tourism_objects_1000m", "engineering_information", "recent_repair", "recent_restoration", "territorial_zone", "permitted_use", "functional_zone", "heritage_zone_overlap", "protection_zone_overlap", "surrounding_land_use", "engineering_access", "urban_constraints", "land_use_mix_500m", "investment_project", "auction_status", "recent_use_evidence", "operating_status", "land_availability", "infrastructure_access", "tourism_access", "legal_document", "legal_document_date", "effective_date", "territory_boundary", "protection_zone", "restoration_evidence", "restoration_confidence", "last_update"]
GROUPS = {"technical": ["technical_condition", "area", "building_info", "restoration_status"], "legal": ["protection_status", "heritage_category", "subject_of_protection", "legal_restrictions"], "urban": ["urban_context", "transport_access", "land_info"], "investment": ["current_use", "available_premises", "owner_info"]}
DEFAULT_WEIGHTS = {"technical": .25, "legal": .25, "urban": .20, "investment": .15, "provenance": .15}
RESTORATION_STATUSES = ("restored_and_operating", "restored_but_use_unknown", "partially_restored", "under_restoration", "not_restored", "vacant", "conserved", "emergency", "unknown")
FUNCTION_PROFILES = {"музей": ("heritage_category", "subject_of_protection", "technical_condition", "area", "current_use", "tourism_access"), "выставочное пространство": ("subject_of_protection", "area", "technical_condition", "current_use", "tourism_access"), "культурный центр": ("current_use", "area", "technical_condition", "infrastructure_access", "legal_restrictions"), "туристический объект": ("heritage_category", "technical_condition", "tourism_access", "current_use", "transport_access"), "гостиница или хостел": ("area", "engineering_information", "transport_access", "legal_restrictions", "current_use"), "ресторан или кафе": ("area", "engineering_information", "current_use", "legal_restrictions", "infrastructure_access"), "офис": ("area", "current_use", "engineering_information", "legal_restrictions"), "смешанное использование": ("area", "technical_condition", "current_use", "legal_restrictions", "infrastructure_access")}
FRESHNESS_DAYS = {"technical": 730, "legal": 365, "urban": 730, "investment": 180, "coordinates": 1095}

def filter_pskov_city(df):
    """Keep the city of Pskov, leaving ambiguous address records for review."""
    x = normalize(df)
    city = x['city'].astype(str).str.casefold()
    address = x['address'].astype(str).str.casefold()
    explicit_city = city.str.contains('псков', na=False)
    address_city = address.str.contains(r'г\.?\s*псков|город\s+псков', regex=True, na=False)
    return exclude_requested_categories(x[explicit_city | address_city].copy())

EXCLUDED_CATEGORY_RE = re.compile(r"археолог|культов|церкв|храм|монастыр|часовн|собор|мечет|синагог|капелл|мемориал|братск.*(?:могил|кладбищ)|могил|захорон|воин|войн", re.I)

def exclude_requested_categories(df):
    """Remove archaeology, religious and memorial objects from the working catalogue."""
    if df.empty:
        return df.copy()
    object_type = df['object_type'] if 'object_type' in df.columns else pd.Series('', index=df.index)
    name = df['name'] if 'name' in df.columns else pd.Series('', index=df.index)
    text = object_type.fillna('').astype(str) + ' ' + name.fillna('').astype(str)
    return df[~text.str.contains(EXCLUDED_CATEGORY_RE, na=False)].copy()

SOURCE_REGISTRY = [
    {"source_id": "egrkn", "title": "ЕГРОКН Минкультуры России", "url": "https://opendata.mkrf.ru/opendata/7705851331-egrkn/", "source_type": "registry", "authority_level": "official"},
    {"source_id": "pskov_museum", "title": "Комитет по охране объектов культурного наследия Псковской области", "url": "https://okn.pskov.ru/", "source_type": "regional_authority", "authority_level": "official"},
    {"source_id": "pskov_open_data", "title": "Открытые данные Псковской области", "url": "https://pskov.ru/opendata", "source_type": "open_data", "authority_level": "official"},
    {"source_id": "osm", "title": "OpenStreetMap / Overpass", "url": "https://www.openstreetmap.org/", "source_type": "geodata", "authority_level": "community"},
    {"source_id": "zakupki", "title": "Единая информационная система закупок", "url": "https://zakupki.gov.ru/", "source_type": "procurement", "authority_level": "official"},
    {"source_id": "torgi", "title": "ГИС Торги", "url": "https://torgi.gov.ru/", "source_type": "auction", "authority_level": "official"},
    {"source_id": "publication", "title": "Официальное опубликование правовых актов", "url": "http://publication.pravo.gov.ru/", "source_type": "legal", "authority_level": "official"},
]

for _source in SOURCE_REGISTRY:
    _source.setdefault("source_level", "official" if _source["authority_level"] == "official" else "semi_official")
    _source.setdefault("coverage", "Псков и объекты культурного наследия")
    _source.setdefault("limitations", "Автоматический результат требует проверки")

def load_sources(path='sources.csv'):
    target = Path(path)
    if target.exists():
        try:
            existing = pd.read_csv(target).fillna('').to_dict('records')
            known = {str(row.get('source_id')) for row in existing}
            return existing + [row for row in SOURCE_REGISTRY if row['source_id'] not in known]
        except Exception: pass
    return SOURCE_REGISTRY

def collect_sources(sources=None, timeout=30, save=True):
    """Fetch every registered source and return normalized records plus statuses."""
    sources = sources or load_sources()
    results, frames = [], []
    log_path = Path('data/logs/source_errors.log')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for source in sources:
        source_id = str(source.get('source_id') or source.get('title') or 'unknown')
        url = str(source.get('url') or '')
        if source_id in ('egrkn', 'rosreestr'):
            try:
                frame, file_name, total = fetch_mkrf_dataset(timeout=timeout)
                if save and not frame.empty: save_snapshot(frame, prefix='egrkn_pskov')
                frames.append(frame)
                results.append({'source_id': source_id, 'status': 'ok', 'records': len(frame), 'source_file': file_name, 'total': total})
            except Exception as exc:
                with log_path.open('a', encoding='utf-8') as log: log.write(f'{datetime.now(timezone.utc).isoformat()} {source_id}: {exc}\n')
                results.append({'source_id': source_id, 'status': 'error', 'records': 0, 'error': str(exc)})
            continue
        if source_id == 'osm':
            try:
                frame = fetch_overpass(timeout=timeout)
                if save and not frame.empty: save_snapshot(frame, prefix='overpass')
                frames.append(frame)
                results.append({'source_id': source_id, 'status': 'ok', 'records': len(frame)})
            except Exception as exc:
                with log_path.open('a', encoding='utf-8') as log: log.write(f'{datetime.now(timezone.utc).isoformat()} {source_id}: {exc}\n')
                results.append({'source_id': source_id, 'status': 'error', 'records': 0, 'error': str(exc)})
            continue
        if not url.startswith(('http://', 'https://')):
            results.append({'source_id': source_id, 'status': 'skipped', 'records': 0, 'error': 'URL не указан'})
            continue
        try:
            response = requests.get(url, timeout=timeout, headers={'User-Agent': 'okn-data-assessment/1.0'})
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').lower()
            frame = None
            if 'json' in content_type or url.lower().endswith('.json'):
                payload = response.json()
                frame = pd.DataFrame(payload if isinstance(payload, list) else payload.get('records', payload))
            elif 'csv' in content_type or url.lower().endswith('.csv'):
                frame = pd.read_csv(io.BytesIO(response.content))
            if frame is None:
                results.append({'source_id': source_id, 'status': 'metadata_only', 'records': 0, 'http_status': response.status_code})
                continue
            frame = normalize(frame)
            frame['source'] = source_id
            if save and not frame.empty: save_snapshot(frame, prefix=source_id.replace(' ', '_')[:40])
            frames.append(frame)
            results.append({'source_id': source_id, 'status': 'ok', 'records': len(frame), 'http_status': response.status_code})
        except Exception as exc:
            message = f'{datetime.now(timezone.utc).isoformat()} {source_id}: {exc}'
            with log_path.open('a', encoding='utf-8') as log: log.write(message + '\n')
            results.append({'source_id': source_id, 'status': 'error', 'records': 0, 'error': str(exc)})
    combined = normalize(pd.concat(frames, ignore_index=True)) if frames else empty_frame()
    if not combined.empty: combined = combined.drop_duplicates('object_id', keep='first')
    return combined, results


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

def _filled(value):
    return pd.notna(value) and str(value).strip() not in ('', '<NA>', 'nan', 'None')

def _distance_m(lat1, lon1, lat2, lon2):
    if not all(_filled(v) for v in (lat1, lon1, lat2, lon2)): return None
    p1, p2 = radians(float(lat1)), radians(float(lat2))
    dp, dl = radians(float(lat2)-float(lat1)), radians(float(lon2)-float(lon1))
    a = sin(dp/2)**2 + cos(p1)*cos(p2)*sin(dl/2)**2
    return 6371000 * 2 * asin(sqrt(a))

def match_records(left, right, threshold=50):
    """Return candidate pairs scored using registry id, address, name and coordinates."""
    a, b = normalize(left), normalize(right)
    rows = []
    for li, l in a.iterrows():
        best = None
        for ri, r in b.iterrows():
            score = 0
            if _filled(l.object_id) and str(l.object_id) == str(r.object_id): score += 100
            if _filled(l.address) and _filled(r.address) and str(l.address).casefold() == str(r.address).casefold(): score += 35
            if _filled(l.name) and _filled(r.name) and str(l.name).casefold() == str(r.name).casefold(): score += 35
            distance = _distance_m(l.latitude, l.longitude, r.latitude, r.longitude)
            if distance is not None: score += 30 if distance <= 50 else 15 if distance <= 200 else 0
            if best is None or score > best[0]: best = (score, ri, distance)
        if best is None: rows.append({'left_index': li, 'right_index': None, 'score': 0, 'status': 'unmatched'})
        else:
            score, ri, distance = best
            status = 'matched' if score >= threshold + 50 else 'probable_match' if score >= 70 else 'possible_match' if score >= 35 else 'unmatched'
            if status == 'possible_match': status = 'requires_review'
            rows.append({'left_index': li, 'right_index': ri, 'score': score, 'distance_m': distance, 'status': status})
    return pd.DataFrame(rows)

def compare_snapshots(old_df, new_df):
    old, new = normalize(old_df), normalize(new_df)
    old_ids, new_ids = set(old.object_id.dropna().astype(str)), set(new.object_id.dropna().astype(str))
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for oid in sorted(new_ids - old_ids): rows.append({'change_id': str(uuid.uuid4()), 'object_id': oid, 'field_name': '', 'old_value': '', 'new_value': 'object added', 'change_type': 'object_added', 'detected_at': now, 'status': 'pending'})
    for oid in sorted(old_ids - new_ids): rows.append({'change_id': str(uuid.uuid4()), 'object_id': oid, 'field_name': '', 'old_value': 'object present', 'new_value': '', 'change_type': 'object_removed', 'detected_at': now, 'status': 'pending'})
    common = old_ids & new_ids
    for oid in common:
        before, after = old[old.object_id.astype(str) == oid].iloc[0], new[new.object_id.astype(str) == oid].iloc[0]
        for field in REQUIRED:
            ov, nv = '' if pd.isna(before[field]) else str(before[field]), '' if pd.isna(after[field]) else str(after[field])
            if ov != nv: rows.append({'change_id': str(uuid.uuid4()), 'object_id': oid, 'field_name': field, 'old_value': ov, 'new_value': nv, 'change_type': 'value_removed' if not nv else 'new_value' if not ov else 'value_changed', 'detected_at': now, 'status': 'pending'})
    return pd.DataFrame(rows, columns=['change_id','object_id','field_name','old_value','new_value','change_type','source_id','detected_at','confirmed_at','confirmed_by','status'])

def save_change_log(changes, path='data/processed/change_log.csv'):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    changes.to_csv(target, index=False)
    return target

def save_field_values(values, path='data/processed/field_values.csv'):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    values.to_csv(target, index=False)
    return target


def fetch_overpass(bbox=(57.75, 28.15, 57.90, 28.35), endpoint='https://overpass-api.de/api/interpreter', timeout=60):
    south, west, north, east = bbox
    query = f'''[out:json][timeout:50];(nwr["historic"="monument"]({south},{west},{north},{east});nwr["heritage"]({south},{west},{north},{east}););out center tags;'''
    response = requests.post(endpoint, data=query, timeout=timeout, headers={'User-Agent': 'okn-data-assessment/1.0', 'Accept': 'application/json'})
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
    rows = []
    for _, r in x.iterrows():
        present = {c: _filled(r[c]) for c in x.columns}
        scores = {g: sum(present.get(f, False) for f in fields) / len(fields) for g, fields in GROUPS.items()}
        completeness = sum(scores[g] * weights.get(g, 0) for g in scores)
        missing = [c for c in ("technical_condition", "area", "building_info", "restoration_status") if not present.get(c, False)]
        date = pd.to_datetime(r["last_verified_at"] if present["last_verified_at"] else r["publication_date"], errors="coerce")
        age = (pd.Timestamp.now() - date.tz_localize(None)).days if pd.notna(date) else None
        actuality = 0.0 if age is None else max(0.0, min(1.0, 1.0 - age / 730))
        source_quality = 1.0 if present["source"] else 0.0
        source_text = str(r["source"]).casefold()
        if source_quality: source_quality = 1.0 if any(k in source_text for k in ("егрокн", "официаль", "мкрф", "комитет")) else .5
        consistency = 0.0 if (present["latitude"] ^ present["longitude"]) else 1.0
        overall = 100 * (0.25*scores["technical"] + 0.25*scores["legal"] + 0.20*scores["urban"] + 0.15*scores["investment"] + 0.15*source_quality)
        overall *= (0.65 + 0.35*actuality) * consistency
        status = "high_quality" if overall >= 80 and not missing else "sufficient_for_preliminary_assessment" if overall >= 60 else "requires_additional_data" if overall >= 35 else "insufficient_data"
        threats, opportunities = [], []
        if not present["technical_condition"]: threats.append("нет технического состояния")
        if age is None or age > 730: threats.append("устарели сведения")
        if not present["latitude"] or not present["longitude"]: threats.append("нет координат")
        if present["emergency_status"] and str(r["emergency_status"]).casefold() in ("аварийное", "аварийный", "да"): threats.append("аварийный статус")
        if present["legal_restrictions"]: threats.append("есть правовые ограничения")
        if present["available_premises"]: opportunities.append("есть доступные помещения")
        if present["current_use"] and str(r["current_use"]).casefold() in ("не используется", "свободно", "не эксплуатируется"): opportunities.append("возможна новая функция")
        if scores["urban"] >= .5: opportunities.append("достаточные градостроительные сведения")
        restoration = str(r.get("restoration_status")) if present.get("restoration_status") else "unknown"
        if restoration not in RESTORATION_STATUSES: restoration = "unknown"
        use = str(r.get("current_use")) if present.get("current_use") else ""
        use = use.casefold()
        if restoration == "unknown" and use and use not in ("не используется", "свободно", "не эксплуатируется"): restoration = "restored_but_use_unknown"
        potentials = {}
        for function, fields in FUNCTION_PROFILES.items():
            score = sum(present.get(field, False) for field in fields) / len(fields)
            potentials[function] = round(max(0, min(100, score * 100 * (.7 + .3 * actuality) * consistency)), 1)
        rows.append({"object_id": r["object_id"], "name": r["name"], "assessment_index": round(overall, 1), "overall_data_index": round(overall, 1), "index_status": status, "technical_score": round(scores["technical"]*100, 1), "legal_score": round(scores["legal"]*100, 1), "urban_score": round(scores["urban"]*100, 1), "investment_score": round(scores["investment"]*100, 1), "completeness_score": round(completeness*100, 1), "actuality_score": round(actuality*100, 1), "consistency_score": round(consistency*100, 1), "source_quality_score": round(source_quality*100, 1), "completeness": round(completeness*100, 1), "actuality": round(actuality*100, 1), "uncertainty": round((1-completeness)*100, 1), "potential": round(overall, 1) if not missing else None, "function_potential": potentials, "restoration_status": restoration, "missing": ", ".join(missing), "threats": threats, "opportunities": opportunities})
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


def collect_object(df, object_id, radius=0.002, timeout=30):
    """Collect the available source snapshots for one object and return values with provenance."""
    base = normalize(df)
    selected = base[base['object_id'].astype(str).eq(str(object_id))]
    if selected.empty:
        raise ValueError('object_id not found')
    row = selected.iloc[0]
    records = []
    for field, value in row.items():
        if field == 'object_id' or not _filled(value):
            continue
        records.append({'value_id': f"{object_id}:{field}:egrkn", 'object_id': object_id, 'field_name': field, 'value': value, 'source_id': 'egrkn', 'source_url': row.get('photo_url', '') or SOURCE_REGISTRY[0]['url'], 'source_title': 'ЕГРОКН Минкультуры России', 'document_title': 'ЕГРОКН', 'document_date': row.get('publication_date', ''), 'acquired_at': datetime.now(timezone.utc).isoformat(), 'extracted_text': f'{field}: {value}', 'verification_status': 'official_registry', 'confidence': 1.0, 'is_current': True, 'comment': 'Базовое значение ЕГРОКН'})
    osm = pd.DataFrame()
    try:
        lat, lon = float(row['latitude']), float(row['longitude'])
        osm = pd.DataFrame()
        last_error = None
        endpoints = ('https://overpass-api.de/api/interpreter',) if timeout <= 2 else ('https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter')
        for endpoint in endpoints:
            try:
                osm = fetch_overpass((lat-radius, lon-radius, lat+radius, lon+radius), endpoint=endpoint, timeout=timeout)
                break
            except Exception as exc:
                last_error = exc
        if osm.empty and last_error:
            raise last_error
        for _, found in osm.iterrows():
            for field in ('name','address','latitude','longitude','object_type'):
                if _filled(found.get(field)):
                    records.append({'value_id': f"{object_id}:{field}:osm:{found['object_id']}", 'object_id': object_id, 'field_name': field, 'value': found[field], 'source_id': 'osm', 'source_url': 'https://www.openstreetmap.org/', 'source_title': 'OpenStreetMap / Overpass', 'document_title': 'OpenStreetMap / Overpass', 'document_date': '', 'acquired_at': datetime.now(timezone.utc).isoformat(), 'extracted_text': f'{field}: {found[field]}', 'verification_status': 'auxiliary', 'confidence': 0.5, 'is_current': True, 'comment': 'Вспомогательное пространственное значение'})
    except Exception as exc:
        Path('data/logs').mkdir(parents=True, exist_ok=True)
        with (Path('data/logs/source_errors.log')).open('a', encoding='utf-8') as log: log.write(f'{datetime.now(timezone.utc).isoformat()} object={object_id} osm: {exc}\n')
    values = pd.DataFrame(records)
    save_field_values(values)
    return {'object': row.to_dict(), 'field_values': values, 'osm_records': len(osm)}
