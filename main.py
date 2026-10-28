"""AI-офис для YouTube Shorts: сценарист -> (вы) -> монтажёр/публикатор -> (вы) -> аналитик -> сценарист."""
import asyncio, glob, json, os, random, re, sqlite3, subprocess, sys, threading, time
from contextlib import asynccontextmanager
from pathlib import Path

import edge_tts
import uvicorn
from anthropic import Anthropic
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Работа и из исходников, и из собранного .exe: данные лежат рядом с программой
if getattr(sys, "frozen", False):
    BASE = Path(sys._MEIPASS); os.chdir(Path(sys.executable).parent)
else:
    BASE = Path(__file__).parent

# ---- настройки (хранятся в settings.json, меняются в интерфейсе) ----
MODEL, MAX_PENDING, STATS_EVERY = "claude-sonnet-5-5", 2, 3600
SF = Path("settings.json")
DEFAULTS = {"api_key": "", "niche": "интересные факты", "lang": "русский",
            "voice": "ru-RU-DmitryNeural", "privacy": "private", "hit_views": 1000, "pexels_key": "", "pixabay_key": ""}

def apply_settings(s=None):
    global CFG, KEY, PEXELS, PIXABAY, NICHE, LANG, VOICE, PRIVACY, HIT_VIEWS
    if s is None: s = json.loads(SF.read_text("utf-8")) if SF.exists() else {}
    CFG = {**DEFAULTS, **s}
    KEY, PEXELS, NICHE, LANG, VOICE = CFG["api_key"], CFG["pexels_key"], CFG["niche"], CFG["lang"], CFG["voice"]
    PIXABAY = CFG["pixabay_key"]
    PRIVACY, HIT_VIEWS = CFG["privacy"], int(CFG["hit_views"])

apply_settings()
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]

