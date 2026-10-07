from __future__ import annotations
import json, os, random, re, shutil, subprocess, sys, time, html
from pathlib import Path
from urllib.parse import quote
import requests
from PIL import Image
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.exceptions import RefreshError

ROOT=Path(__file__).resolve().parent
CFG=json.loads((ROOT/'config/config.json').read_text(encoding='utf-8'))
CAT=json.loads((ROOT/'config/topics_catalog.json').read_text(encoding='utf-8'))
STATE_PATH=ROOT/'state/history.json'
WORK=ROOT/'work'; OUT=ROOT/'output'
WORK.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)
UA='TitanHistoryAuto/2.0 (educational YouTube pipeline; GitHub Actions)'
SESSION=requests.Session(); SESSION.headers.update({'User-Agent':UA})

def log(*a): print(time.strftime('[%H:%M:%S]'),*a,flush=True)

def run(cmd, check=True, capture=False, input_text=None):
    log('RUN:', ' '.join(str(x) for x in cmd))
    return subprocess.run(cmd, check=check, text=True, input=input_text,
                          capture_output=capture)

def ffprobe_duration(path:Path)->float:
    r=run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture=True)
    return float(r.stdout.strip())

def load_state():
    try: return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except Exception: return {'used_topics':[],'uploads':[]}

def save_state(s):
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding='utf-8')

def get_wikipedia(title:str):
    url='https://en.wikipedia.org/w/api.php'
    params={'action':'query','format':'json','redirects':'1','prop':'extracts|info','inprop':'url','exintro':'1','explaintext':'1','titles':title}
    for n in range(3):
        try:
            r=SESSION.get(url,params=params,timeout=25); r.raise_for_status(); data=r.json()
            page=next(iter(data['query']['pages'].values()))
            extract=(page.get('extract') or '').strip()
            if page.get('missing') is not None or len(extract)<450: return None
            return {'title':page.get('title',title),'extract':extract,'url':page.get('fullurl') or f'https://en.wikipedia.org/wiki/{quote(title.replace(" ","_"))}'}
        except Exception as e:
            log('Wikipedia retry',n+1,e); time.sleep(3*(n+1))
    return None

def split_sentences(text):
    text=re.sub(r'\[[^\]]+\]','',text)
    text=re.sub(r'\s+',' ',text).strip()
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+',text) if 7 <= len(s.split()) <= 42]

def deterministic_script(source, minw, maxw):
    sentences=split_sentences(source['extract'])
    chosen=[]; total=0
    for s in sentences:
        w=len(s.split())
        if total+w>maxw: continue
        chosen.append(s); total+=w
        if total>=minw: break
    if total<minw: return None
    return ' '.join(chosen)

def ollama_rewrite(source, minw, maxw, model):
    if shutil.which('ollama') is None: return None
    prompt=f'''Rewrite ONLY the evidence below into a factual YouTube Shorts narration of {minw}-{maxw} words.
Rules: use no facts, dates, names, numbers, causes, motives, or claims that are absent from the evidence. No invented quotations. No speculation. Keep a strong first sentence, explain how the engineering worked, then end with a concise payoff. Plain narration only, no title, no bullets.
EVIDENCE:\n{source['extract']}'''
    try:
        r=run(['ollama','run',model],capture=True,input_text=prompt)
        text=re.sub(r'<think>.*?</think>','',r.stdout,flags=re.S).strip().strip('"')
        words=text.split()
        # conservative gates: length + every number in output must exist in evidence
        if not (minw <= len(words) <= maxw): return None
        srcnums=set(re.findall(r'\b\d[\d,.-]*\b',source['extract']))
        outnums=set(re.findall(r'\b\d[\d,.-]*\b',text))
        if not outnums.issubset(srcnums): return None
        return text
    except Exception as e:
        log('Ollama rewrite failed:',e); return None

def build_script(source):
    minw=int(CFG['target_words_min']); maxw=int(CFG['target_words_max'])
    if CFG.get('use_ollama',True):
        s=ollama_rewrite(source,minw,maxw,CFG.get('ollama_model','qwen3:1.7b'))
        if s:
            log('Script: local Ollama rewrite accepted.',len(s.split()),'words'); return s
    s=deterministic_script(source,minw,maxw)
    if s: log('Script: evidence-only fallback.',len(s.split()),'words')
    return s

