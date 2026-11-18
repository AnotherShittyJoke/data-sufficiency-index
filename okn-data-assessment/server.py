from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import json
import sys
import pandas as pd
import requests
import re
import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from core import normalize, calculate, audit, priority_table, save_snapshot, fetch_mkrf_dataset

def load_data():
    files = [ROOT / 'data/processed/egrkn_pskov_filtered.csv'] + sorted((ROOT / 'data/raw').glob('*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            frame = normalize(pd.read_csv(path))
            valid = frame['object_id'].astype(str).str.strip().ne('') & frame['name'].astype(str).str.strip().ne('')
            if len(frame) and valid.any(): return frame
        except Exception: continue
    return normalize(pd.DataFrame())

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        body=json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        route=urlparse(self.path).path
        if route == '/api/enrich':
            query = urlparse(self.path).query
            params = dict(item.split('=', 1) for item in query.split('&') if '=' in item)
            object_id = params.get('id',''); group = params.get('group','all')
            df = load_data(); row = df[df['object_id'].astype(str).eq(object_id)]
            if row.empty: self.send_json({'error':'Объект не найден'},404); return
            r = row.iloc[0]; fields = {'technical':['technical_condition','building_info','area','subject_of_protection'], 'legal':['protection_status','legal_restrictions','subject_of_protection'], 'urban':['urban_context','land_info','transport_access'], 'investment':['current_use','available_premises','owner_info'], 'spatial':['latitude','longitude','address'], 'provenance':['source','publication_date','acquired_at']}
            selected = fields.get(group, [f for group_fields in fields.values() for f in group_fields]); values=[{'field':f,'value':None if pd.isna(r.get(f)) else r.get(f),'present':pd.notna(r.get(f)) and bool(str(r.get(f)).strip()),'source':r.get('source') if pd.notna(r.get('source')) else 'не указан','verification':'официальный реестр' if 'ЕГРОКН' in str(r.get('source')) else 'требует проверки'} for f in dict.fromkeys(selected)]
            address=str(r.get('address') or ''); name=str(r.get('name') or '');
            links=[{'title':'ЕГРОКН Минкультуры','url':str(r.get('photo_url') or 'https://opendata.mkrf.ru/opendata/7705851331-egrkn/'),'status':'официальный источник'}, {'title':'Яндекс.Картинки по адресу','url':'https://yandex.ru/images/search?text='+requests.utils.quote(address),'status':'поиск изображения'}, {'title':'OpenStreetMap','url':'https://www.openstreetmap.org/search?query='+requests.utils.quote(address),'status':'вспомогательный источник'}, {'title':'Открытые данные Псковской области','url':'https://pskov.ru/opendata','status':'региональный источник'}]
            self.send_json({'object_id':object_id,'name':name,'group':group,'values':values,'links':links,'note':'Найденные сведения требуют проверки перед включением в индекс.'}); return
        if route == '/api/photo':
            object_id = urlparse(self.path).query.replace('id=', '', 1)
            df = load_data(); row = df[df['object_id'].astype(str).eq(object_id)]
            if row.empty: self.send_json({'found': False}); return
            address = str(row.iloc[0].get('address') or '')
            search_text = address
            page_url = 'https://yandex.ru/images/search'
            page = requests.get(page_url, params={'text':search_text}, headers={'User-Agent':'Mozilla/5.0'}, timeout=20)
            text = html.unescape(page.text)
            origin = re.search(r'"origin"\s*:\s*\{[^{}]*"url"\s*:\s*"(https?://[^"\\]+)', text)
            if not origin: self.send_json({'found':False,'query':search_text,'source_url':page.url}); return
            image_url = origin.group(1).replace('\\/', '/')
            self.send_json({'found':True,'query':search_text,'thumbnail':image_url,'source_url':page.url,'license':'Проверить права на странице источника'}); return
        if route in ('/vendor/leaflet.js', '/vendor/leaflet.css'):
            path = Path(__file__).parent / 'vendor' / route.rsplit('/', 1)[-1]
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript' if route.endswith('.js') else 'text/css')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route == '/api/catalog':
            df=load_data(); self.send_json({'records':df.fillna('').to_dict('records'),'audit':audit(df),'calculated':calculate(df).fillna('').to_dict('records')}); return
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
            self.send_json({'error':'Не найдено'},404)
        except Exception as exc: self.send_json({'error':str(exc)},400)

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8765); args=parser.parse_args()
    print(f'ОКН: http://127.0.0.1:{args.port}/', flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
