from pathlib import Path
import sys
import streamlit as st
sys.path.insert(0, str(Path(__file__).parent))
from core import *

st.set_page_config(page_title='ОКН Пскова', layout='wide')
st.title('ОКН Пскова — оценка достаточности данных')
st.caption('Предварительная оценка. Не заменяет техническое обследование, правовую экспертизу или градостроительное обоснование.')
if 'df' not in st.session_state: st.session_state.df = empty_frame()
if 'baseline' not in st.session_state: st.session_state.baseline = None
if 'source_log' not in st.session_state: st.session_state.source_log = []
if st.sidebar.button('Загрузить ЕГРОКН и выбрать Псков'):
    try:
        official, file_name, total = fetch_mkrf_dataset()
        st.session_state.df = official
        save_snapshot(official, prefix='egrkn_pskov')
        st.success(f'ЕГРОКН: выбрано {len(official)} из {total}; файл {file_name}')
    except Exception as exc:
        st.error(f'Ошибка ЕГРОКН: {exc}')
if st.sidebar.button('Загрузить данные из OpenStreetMap'):
    try:
        osm = fetch_overpass()
        st.session_state.df = normalize(pd.concat([st.session_state.df, osm], ignore_index=True).drop_duplicates('object_id'))
        save_snapshot(osm, prefix='overpass')
        st.session_state.source_log.append({'source': 'OpenStreetMap / Overpass', 'rows': len(osm), 'status': 'получено'})
        st.success(f'Получено объектов OSM: {len(osm)}')
    except Exception as exc:
        st.error(f'Ошибка Overpass: {exc}')
with st.sidebar:
    st.header('Данные')
    file = st.file_uploader('Загрузить CSV, JSON или Excel', type=['csv','json','xlsx','xls'])
    if file and st.button('Импортировать'):
        try: st.session_state.df=normalize(load_file(file)); st.session_state.baseline=None; st.success(f'Загружено объектов: {len(st.session_state.df)}')
        except Exception as e: st.error(str(e))
    if st.button('Обновить данные'): st.info('Сетевой сбор отключён. Загрузите локальный снимок данных.')
    if st.button('Сбросить сценарий'): st.session_state.baseline=None; st.rerun()
    st.download_button('Экспорт CSV', st.session_state.df.to_csv(index=False).encode('utf-8-sig'), 'okn.csv')
    st.download_button('Экспорт JSON', st.session_state.df.to_json(orient='records', force_ascii=False, indent=2).encode(), 'okn.json')
df=st.session_state.df; audit_result=audit(df); calc=calculate(df)
st.info('Пустые поля сохраняются как пропуски. Автоматически выдуманные значения не создаются.')
cols=st.columns(5)
for col,label,val in zip(cols,['Объектов','С координатами','Заполненность','Устаревшие','Противоречия'],[audit_result['total'],audit_result['with_coordinates'],f"{audit_result['fill_rate']:.1%}",audit_result['stale'],audit_result['contradictions']]): col.metric(label,val)
query=st.text_input('Поиск')
view=df if not query else df[df.astype(str).apply(lambda s:s.str.contains(query,case=False,na=False)).any(axis=1)]
st.subheader('Реестр объектов'); st.dataframe(view,use_container_width=True,hide_index=True)
if not df.empty:
    selected=st.selectbox('Выбрать объект',df.object_id.astype(str).tolist()); obj=df[df.object_id.astype(str)==selected].iloc[0]
    st.subheader(f"Карточка: {obj.get('name') or selected}"); st.json(obj.dropna().to_dict())
    st.subheader('Показатели'); st.dataframe(calc[calc.object_id.astype(str)==selected],use_container_width=True,hide_index=True)
    st.subheader('Сценарий актуализации')
    works=st.multiselect('Работы',['Технические характеристики','Правовой статус','Градостроительное окружение','Координаты','Современное использование','Источник'])
    fm={'Технические характеристики':'technical_condition','Правовой статус':'protection_status','Градостроительное окружение':'urban_context','Координаты':'latitude','Современное использование':'current_use','Источник':'source'}
    updates={fm[w]:st.text_input(f'Новое значение: {fm[w]}',key=w) for w in works}
    if st.button('Рассчитать сценарий'): st.session_state.baseline=(selected,updates)
    if st.session_state.baseline: st.dataframe(priority_table(df,st.session_state.baseline),use_container_width=True,hide_index=True)
else: st.warning('Данные не загружены. Загрузите локальный снимок реестра.')
st.subheader('Аудит'); st.write('Неполные поля: '+(', '.join(audit_result['missing_fields']) or 'нет')); st.write('Без источника: '+str(audit_result['unverified']))