def commons_search(query, limit=18):
    api='https://commons.wikimedia.org/w/api.php'
    try:
        r=SESSION.get(api,params={'action':'query','format':'json','list':'search','srnamespace':6,'srlimit':limit,'srsearch':query},timeout=25); r.raise_for_status()
        results=r.json().get('query',{}).get('search',[])
    except Exception as e:
        log('Commons search failed:',e); return []
    out=[]
    badwords=('map','diagram','plan','drawing','illustration','reconstruction','logo','icon','seal','coat of arms')
    for item in results:
        title=item['title']
        if any(b in title.lower() for b in badwords): continue
        try:
            rr=SESSION.get(api,params={'action':'query','format':'json','prop':'imageinfo','pageids':item['pageid'],'iiprop':'url|mime|extmetadata','iiurlwidth':1280},timeout=25); rr.raise_for_status()
            p=next(iter(rr.json()['query']['pages'].values())); ii=(p.get('imageinfo') or [{}])[0]
            mime=ii.get('mime','')
            if mime not in ('image/jpeg','image/png','image/webp'): continue
            meta=ii.get('extmetadata') or {}
            lic=(meta.get('LicenseShortName') or {}).get('value','')
            if not lic: continue
            url=ii.get('thumburl') or ii.get('url')
            if not url: continue
            out.append({'title':title,'url':url,'page_url':ii.get('descriptionurl',''),'license':html.unescape(re.sub('<[^>]+>','',lic)),
                        'artist':html.unescape(re.sub('<[^>]+>','',(meta.get('Artist') or {}).get('value','')))[:120]})
        except Exception:
            continue
        if len(out)>=8: break
    return out

def download_images(items):
    imgdir=WORK/'images'; shutil.rmtree(imgdir,ignore_errors=True); imgdir.mkdir(parents=True)
    good=[]
    for i,it in enumerate(items):
        try:
            r=SESSION.get(it['url'],timeout=30); r.raise_for_status()
            if len(r.content)>12_000_000: continue
            p=imgdir/f'{i:02d}.jpg'; p.write_bytes(r.content)
            with Image.open(p) as im:
                if im.width<500 or im.height<400: p.unlink(missing_ok=True); continue
                im.convert('RGB').save(p,'JPEG',quality=90)
            good.append((p,it))
        except Exception as e: log('Image skip:',e)
    return good

def tts(text, wav):
    model=ROOT/'models'/'piper'/'en_US-lessac-medium.onnx'
    if shutil.which('piper') and model.exists():
        try:
            run(['piper','--model',str(model),'--output_file',str(wav)],input_text=text)
            if wav.exists() and wav.stat().st_size>10000: return 'piper'
        except Exception as e: log('Piper failed:',e)
    if shutil.which('espeak-ng'):
        run(['espeak-ng','-s',str(CFG.get('speech_rate',155)),'-w',str(wav),text])
        if wav.exists() and wav.stat().st_size>10000: return 'espeak-ng'
    raise RuntimeError('No working local TTS engine.')

def make_srt(script,duration,path):
    sents=split_sentences(script) or [script]
    weights=[max(1,len(s.split())) for s in sents]; total=sum(weights); t=0.0; blocks=[]
    def fmt(x):
        ms=int(round(x*1000)); h=ms//3600000; ms%=3600000; m=ms//60000; ms%=60000; s=ms//1000; ms%=1000
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    for i,(s,w) in enumerate(zip(sents,weights),1):
        end=duration if i==len(sents) else t+duration*w/total
        blocks.append(f'{i}\n{fmt(t)} --> {fmt(end)}\n{s}\n'); t=end
    path.write_text('\n'.join(blocks),encoding='utf-8')

def make_video(images,wav,srt,out):
    duration=ffprobe_duration(wav)
    if not (35 <= duration <= 75): raise RuntimeError(f'Narration duration out of safe range: {duration:.1f}s')
    segdur=duration/len(images); listfile=WORK/'concat.txt'; segs=[]
    W=int(CFG['width']); H=int(CFG['height']); fps=int(CFG['fps'])
    for i,(img,_) in enumerate(images):
        seg=WORK/f'seg_{i:02d}.mp4'; frames=max(1,int(segdur*fps))
        vf=(f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"zoompan=z='min(zoom+0.00035,1.06)':d={frames}:s={W}x{H}:fps={fps},format=yuv420p")
        run(['ffmpeg','-y','-loop','1','-i',str(img),'-vf',vf,'-t',f'{segdur:.3f}','-an','-c:v','libx264','-preset','veryfast','-crf','24',str(seg)])
        segs.append(seg)
    listfile.write_text('\n'.join(f"file '{p.as_posix()}'" for p in segs),encoding='utf-8')
    silent=WORK/'silent.mp4'
    run(['ffmpeg','-y','-f','concat','-safe','0','-i',str(listfile),'-c','copy',str(silent)])
    # Copy SRT into work with simple filename to avoid filter escaping problems.
    local_srt=WORK/'captions.srt'; shutil.copy2(srt,local_srt)
    run(['ffmpeg','-y','-i',str(silent),'-i',str(wav),'-vf',
         "subtitles=work/captions.srt:force_style='FontName=Arial,FontSize=18,Outline=2,Shadow=1,Alignment=2,MarginV=85'",
         '-c:v','libx264','-preset','veryfast','-crf','23','-c:a','aac','-b:a','160k','-shortest','-movflags','+faststart',str(out)])
    return duration

