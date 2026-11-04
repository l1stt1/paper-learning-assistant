"""Check the frozen executable with a new synthetic library and no usable model credentials."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler, Request
import fitz

import argparse
SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from backup import create_backup,restore_backup
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--exe',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
EXE=args.exe.resolve()
CHECK=args.output.resolve()
CHECK.mkdir(parents=True)
PROFILE=CHECK/'资料库 with spaces'
opener=build_opener(ProxyHandler({}))
def call(url,path,data=None,token=None,mime='application/json'):
    headers={}
    if data is not None:
        headers['Content-Type']=mime
        if not isinstance(data,bytes):data=json.dumps(data).encode()
    if token:headers['X-Learning-Token']=token
    with opener.open(Request(url+path,data=data,headers=headers),timeout=10) as res:
        value=res.read()
        return json.loads(value) if 'application/json' in res.headers.get('Content-Type','') else value
def start(profile):
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    env={k:v for k,v in os.environ.items() if k not in {'OPENAI_API_KEY'}}
    env['LEARNING_ASSISTANT_CODEX_CLI']='unavailable-synthetic-cli'
    env['PATH']=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32')
    for name in ['PYTHONHOME','PYTHONPATH','CONDA_PREFIX','SSL_CERT_FILE','SSL_CERT_DIR','OPENSSL_CONF','OPENSSL_MODULES']:
        env.pop(name,None)
    log=(CHECK/f'run-{port}.log').open('wb')
    p=subprocess.Popen([str(EXE),'--serve','--data-dir',str(profile),'--port',str(port)],stdout=log,stderr=log,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    url=f'http://127.0.0.1:{port}'
    for _ in range(100):
        try:
            health=call(url,'/health')
            return p,url,call(url,'/api/library')['token'],health
        except Exception:
            if p.poll() is not None:raise RuntimeError('Frozen process exited; inspect retained log')
            time.sleep(.2)
    p.terminate()
    p.wait(timeout=10)
    raise RuntimeError('Frozen startup timed out; inspect retained log')
def stop(p,url,token):
    call(url,'/api/shutdown',{},token)
    p.wait(timeout=15)
    assert p.returncode==0

p,url,token,health=start(PROFILE)
try:
    from app_version import VERSION
    assert health['version']==VERSION and Path(health['root'])==PROFILE
    lib=call(url,'/api/library');assert lib['articles']==[] and not lib['ai_available']
    assert b'<!doctype html>' in call(url,'/')
    assert len(call(url,'/vendor/pdfjs/build/pdf.mjs'))>10000
    with fitz.open() as pdf:
        page=pdf.new_page();page.insert_text((72,72),'Synthetic article: two blue circles are shown.')
        page.draw_circle((100,150),20,color=(0,0,1),fill=(0,0,1))
        page.draw_circle((180,150),20,color=(0,0,1),fill=(0,0,1))
        pdf.set_metadata({'title':'Synthetic circles'})
        data=pdf.tobytes()
    preview=call(url,'/api/import/preview',data,token,'application/pdf')
    article=call(url,'/api/import/commit',{'preview_id':preview['preview_id'],'title':'Synthetic circles'},token)['article']
    aid=article['id']
    assert call(url,'/api/pdf?id='+aid)==data
    assert call(url,'/api/page-image?id='+aid+'&page=1').startswith(b'\x89PNG')
    q=call(url,'/api/question',{'article_id':aid,'question':'How many circles?','source_scope':'article','reading_page':1,'generate':False},token)['question']
    assert q['status']=='saved'
finally:stop(p,url,token)
backup=CHECK/'full-library.zip'
summary=create_backup(PROFILE,backup)
restored=CHECK/'restored'
restore_backup(backup,restored)
p,url,token,health=start(restored)
try:
    state=call(url,'/api/article?id='+aid)
    assert state['state']['questions'][0]['question']=='How many circles?'
    assert call(url,'/api/pdf?id='+aid)==data
    status=call(url,'/api/model-settings');assert not status['available'] and '备份' in status['message']
finally:stop(p,url,token)
result={'version':health['version'],'empty_profile':True,'pdf_import_and_render':True,'question_saved':True,
        'restart_restore_record_and_pdf':True,'restored_model_requires_review':True,'api_calls':0,
        'backup_files':summary['file_count'],'exe_sha256':hashlib.sha256(EXE.read_bytes()).hexdigest()}
(CHECK/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result))
