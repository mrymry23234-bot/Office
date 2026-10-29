"""AI-офис для YouTube Shorts: сценарист -> (вы) -> монтажёр/публикатор -> (вы) -> аналитик -> сценарист."""
import asyncio, glob, json, os, random, re, sqlite3, subprocess, sys, threading, time
from contextlib import asynccontextmanager
from pathlib import Path

import edge_tts
import uvicorn
from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
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
            "voice": "ru-RU-DmitryNeural", "privacy": "private", "hit_views": 1000,
            "replicate_key": "", "pixabay_key": ""}

def apply_settings(s=None):
    global CFG, KEY, REPLICATE, PIXABAY, NICHE, LANG, VOICE, PRIVACY, HIT_VIEWS
    if s is None: s = json.loads(SF.read_text("utf-8")) if SF.exists() else {}
    CFG = {**DEFAULTS, **s}
    KEY, NICHE, LANG, VOICE = CFG["api_key"], CFG["niche"], CFG["lang"], CFG["voice"]
    REPLICATE, PIXABAY = CFG["replicate_key"], CFG["pixabay_key"]
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
def revise(d, notes):
    prompt = (f"Вот сценарий YouTube Shorts в формате JSON:\n{json.dumps(d, ensure_ascii=False)}\n\n"
              f"Правки владельца канала: {notes}\n\nВнеси правки: можно менять фразы, порядок и число сцен, "
              "описания картинок (поля image и style, на английском), название и описание. "
              f"Язык озвучки: {LANG}. Сохрани тот же формат JSON. Ответь только JSON.")
    r = Anthropic(api_key=KEY).messages.create(model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
    s = "".join(b.text for b in r.content if b.type == "text")
    nd = json.loads(s[s.find("{"): s.rfind("}") + 1])
    nd["narration"] = " ".join(x["text"] for x in nd["scenes"])
    return nd

def gen_script(insights, titles):
    prompt = (f"Ты сценарист YouTube Shorts в нише «{NICHE}». Язык: {LANG}.\n"
              f"Аналитика канала: {insights}\nУже были темы (не повторяй): {titles}\n"
              "Придумай новую тему и напиши сценарий на 25-35 секунд. Первая фраза: сильный хук. "
              'Ответь только JSON: {"title":"","style":"единый визуальный стиль всех картинок, 5-10 английских слов","scenes":[{"text":"1-2 короткие фразы для озвучки","image":"конкретное описание картинки к фразе на английском: кто и что изображено, место, настроение, 15-25 слов","search":"2-3 английских слова для поиска настоящего фото"}],"description":"","tags":[]}. '
              'Сцен 6-9. Картинка должна точно показывать то, о чём говорит фраза (например, пирамиды, египтяне, пустыня), Если речь об акулах, на картинке акулы; главный предмет фразы всегда в центре кадра. Никаких надписей, цифр и букв на картинке.')
    r = Anthropic(api_key=KEY).messages.create(model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
    t = "".join(b.text for b in r.content if b.type == "text")
    d = json.loads(t[t.find("{"): t.rfind("}") + 1])
    d["narration"] = " ".join(s["text"] for s in d["scenes"])
    return d

async def writer_loop():
    while True:
        rv = q("select * from items where status='script_revise' limit 1")
        if rv:
            rv = rv[0]; setag("writer", "working", "Переписывает по вашим правкам")
            d = json.loads(rv["data"]); notes = d.pop("notes", "")
            try:
                d = await asyncio.to_thread(revise, d, notes)
                q("update items set status='script_pending', title=?, data=? where id=?",
                  d["title"], json.dumps(d, ensure_ascii=False), rv["id"])
                log("Сценарист: сценарий переработан по вашим правкам")
            except Exception as e:
                q("update items set status='script_pending' where id=?", rv["id"]); log(f"Сценарист: не смог внести правки ({e})")
            continue
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

async def tts(text, path, voice=None):
    voice = voice or VOICE
    text = re.sub(r"[^\w\s.,!?;:()«»\"'—–-]", " ", text)  # убираем эмодзи и странные символы
    text = re.sub(r"\s+", " ", text).strip()
    last = None
    for _ in range(4):  # сервис озвучки иногда отвечает ошибкой: пробуем тот же голос ещё раз
        try:
            await edge_tts.Communicate(text, voice).save(path)
            if Path(path).stat().st_size > 0: return
        except Exception as e:
            last = e; await asyncio.sleep(3)
    raise RuntimeError(f"голос {voice} недоступен: {last}")

UA = {"User-Agent": "Mozilla/5.0"}

def download(url, path):
    import urllib.request
    Path(path).write_bytes(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90).read())

def gen_image(prompt, path):  # бесплатный генератор Pollinations
    import urllib.parse
    u = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:500]) +
         f"?width=1080&height=1920&model=flux&nologo=true&seed={random.randint(1, 99999)}")
    last = None
    for k in range(3):
        try:
            download(u, path)
            if Path(path).stat().st_size > 5000: return
        except Exception as e:
            last = e
        time.sleep(8 * (k + 1))
    raise RuntimeError(last or "пустой ответ")