def yt_upload(video,title,description):
    token=ROOT/'config'/'youtube_token.json'
    if not token.exists(): raise RuntimeError('Missing config/youtube_token.json generated from GitHub Secret.')
    creds=Credentials.from_authorized_user_file(str(token),['https://www.googleapis.com/auth/youtube.upload'])
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest()); token.write_text(creds.to_json(),encoding='utf-8')
    except RefreshError as e:
        raise RuntimeError('YOUTUBE OAUTH EXPIRED/REVOKED. Run AUTH_LOCAL_WINDOWS.bat again and replace GitHub Secret YOUTUBE_TOKEN_JSON.') from e
    youtube=build('youtube','v3',credentials=creds,cache_discovery=False)
    body={'snippet':{'title':title[:100],'description':description[:5000],'categoryId':'27','tags':['history','ancient history','engineering','shorts']},
          'status':{'privacyStatus':CFG.get('youtube_privacy','public'),'selfDeclaredMadeForKids':False}}
    media=MediaFileUpload(str(video),chunksize=8*1024*1024,resumable=True,mimetype='video/mp4')
    req=youtube.videos().insert(part='snippet,status',body=body,media_body=media)
    resp=None
    while resp is None:
        status,resp=req.next_chunk()
        if status: log(f'Upload {int(status.progress()*100)}%')
    return resp['id']

def description(source, script, images):
    lines=[f"Source used for factual narration: {source['url']}","","Visuals: Wikimedia Commons files used under their listed licenses:"]
    for _,it in images:
        label=it['title'].replace('File:','')
        lines.append(f"- {label} | {it['license']} | {it['page_url']}")
    lines += ["","Narration is based only on the cited source extract; visuals are real Wikimedia Commons media.","","#Shorts #History #AncientEngineering"]
    return '\n'.join(lines)

def main():
    state=load_state(); used=set(state.get('used_topics',[]))
    pool=[x for x in CAT if x['title'] not in used]
    if not pool:
        log('All catalog topics used. Starting a new cycle.'); used.clear(); state['used_topics']=[]; pool=CAT[:]
    random.shuffle(pool)
    attempts=int(CFG.get('max_topic_attempts',8))
    for topic in pool[:attempts]:
        log('\n=== TOPIC:',topic['title'],'===')
        shutil.rmtree(WORK,ignore_errors=True); WORK.mkdir()
        source=get_wikipedia(topic['title'])
        if not source: log('SKIP: weak/missing source.'); continue
        script=build_script(source)
        if not script: log('SKIP: script evidence/length gate.'); continue
        media=commons_search(topic['commons_query'])
        if len(media)<int(CFG.get('min_commons_images',5)): log('SKIP: not enough Commons results.'); continue
        images=download_images(media)
        if len(images)<int(CFG.get('min_commons_images',5)): log('SKIP: not enough usable real images.'); continue
        wav=WORK/'narration.wav'; engine=tts(script,wav); dur=ffprobe_duration(wav); log('TTS:',engine,f'{dur:.1f}s')
        srt=WORK/'captions.srt'; make_srt(script,dur,srt)
        safe=re.sub(r'[^A-Za-z0-9_-]+','_',source['title'])[:60]; video=OUT/f'{safe}.mp4'
        make_video(images,wav,srt,video)
        title=f"{source['title']}: Ancient Engineering in 60 Seconds #Shorts"
        desc=description(source,script,images)
        vid=yt_upload(video,title,desc)
        log('UPLOAD SUCCESS:',f'https://youtu.be/{vid}')
        state.setdefault('used_topics',[]).append(topic['title'])
        state.setdefault('uploads',[]).append({'topic':topic['title'],'youtube_id':vid,'title':title,'time_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())})
        save_state(state)
        shutil.rmtree(WORK,ignore_errors=True)
        try: video.unlink()
        except Exception: pass
        return 0
    log('NO VIDEO UPLOADED: every attempted topic failed a conservative gate.')
    return 3

if __name__=='__main__':
    raise SystemExit(main())