Path("videos").mkdir(exist_ok=True)
db = sqlite3.connect("office.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""create table if not exists items(id integer primary key, status text, title text,
              data text, video text, yt_id text, views integer default 0)""")
lock = threading.Lock()

def q(sql, *a):
    with lock:
        c = db.execute(sql, a); db.commit(); return c.fetchall()

AG = {k: {"s": "idle", "t": "Ждёт задачу"} for k in ("writer", "editor", "analyst")}
LOG, INSIGHTS = [], "Данных пока нет, это первые видео."

def setag(k, s, t): AG[k] = {"s": s, "t": t}
def log(m): LOG.insert(0, time.strftime("%H:%M:%S ") + m); del LOG[100:]

# ---- YouTube ----
def youtube():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file("token.json", SCOPES) if os.path.exists("token.json") else None
    if not creds or not creds.valid:
        if creds and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES).run_local_server(port=0)
        Path("token.json").write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def upload(path, d):
    from googleapiclient.http import MediaFileUpload
    body = {"snippet": {"title": d["title"][:90] + " #Shorts", "description": d["description"] + "\n#Shorts",
                        "tags": d.get("tags", []), "categoryId": "22"},
            "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False}}
    r = youtube().videos().insert(part="snippet,status", body=body,
                                  media_body=MediaFileUpload(path, resumable=True)).execute()
    return r["id"]

def fetch_stats(ids):
    r = youtube().videos().list(part="statistics", id=",".join(ids)).execute()
    return {i["id"]: int(i["statistics"].get("viewCount", 0)) for i in r["items"]}

# ---- агент 1: сценарист ----
def gen_script(insights, titles):
    prompt = (f"Ты сценарист YouTube Shorts в нише «{NICHE}». Язык: {LANG}.\n"
              f"Аналитика канала: {insights}\nУже были темы (не повторяй): {titles}\n"
              "Придумай новую тему и напиши сценарий на 25-35 секунд. Первая фраза: сильный хук. "
              'Ответь только JSON: {"title":"","scenes":[{"text":"1-2 короткие фразы для озвучки","query":"2-3 английских слова для поиска подходящего видео"}],"description":"","tags":[]}. '
              'Сцен 6-9. query должен конкретно показывать то, о чём говорит фраза.')
    r = Anthropic(api_key=KEY).messages.create(model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
    t = "".join(b.text for b in r.content if b.type == "text")
    d = json.loads(t[t.find("{"): t.rfind("}") + 1])
    d["narration"] = " ".join(s["text"] for s in d["scenes"])
    return d

async def writer_loop():
    while True:
        n = q("select count(*) c from items where status='script_pending'")[0]["c"]
        if KEY and n < MAX_PENDING:
            setag("writer", "working", "Пишет сценарий")
            titles = [r["title"] for r in q("select title from items")]
            try:
                d = await asyncio.to_thread(gen_script, INSIGHTS, titles)
                q("insert into items(status,title,data) values('script_pending',?,?)",
                  d["title"], json.dumps(d, ensure_ascii=False))
                log("Сценарист: готов сценарий «" + d["title"] + "»")
            except Exception as e:
                log(f"Сценарист: ошибка {e}"); await asyncio.sleep(60)
        n = q("select count(*) c from items where status='script_pending'")[0]["c"]
        setag("writer", "waiting" if n else "idle", f"Ждёт вашего решения ({n})" if n else ("Свободен" if KEY else "Введите ключ Claude в настройках"))
        await asyncio.sleep(10)

# ---- агент 2: монтажёр и публикатор ----
def ts(s):
    return f"{int(s//3600):02}:{int(s%3600//60):02}:{int(s%60):02},{int(s%1*1000):03}"

def make_srt(text, dur):
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p]
    total, t, out = sum(len(p) for p in parts), 0.0, []
    for i, p in enumerate(parts, 1):
        e = t + dur * len(p) / total
        out.append(f"{i}\n{ts(t)} --> {ts(e)}\n{p}\n"); t = e
    return "\n".join(out)

def pixabay_clip(query, path):
    import urllib.parse, urllib.request
    ua = {"User-Agent": "Mozilla/5.0"}
    u = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
        {"key": PIXABAY, "q": query, "per_page": 20, "safesearch": "true"})
    hits = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=ua), timeout=30))["hits"]
    v = random.choice(hits[:10])["videos"]
    url = next(v[k]["url"] for k in ("medium", "small", "large", "tiny") if v.get(k, {}).get("url"))
    Path(path).write_bytes(urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=120).read())

def pexels_clip(query, path):
    import urllib.parse, urllib.request
    ua = {"User-Agent": "Mozilla/5.0"}
    u = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
        {"query": query, "orientation": "portrait", "per_page": 8})
    vids = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={**ua, "Authorization": PEXELS}), timeout=30))["videos"]
    v = random.choice(vids)
    f = min((f for f in v["video_files"] if f["file_type"] == "video/mp4" and (f.get("width") or 0) >= 540),
            key=lambda f: abs(f["width"] - 1080))
    Path(path).write_bytes(urllib.request.urlopen(urllib.request.Request(f["link"], headers=ua), timeout=120).read())

async def render(i, d):
    import imageio_ffmpeg
    from mutagen.mp3 import MP3
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    mp3, out = f"videos/{i}.mp3", f"videos/{i}.mp4"
    scenes = d.get("scenes") or [{"text": s, "query": "abstract background"}
                                 for s in re.split(r"(?<=[.!?])\s+", d["narration"]) if s]
    await edge_tts.Communicate(" ".join(s["text"] for s in scenes), VOICE).save(mp3)
    dur, total = MP3(mp3).info.length, sum(len(s["text"]) for s in scenes)
    base = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p"

    async def run(*args):
        p = await asyncio.create_subprocess_exec(ff, "-y", *args, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        return await p.wait() == 0

    segs, srt, t = [], [], 0.0
    for n, s in enumerate(scenes):
        sd = dur * len(s["text"]) / total
        srt.append(f"{n+1}\n{ts(t)} --> {ts(t+sd)}\n{s['text']}\n"); t += sd
        seg, clip, ok = f"videos/{i}_{n}.mp4", f"videos/{i}_{n}_src.mp4", False
        if PIXABAY or PEXELS:
            try:
                await asyncio.to_thread(pixabay_clip if PIXABAY else pexels_clip, s["query"], clip)
                ok = await run("-stream_loop", "-1", "-i", clip, "-t", f"{sd:.2f}", "-vf", base, "-an", "-c:v", "libx264", seg)
            except Exception as e:
                log(f"Монтажёр: нет клипа для «{s['query']}», беру фон ({e})")
        if not ok:
            color = random.choice(["0x1e1b4b", "0x064e3b", "0x7c2d12", "0x1e3a8a"])
            ok = await run("-f", "lavfi", "-i", f"color=c={color}:s=1080x1920:r=30", "-t", f"{sd:.2f}",
                           "-pix_fmt", "yuv420p", "-c:v", "libx264", seg)
        if not ok: raise RuntimeError("ffmpeg не смог собрать сцену")
        segs.append(Path(seg).name)
    Path(f"videos/{i}.srt").write_text("\n".join(srt), encoding="utf-8")
    lst = f"videos/{i}_list.txt"
    Path(lst).write_text("".join(f"file '{x}'\n" for x in segs))
    sub = f"subtitles=videos/{i}.srt:force_style='FontSize=20,Alignment=2,MarginV=350,Outline=2'"
    for vf in (sub, None):  # если субтитры не поддерживаются, соберём без них
        args = ["-f", "concat", "-safe", "0", "-i", lst, "-i", mp3] + (["-vf", vf] if vf else []) + [
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out]
        if await run(*args): break
    else:
        raise RuntimeError("ffmpeg завершился с ошибкой")
    for x in glob.glob(f"videos/{i}_*"): os.remove(x)
    return out

async def editor_loop():
    while True:
        r = q("select * from items where status='script_approved' limit 1")
        if r:
            r = r[0]; setag("editor", "working", "Монтирует: " + r["title"])
            q("update items set status='rendering' where id=?", r["id"])
            try:
                path = await render(r["id"], json.loads(r["data"]))
                q("update items set status='video_pending', video=? where id=?", path, r["id"])
                log("Монтажёр: видео готово «" + r["title"] + "»")
            except Exception as e:
                q("update items set status='render_failed' where id=?", r["id"]); log(f"Монтажёр: ошибка {e}")
            continue
        r = q("select * from items where status='video_approved' limit 1")
        if r:
            r = r[0]; setag("editor", "working", "Загружает на YouTube: " + r["title"])
            try:
                yid = await asyncio.to_thread(upload, r["video"], json.loads(r["data"]))
                q("update items set status='published', yt_id=? where id=?", yid, r["id"])
                log(f"Монтажёр: опубликовано https://youtu.be/{yid}")
            except Exception as e:
                q("update items set status='upload_failed' where id=?", r["id"]); log(f"Публикация: ошибка {e}")
            continue
        n = q("select count(*) c from items where status='video_pending'")[0]["c"]
        setag("editor", "waiting" if n else "idle", f"Ждёт вашего решения ({n})" if n else "Ждёт сценарий")
        await asyncio.sleep(5)

# ---- агент 3: аналитик ----
async def analyst_loop():
    global INSIGHTS
    while True:
        rows = q("select id,title,yt_id from items where status='published'")
        if rows:
            setag("analyst", "working", "Собирает статистику канала")
            try:
                st = await asyncio.to_thread(fetch_stats, [r["yt_id"] for r in rows])
                for r in rows: q("update items set views=? where id=?", st.get(r["yt_id"], 0), r["id"])
                rows = sorted(q("select title,views from items where status='published'"), key=lambda r: -r["views"])
                hit = [f"«{r['title']}» ({r['views']})" for r in rows if r["views"] >= HIT_VIEWS][:3]
                miss = [f"«{r['title']}» ({r['views']})" for r in rows if r["views"] < HIT_VIEWS][-3:]
                INSIGHTS = f"Залетели: {', '.join(hit) or 'нет'}. Не зашли: {', '.join(miss) or 'нет'}. Делай больше похожего на залетевшее."
                log("Аналитик: отчёт передан сценаристу")
            except Exception as e:
                log(f"Аналитик: ошибка {e}")
        setag("analyst", "idle", "Следующий сбор данных позже")
        await asyncio.sleep(STATS_EVERY)

# ---- API и запуск ----
@asynccontextmanager
async def lifespan(_):
    for f in (writer_loop, editor_loop, analyst_loop): asyncio.create_task(f())
    yield

app = FastAPI(lifespan=lifespan)

class Decision(BaseModel):
    id: int
    ok: bool

@app.get("/api/state")
def state():
    rows = q("select id,status,title,data,views from items order by id desc")
    return {"agents": AG, "log": LOG[:30],
            "pending": [{"id": r["id"], "kind": r["status"], "title": r["title"],
                         "text": json.loads(r["data"])["narration"]}
                        for r in rows if r["status"] in ("script_pending", "video_pending")],
            "videos": [{"title": r["title"], "views": r["views"], "hit": r["views"] >= HIT_VIEWS}
                       for r in rows if r["status"] == "published"]}

@app.post("/api/decide")
def decide(d: Decision):
    r = q("select status from items where id=?", d.id)
    nxt = {"script_pending": ("script_approved", "script_rejected"),
           "video_pending": ("video_approved", "video_rejected")}.get(r[0]["status"]) if r else None
    if nxt: q("update items set status=? where id=?", nxt[0 if d.ok else 1], d.id)
    return {"ok": True}

@app.get("/media/{i}")
def media(i: int): return FileResponse(f"videos/{i}.mp4")

@app.get("/")
def index(): return FileResponse(BASE / "index.html")

class Settings(BaseModel):
    api_key: str = ""
    niche: str
    lang: str
    voice: str
    privacy: str
    pexels_key: str = ""
    pixabay_key: str = ""
    hit_views: int

@app.get("/api/settings")
def get_settings():
    return {**{k: v for k, v in CFG.items() if k not in ("api_key", "pexels_key", "pixabay_key")}, "has_key": bool(KEY), "has_pexels": bool(PEXELS), "has_pixabay": bool(PIXABAY),
            "has_client": os.path.exists("client_secret.json"), "has_token": os.path.exists("token.json")}

@app.post("/api/settings")
def set_settings(s: Settings):
    new = s.model_dump()
    if not new["api_key"]: new["api_key"] = KEY
    if not new["pexels_key"]: new["pexels_key"] = PEXELS
    if not new["pixabay_key"]: new["pixabay_key"] = PIXABAY
    SF.write_text(json.dumps(new, ensure_ascii=False), "utf-8"); apply_settings(new)
    return {"ok": True}

@app.post("/api/client_secret")
def client_secret(body: dict):
    try:
        json.loads(body["text"]); Path("client_secret.json").write_text(body["text"], "utf-8")
        return {"ok": True}
    except Exception:
        return {"ok": False}

@app.post("/api/youtube_auth")
def yt_auth():
    try:
        youtube(); log("YouTube подключён"); return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

if __name__ == "__main__":
    import webbrowser
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8000")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