def replicate_image(prompt, path):  # платный, но дешёвый и надёжный генератор FLUX (≈0,3 цента за картинку)
    import urllib.request
    H = {"Authorization": "Bearer " + REPLICATE, "Content-Type": "application/json", "Prefer": "wait=60"}
    body = json.dumps({"input": {"prompt": prompt, "aspect_ratio": "9:16", "output_format": "jpg", "num_outputs": 1}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions", body, H), timeout=90))
    for _ in range(30):
        if r.get("status") == "succeeded": break
        if r.get("status") in ("failed", "canceled"): raise RuntimeError(r.get("error") or r["status"])
        time.sleep(2)
        r = json.load(urllib.request.urlopen(urllib.request.Request(r["urls"]["get"], headers=H), timeout=30))
    out = r["output"]
    download(out[0] if isinstance(out, list) else out, path)

def pixabay_image(q, path):  # запасной вариант: настоящие фото
    import urllib.parse, urllib.request
    u = "https://pixabay.com/api/?" + urllib.parse.urlencode({"key": PIXABAY, "q": q[:100], "image_type": "photo",
        "orientation": "vertical", "per_page": 15, "safesearch": "true"})
    hits = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30))["hits"]
    if not hits: raise RuntimeError("ничего не найдено")
    download(random.choice(hits[:8])["largeImageURL"], path)

async def get_image(path, s, d):
    steps = []
    if s.get("image"):
        p = f"{s['image']}, {d.get('style', '')}, cinematic, detailed, vertical composition, no text, no letters, no watermark"
        if REPLICATE: steps.append(("Replicate", replicate_image, p))
        steps.append(("бесплатный генератор", gen_image, p))
    if PIXABAY and (s.get("search") or s.get("image")): steps.append(("Pixabay", pixabay_image, s.get("search") or s["image"]))
    for name, fn, arg in steps:
        try:
            await asyncio.to_thread(fn, arg, path)
            return path
        except Exception as e:
            log(f"Монтажёр: {name} не дал картинку ({e})")
    return None

PALETTES = [("#1e1b4b", "#7c3aed"), ("#0f172a", "#0ea5e9"), ("#431407", "#f97316"),
            ("#052e16", "#22c55e"), ("#4c0519", "#f43f5e"), ("#172554", "#06b6d4")]

