from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, quote
import json
import sys
import pandas as pd
import requests
import re
import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from core import normalize, calculate, audit, priority_table, save_snapshot, fetch_mkrf_dataset, load_sources, collect_sources, filter_pskov_city, collect_object

SEED_PATH = ROOT / 'data/processed/enrichment_seed.json'
IMAGE_PATTERNS = {'pskov:278': 'Биржа*', 'pskov:279': 'Завод*', 'pskov:305': 'Здание Псковской теплоэлектростанции*'}

def load_enrichment_seed(object_id):
    try:
        payload = json.loads(SEED_PATH.read_text(encoding='utf-8'))
        return payload.get(str(object_id), [])
    except (OSError, ValueError):
        return []

def load_data():
    files = [ROOT / 'data/processed/egrkn_pskov_filtered.csv'] + sorted((ROOT / 'data/raw').glob('*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            frame = normalize(pd.read_csv(path))
            valid = frame['object_id'].astype(str).str.strip().ne('') & frame['name'].astype(str).str.strip().ne('')
            if len(frame) and valid.any():
                frame = filter_pskov_city(frame)
                raw_path = ROOT / 'data/raw/pskov_heritage_points.csv'
                if raw_path.exists():
                    extra = normalize(pd.read_csv(raw_path))
                    wanted = extra['name'].astype(str).str.contains('Биржа и контора|Завод лесопильный|теплоэлектростанции', case=False, regex=True, na=False)
                    frame = pd.concat([frame, filter_pskov_city(extra[wanted])], ignore_index=True).drop_duplicates('object_id')
                return frame
        except Exception: continue
    return normalize(pd.DataFrame())

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        body=json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        route=urlparse(self.path).path
        if route == '/api/enrich':
            params = parse_qs(urlparse(self.path).query)
            object_id = unquote(params.get('id',[''])[0]); group = params.get('group',['all'])[0]
            df = load_data(); row = df[df['object_id'].astype(str).eq(object_id)]
            if row.empty: self.send_json({'error':'Объект не найден'},404); return
            r = row.iloc[0]; fields = {'technical':['technical_condition','building_info','area','restoration_status','engineering_information'], 'legal':['protection_status','legal_restrictions','subject_of_protection','legal_document'], 'urban':['urban_context','land_info','transport_access','territorial_zone','permitted_use'], 'investment':['current_use','available_premises','owner_info','operating_status'], 'spatial':['latitude','longitude','address'], 'provenance':['source','publication_date','acquired_at']}
            selected = fields.get(group, [f for group_fields in fields.values() for f in group_fields])
            try:
                collected = collect_object(df, object_id, timeout=1)
                evidence = [item for item in collected['field_values'].fillna('').to_dict('records') if item.get('source_id') not in ('egrkn', 'rosreestr')]
            
            
            except Exception as exc: evidence = []; self.log_error(object_id, exc)
            seed = load_enrichment_seed(object_id)
            evidence.extend(seed)
            values=[{'field':f,'value':None if pd.isna(r.get(f)) else r.get(f),'present':pd.notna(r.get(f)) and bool(str(r.get(f)).strip()),'source':r.get('source') if pd.notna(r.get('source')) else 'не указан','verification':'официальный реестр' if 'ЕГРОКН' in str(r.get('source')) else 'требует проверки','evidence':[e for e in evidence if e.get('field_name') == f]} for f in dict.fromkeys(selected)]
            address=str(r.get('address') or ''); name=str(r.get('name') or '');
            query = requests.utils.quote(f'{name} {address}')
            links=[{'title':'OpenStreetMap','url':'https://www.openstreetmap.org/search?query='+query,'status':'дополнительный источник'}, {'title':'Яндекс.Карты','url':'https://yandex.ru/maps/?text='+query,'status':'дополнительный источник'}, {'title':'Яндекс.Картинки','url':'https://yandex.ru/images/search?text='+query,'status':'пользовательские фотографии'}, {'title':'Google Maps','url':'https://www.google.com/maps/search/'+query,'status':'дополнительный источник'}, {'title':'Поиск публикаций и объявлений','url':'https://yandex.ru/search/?text='+query,'status':'СМИ, сайты организаций и объявления'}, {'title':'Wikimedia Commons','url':'https://commons.wikimedia.org/w/index.php?search='+query+'&title=Special:MediaSearch&type=image','status':'фотографии пользователей'}]
            seed_field_map = {'historical_description': 'building_info', 'address_and_use': 'current_use', 'photo_search': 'photo_url'}
            updates = {seed_field_map[item['field_name']]: item['value'] for item in seed if item.get('field_name') in seed_field_map}
            updates.update({item['field_name']: item['value'] for item in seed if item.get('field_name') in selected})
            values=[]
            for field in dict.fromkeys(selected):
                matching = [item for item in evidence if item.get('field_name') == field or seed_field_map.get(item.get('field_name')) == field]
                value = updates.get(field, None if pd.isna(r.get(field)) else r.get(field))
                values.append({'field': field, 'value': value, 'present': bool(value) if value is not None else False, 'source': (matching[0].get('source_title') or matching[0].get('source_id')) if matching else (r.get('source') if pd.notna(r.get('source')) else 'source'), 'verification': 'additional source' if matching else 'requires verification', 'evidence': matching})
            updated = calculate(normalize(df).pipe(lambda frame: frame.assign(**{key: frame[key].where(~frame['object_id'].astype(str).eq(object_id), value) for key, value in updates.items()})))
            metric = updated[updated['object_id'].astype(str).eq(object_id)].fillna('').to_dict('records')
            self.send_json({'object_id':object_id,'name':name,'group':group,'values':values,'evidence':evidence,'updates':updates,'calculated':metric[0] if metric else {},'last_checked':pd.Timestamp.utcnow().isoformat(),'links':links,'note':'Дополнительные источники сохранены с уровнем доверия и требуют проверки.'}); return
        if route == '/api/photo':
            object_id = unquote(parse_qs(urlparse(self.path).query).get('id', [''])[0])
            df = load_data(); row = df[df['object_id'].astype(str).eq(object_id)]
            if row.empty: self.send_json({'found': False}); return
            address = str(row.iloc[0].get('address') or '')
            image_path = next(iter((ROOT / 'images_test').glob(IMAGE_PATTERNS.get(object_id, '__missing__'))), None)
            if image_path:
                self.send_json({'found': True, 'query': address, 'thumbnail': '/images_test/' + quote(image_path.name), 'source_url': '/images_test/' + quote(image_path.name), 'license': 'Локальное демонстрационное изображение'}); return
            search_text = address
            page_url = 'https://yandex.ru/images/search?text=' + requests.utils.quote(search_text)
            self.send_json({'found':False,'query':search_text,'source_url':page_url,'license':'Открыть выдачу и проверить права на изображение'}); return
        if route in ('/vendor/leaflet.js', '/vendor/leaflet.css'):
            path = Path(__file__).parent / 'vendor' / route.rsplit('/', 1)[-1]
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript' if route.endswith('.js') else 'text/css')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route.startswith('/images_test/'):
            image = ROOT / 'images_test' / unquote(route.removeprefix('/images_test/'))
            if image.exists() and image.is_file():
                body = image.read_bytes(); self.send_response(200); self.send_header('Content-Type', 'image/jpeg'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
            self.send_json({'error':'Изображение не найдено'},404); return
        if route == '/presentation.txt':
            path = ROOT / 'presentation.txt'
            if path.exists():
                body = path.read_bytes(); self.send_response(200); self.send_header('Content-Type', 'text/plain; charset=utf-8'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
            self.send_json({'error':'Файл не найден'},404); return
        if route == '/api/catalog':
            df=load_data(); self.send_json({'records':df.fillna('').to_dict('records'),'audit':audit(df),'calculated':calculate(df).fillna('').to_dict('records')}); return
        if route == '/api/sources':
            self.send_json({'sources': load_sources()}); return
        if route in ('/','/dashboard.html','/object.html'):
            path=ROOT/('object.html' if route == '/object.html' else 'dashboard.html'); body=path.read_bytes() if path.exists() else b'<h1>Dashboard not built</h1>'
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_json({'error':'Не найдено'},404)
    def do_POST(self):
        try:
            length=int(self.headers.get('Content-Length','0')); data=json.loads(self.rfile.read(length) or b'{}')
            if self.path == '/api/run':
                df=load_data(); selected=str(data.get('object_id','')); updates=data.get('updates',{}); result=priority_table(df,(selected,updates))
                self.send_json({'rows':result.fillna('').to_dict('records')}); return
            if self.path == '/api/refresh/egrkn':
                rows,name,total=fetch_mkrf_dataset(); save_snapshot(rows,prefix='egrkn_pskov'); self.send_json({'source_file':name,'total':total,'selected':len(rows)}); return
            if self.path == '/api/refresh/sources':
                rows, statuses = collect_sources()
                self.send_json({'records': len(rows), 'statuses': statuses, 'calculated': calculate(rows).fillna('').to_dict('records')}); return
            self.send_json({'error':'Не найдено'},404)
        except Exception as exc: self.send_json({'error':str(exc)},400)

    def log_error(self, object_id, exc):
        path = ROOT / 'data/logs/source_errors.log'; path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as log: log.write(f'{pd.Timestamp.utcnow().isoformat()} object={object_id}: {exc}\n')

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8765); args=parser.parse_args()
    print(f'ОКН: http://127.0.0.1:{args.port}/', flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