def font_path():
    for p in (BASE / "font.ttf", Path("C:/Windows/Fonts/arialbd.ttf"),
              Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")):
        if Path(p).exists(): return str(p)
    raise RuntimeError("не найден файл шрифта font.ttf")

def chunks(text, maxw=7):
    """Режет фразу на короткие субтитры (до 7 слов), стараясь рвать по знакам препинания."""
    out = []
    for part in re.split(r"(?<=[.!?,;:])\s+", text):
        w = part.split()
        k = -(-len(w) // maxw) or 1
        size = -(-len(w) // k) if w else 1
        out += [" ".join(w[i:i + size]) for i in range(0, len(w), size)]
    return [c for c in out if c] or [text]

def scene_png(text, n, total, path, photo=None):
    """Кадр: картинка (или мягкий фон), маленькие субтитры внизу и тонкая полоса прогресса."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
    W, H, fp = 1080, 1920, font_path()
    if photo:
        img = ImageOps.fit(Image.open(photo).convert("RGB"), (W, H)).convert("RGBA")
    else:
        c1, c2 = random.choice(PALETTES)
        bg = Image.composite(Image.new("RGB", (W, H), c2), Image.new("RGB", (W, H), c1),
                             Image.linear_gradient("L").resize((W, H)))
        img = bg.convert("RGBA")
    shade = Image.new("RGBA", (W, 650), (0, 0, 0, 255))
    shade.putalpha(Image.linear_gradient("L").resize((W, 650)).point(lambda v: int(v * 0.75)))
    img.alpha_composite(shade, (0, H - 650))
    d = ImageDraw.Draw(img)
    f, lines, cur = ImageFont.truetype(fp, 46), [], ""
    for w in text.split():
        tst = (cur + " " + w).strip()
        if f.getlength(tst) <= 900: cur = tst
        else: lines.append(cur); cur = w
    lines = [l for l in lines + [cur] if l]
    y = H - 330 - len(lines) * 60
    for l in lines:
        d.text(((W - f.getlength(l)) / 2, y), l, font=f, fill="white", stroke_width=3, stroke_fill="black")
        y += 60
    d.rounded_rectangle((160, H - 260, 920, H - 254), 3, fill=(255, 255, 255, 60))
    d.rounded_rectangle((160, H - 260, 160 + 760 * (n + 1) / total, H - 254), 3, fill="#fde047")
    img.convert("RGB").save(path)

async def render(i, d):
    import imageio_ffmpeg
    from mutagen.mp3 import MP3
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    mp3, out = f"videos/{i}.mp3", f"videos/{i}.mp4"
    scenes = d.get("scenes") or [{"text": s} for s in re.split(r"(?<=[.!?])\s+", d["narration"]) if s]
    await tts(" ".join(s["text"] for s in scenes), mp3)
    dur, total = MP3(mp3).info.length, sum(len(s["text"]) for s in scenes)

    async def run(*args):
        p = await asyncio.create_subprocess_exec(ff, "-y", *args, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        return await p.wait() == 0

    segs, fails, last = [], 0, None
    for n, s in enumerate(scenes):
        sd = dur * len(s["text"]) / total
        photo = None
        if fails < 2:  # если картинки два раза подряд не получились, дальше повторяем последнюю
            photo = await get_image(f"videos/{i}_{n}.jpg", s, d)
            fails = 0 if photo else fails + 1
        photo = photo or last
        last = photo or last
        parts = chunks(s["text"])
        tot_frames, off = max(2, round(sd * 30)), 0
        z0, z1 = (1.0, 1.12) if n % 2 == 0 else (1.12, 1.0)  # плавное приближение / отдаление
        for k, ch in enumerate(parts):
            cd = sd * len(ch) / sum(len(c) for c in parts)
            frames = max(2, round(cd * 30))
            png, seg = f"videos/{i}_{n}_{k}.png", f"videos/{i}_{n}_{k}.mp4"
            await asyncio.to_thread(scene_png, ch, n, len(scenes), png, photo)
            zoom = (f"scale=1620:2880,zoompan=z='{z0}+({z1}-{z0})*(on+{off})/{tot_frames}':x='iw/2-(iw/zoom/2)':"
                    f"y='ih/2-(ih/zoom/2)':d={frames}:s=1080x1920:fps=30,format=yuv420p")
            ok = await run("-i", png, "-vf", zoom, "-t", f"{cd:.2f}", "-c:v", "libx264", "-preset", "veryfast", seg)
            if not ok:  # запасной вариант без движения
                ok = await run("-loop", "1", "-i", png, "-t", f"{cd:.2f}", "-vf", "scale=1080:1920,fps=30,format=yuv420p",
                               "-c:v", "libx264", "-preset", "veryfast", seg)
            if not ok: raise RuntimeError("ffmpeg не смог собрать сцену")
            segs.append(Path(seg).name); off += frames
    lst = f"videos/{i}_list.txt"
    Path(lst).write_text("".join(f"file '{x}'\n" for x in segs))
    if not await run("-f", "concat", "-safe", "0", "-i", lst, "-i", mp3, "-map", "0:v", "-map", "1:a",
                     "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out):
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
        r = q("select * from items where status='video_revise' limit 1")
        if r:
            r = r[0]; setag("editor", "working", "Учитывает ваши правки: " + r["title"])
            d = json.loads(r["data"]); notes = d.pop("notes", "")
            try:
                d = await asyncio.to_thread(revise, d, notes)
                q("update items set status='script_approved', title=?, data=? where id=?",
                  d["title"], json.dumps(d, ensure_ascii=False), r["id"])
                log("Монтажёр: правки приняты, пересобираю видео")
            except Exception as e:
                q("update items set status='video_pending' where id=?", r["id"]); log(f"Монтажёр: не смог внести правки ({e})")
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
    ok: bool = False
    revise: str = ""

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
    r = q("select status, data from items where id=?", d.id)
    if not r: return {"ok": False}
    st = r[0]["status"]
    if d.revise.strip() and st in ("script_pending", "video_pending"):  # отправить на доработку
        data = json.loads(r[0]["data"]); data["notes"] = d.revise.strip()
        q("update items set status=?, data=? where id=?", st.replace("_pending", "_revise"),
          json.dumps(data, ensure_ascii=False), d.id)
        return {"ok": True}
    nxt = {"script_pending": ("script_approved", "script_rejected"),
           "video_pending": ("video_approved", "video_rejected")}.get(st)
    if nxt: q("update items set status=? where id=?", nxt[0 if d.ok else 1], d.id)
    return {"ok": True}

@app.get("/api/voice_test")
async def voice_test(voice: str):
    if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}-\w+Neural", voice): raise HTTPException(400)
    sample = {"ru": "Привет! Так звучит этот голос.", "uk": "Привіт! Так звучить цей голос.",
              "de": "Hallo! So klingt diese Stimme.", "es": "¡Hola! Así suena esta voz.",
              "fr": "Bonjour ! Voici ma voix.", "pl": "Cześć! Tak brzmi ten głos."}.get(voice[:2], "Hello! This is how this voice sounds.")
    path = f"videos/test_{voice}.mp3"
    try: await tts(sample, path, voice)
    except Exception: raise HTTPException(503)
    return FileResponse(path, media_type="audio/mpeg")

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
    hit_views: int
    replicate_key: str = ""
    pixabay_key: str = ""

@app.get("/api/settings")
def get_settings():
    return {**{k: v for k, v in CFG.items() if k not in ("api_key", "replicate_key", "pixabay_key")}, "has_key": bool(KEY), "has_replicate": bool(REPLICATE), "has_pixabay": bool(PIXABAY),
            "has_client": os.path.exists("client_secret.json"), "has_token": os.path.exists("token.json")}

@app.post("/api/settings")
def set_settings(s: Settings):
    new = s.model_dump()
    if not new["api_key"]: new["api_key"] = KEY
    if not new["replicate_key"]: new["replicate_key"] = REPLICATE
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
