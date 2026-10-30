"""AI-офис для YouTube Shorts: сценарист -> (вы) -> монтажёр/публикатор -> (вы) -> аналитик -> сценарист."""
import asyncio, base64, glob, json, math, os, random, re, sqlite3, subprocess, sys, threading, time, urllib.request, urllib.parse, uuid
from contextlib import asynccontextmanager
from pathlib import Path

import edge_tts
import uvicorn
from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
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
            "writer_rules": "", "editor_rules": "", "analyst_rules": "", "visual_mode": "comfy", "motion": "dynamic", "character": "", "comfy_url": "http://127.0.0.1:8188", "comfy_checkpoint": "", "image_steps": 2}

def apply_settings(s=None):
    global CFG, KEY, NICHE, LANG, VOICE, PRIVACY, HIT_VIEWS
    if s is None: s = json.loads(SF.read_text("utf-8")) if SF.exists() else {}
    CFG = {**DEFAULTS, **s}
    KEY, NICHE, LANG, VOICE = CFG["api_key"], CFG["niche"], CFG["lang"], CFG["voice"]
    PRIVACY, HIT_VIEWS = CFG["privacy"], int(CFG["hit_views"])

apply_settings()
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]

Path("videos").mkdir(exist_ok=True)
db = sqlite3.connect("office.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""create table if not exists items(id integer primary key, status text, title text,
              data text, video text, yt_id text, views integer default 0)""")
db.execute("""create table if not exists item_errors(item_id integer primary key, message text, updated_at text default current_timestamp)""")
db.commit()
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
              "описания кадров и персонажей (поля image, style и characters, на английском), название и описание. "
              f"Язык озвучки: {LANG}. Сохрани тот же формат JSON. Ответь только JSON." + rules("w") + rules("e"))
    r = Anthropic(api_key=KEY).messages.create(model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
    s = "".join(b.text for b in r.content if b.type == "text")
    nd = json.loads(s[s.find("{"): s.rfind("}") + 1])
    nd["narration"] = " ".join(x["text"] for x in nd["scenes"])
    return nd

def gen_script(insights, titles):
    prompt = (f"Ты сценарист YouTube Shorts в нише «{NICHE}». Язык: {LANG}.\n"
              f"Аналитика канала: {insights}\nУже были темы (не повторяй): {titles}\n"
              "Придумай новую тему и напиши сценарий на 25-35 секунд. Первая фраза: сильный хук. "
              'Ответь только JSON: {"title":"","style":"единый стиль плоских иллюстраций: палитра и настроение, 5-10 английских слов","characters":["главные персонажи и предметы видео, по одному на английском, например: pharaoh in gold headdress"],"scenes":[{"text":"1-2 короткие фразы для озвучки","image":"описание кадра на английском: какие персонажи и предметы и что они делают, место, настроение, 15-25 слов"}],"description":"","tags":[]}. '
              'Сцен 6-9. Картинка должна точно показывать то, о чём говорит фраза (например, пирамиды, египтяне, пустыня), Если речь об акулах, на картинке акулы; главный предмет фразы всегда в центре кадра. Никаких надписей, цифр и букв на картинке. Рисунок будет плоской мультяшной иллюстрацией, поэтому описывай простые узнаваемые предметы.' + rules("w"))
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

FPS = 24
W, H, U = 1080, 1920, 10.8  # кадр в пикселях; холст художника 100 x 178 условных единиц, 1 единица = 10,8 px

SHAPES_DOC = """Фигуры (поздние рисуются поверх ранних; цвет "#rrggbb" или с прозрачностью "#rrggbbaa"; без текста и букв):
{"t":"rect","x":0,"y":0,"w":10,"h":10,"c":"#hex","r":2}  прямоугольник (r: скругление, необязательно)
{"t":"circle","x":50,"y":50,"r":5,"c":"#hex"}
{"t":"ellipse","x":50,"y":50,"rx":8,"ry":4,"c":"#hex","rot":0}  rot в градусах, необязательно
{"t":"poly","p":[[x,y],[x,y],[x,y]],"c":"#hex"}  многоугольник с острыми углами
{"t":"blob","p":[[x,y],[x,y],...],"c":"#hex"}  замкнутая плавная форма по опорным точкам (тела, головы, облака, холмы)
{"t":"line","p":[[x,y],[x,y]],"w":1,"c":"#hex"}  линия или ломаная толщиной w
У любой фигуры (кроме line) могут быть: "c2":"#hex" (градиент сверху вниз от c к c2) и "o":"#hex","ow":0.4 (контур и его толщина)."""

STYLE_GUIDE = """СТИЛЬ: современная профессиональная 2D-анимация уровня Kurzgesagt, TED-Ed и Headspace, а не детский рисунок. Ограниченная согласованная палитра (5-7 цветов на всё видео) и их оттенки. Плавные скруглённые формы: для тел, голов, облаков, холмов используй blob, а не голые круги и треугольники. У каждой крупной формы градиент (c и c2 близких оттенков), мягкие тени и блики отдельными полупрозрачными фигурами (#00000030, #ffffff40). Тонкий тёмный контур (o, ow 0.3-0.5) у главных персонажей. У персонажей живые лица: глаза с белком, зрачком и бликом, брови, рот, румянец. Приятные пропорции как в современных мультфильмах."""

PLAN_RULES = """Ты арт-директор мультфильма для YouTube Shorts. Составь план актёров: главные персонажи и ключевые предметы этого видео (3-6 штук, например: фараон, пирамида, верблюд, солнце) плюс 1-2 фоновых живых актёра (облако, птица). Каждый актёр затем рисуется ОДИН раз и используется во всех сценах, поэтому выглядит везде одинаково. Подбери палитру видео из 6 цветов.
Верни ТОЛЬКО JSON: {"palette":["#hex","#hex","#hex","#hex","#hex","#hex"],"assets":[{"id":"pharaoh","desc":"внешность и повадки, 1-2 предложения на английском"}]}. id только латиницей."""

ASSET_RULES = """Актёр рисуется в собственном квадрате 100x100 (центр 50,50; ноги и основание примерно на y=90, не выходи за квадрат). Актёр состоит из частей (parts), которые двигаются независимо: у персонажа туловище, голова, глаза, каждая рука, каждая нога, хвост или одежда (плащ, волосы) отдельными частями; у неподвижного предмета обычно одна-две части. Порядок частей = порядок рисования (задние первыми). До 12 частей, до 25 фигур в части.
Формат ответа, ТОЛЬКО JSON: {"id":"...","parts":[{"name":"body","pivot":[50,50],"anim":{"type":"bob","amp":0.4,"speed":0.5},"shapes":[...]},{"name":"arm_r","pivot":[62,48],"anim":{"type":"swing","amp":30,"speed":0.7,"phase":0},"shapes":[...]}]}
pivot: точка вращения части (плечо, бедро, основание хвоста). anim.type: "none"; "swing" (качание вокруг pivot на ±amp градусов, speed колебаний в секунду, phase 0..1); "sway" (вбок на amp единиц); "bob" (вверх-вниз на amp единиц); "spin" (вращение, speed оборотов в секунду); "talk" (рот открывается и закрывается, ставь на часть-рот); "blink" (часть видна только в момент моргания: нарисуй веки цвета кожи поверх глаз, speed 0.3).
Движение должно выглядеть живым: туловище слегка дышит (bob amp 0.4), руки и ноги качаются (ноги в противофазе: phase 0 и 0.5), хвост и одежда запаздывают (меньше amp, другой phase), голова слегка кивает, моргают глаза. Облака плывут (sway), у солнца вращаются лучи (spin)."""

HOST_RULES = """Это постоянный ведущий-рассказчик канала: он должен быть обаятельным, запоминающимся и выразительным, с ярким узнаваемым силуэтом. Нарисуй его целиком, в полный рост, лицом в сторону зрителя (три четверти или анфас). Обязательные части: туловище, голова, глаза, веки (anim blink), рот (anim talk), брови, обе руки (правая размахивает как при рассказе: swing amp 35, левая слегка), ноги, детали образа (волосы, шляпа, хвост, аксессуар) с запаздывающим sway или swing."""

STAGE_RULES = """Холст: ширина 100, высота 178 условных единиц (вертикальный кадр 9:16), (0,0) в левом верхнем углу, y растёт вниз. Нижние 40 единиц закрывают субтитры: ничего важного туда не ставь.
Верни ТОЛЬКО JSON: {"bg":["#цвет верха","#цвет низа"],"shapes":[неподвижный фон в профессиональном стиле: небо с градиентом, дальние холмы, земля, объекты; координаты холста; 15-35 фигур],"fx":"dust","actors":[...]}
fx: частицы в воздухе: "dust" (пылинки), "sparkles" (искры), "snow", "bubbles", "leaves" или "none".
actors: актёры из cast, рисуются по порядку (дальние первыми). Поля: {"asset":"id","x":50,"y":100,"size":40,"flip":false,"to":[70,100],"bob":{"amp":1.5,"speed":2},"show":[0,1],"shadow":true}
x,y: где центр актёра в начале сцены (землю рисуй примерно на y 100-125); size: ширина актёра в единицах холста; to: где он окажется в конце сцены; flip: true, если смотрит или идёт влево; bob: подпрыгивание всего актёра; show: доля времени сцены [от, до], когда актёр виден; shadow: false для облаков и летающих.
Кадр обязан показывать то, о чём говорит фраза. В кадре всегда много движения: не меньше 3 актёров перемещаются (to) одновременно, облака и птицы плывут, предметы переезжают, персонажи идут и жестикулируют. Ставь 4-9 актёров, главный крупнее всех. Один и тот же актёр обязан выглядеть одинаково во всех сценах. Используй цвета палитры видео."""

def rules(kind):
    r = (CFG.get("editor_rules" if kind == "e" else "writer_rules") or "").strip()
    return f"\n\nПостоянные правила владельца канала (соблюдай строго, они важнее остальных пожеланий):\n{r}" if r else ""

def ask_json(prompt, max_tokens):
    with Anthropic(api_key=KEY).messages.stream(model=MODEL, max_tokens=max_tokens,
                                                messages=[{"role": "user", "content": prompt}]) as st:
        r = st.get_final_message()
    txt = "".join(b.text for b in r.content if b.type == "text")
    return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])

def plan_cast(d, host):
    scenes = "\n".join(f"{n+1}. {s['text']} [{s.get('image', '')}]" for n, s in enumerate(d["scenes"]))
    note = "\nВедущий-рассказчик уже существует, его НЕ включай в план." if host else ""
    plan = ask_json(f"Видео: «{d['title']}». Стиль: {d.get('style') or 'flat cartoon'}.\nГлавные персонажи и предметы: "
                    f"{'; '.join(d.get('characters', [])) or 'определи сам'}.{note}\nСцены:\n{scenes}\n\n" + PLAN_RULES + rules("e"), 3000)
    if not plan.get("assets"): raise RuntimeError("пустой план")
    return plan

def draw_asset(item, plan, d):
    a = ask_json(f"Нарисуй ОДНОГО актёра «{item['id']}»: {item.get('desc', '')}. Видео: «{d['title']}».\n"
                 f"Палитра видео (используй только эти цвета и их оттенки): {', '.join(plan.get('palette', []))}.\n\n"
                 + STYLE_GUIDE + "\n\n" + ASSET_RULES + "\n\n" + SHAPES_DOC + rules("e"), 12000)
    a["id"] = item["id"]
    if not a.get("parts"): raise RuntimeError("пустой актёр")
    return a

def design_host(desc):
    a = ask_json(f"Ты дизайнер персонажей для анимации. Создай персонажа-рассказчика, который ведёт YouTube Shorts и рассказывает зрителям истории.\n"
                 f"Описание от владельца канала: {desc}\n\n" + STYLE_GUIDE + "\n\n" + HOST_RULES + "\n\n" + ASSET_RULES + "\n\n" + SHAPES_DOC, 16000)
    a["id"] = "host"
    if not a.get("parts"): raise RuntimeError("пустой персонаж")
    return a

def make_stage(s, d, cast, palette, host):
    brief = "; ".join(f"{a['id']} (части: {', '.join(p.get('name', '?') for p in a['parts'])})" for a in cast)
    note = ("\nВедущий-рассказчик (asset \"host\") присутствует в каждой сцене: поставь его крупно сбоку на переднем плане (size 38-50, y около 112), "
            "лицом к зрителю, он рассказывает историю и реагирует на происходящее; остальные актёры показывают сюжет.") if host else ""
    return ask_json(f"Ты режиссёр мультфильма для YouTube Shorts. Видео: «{d['title']}». Поставь кадр к фразе диктора: «{s['text']}»\n"
                    f"Что показать: {s.get('image') or s['text']}\nСтиль: {d.get('style') or 'flat cartoon'}\n"
                    f"Палитра видео: {', '.join(palette)}\nДоступные актёры (cast): {brief}{note}\n\n"
                    + STYLE_GUIDE + "\n\n" + SHAPES_DOC + "\n\n" + STAGE_RULES + rules("e"), 9000)

CF = Path("characters.json")
def load_chars(): return json.loads(CF.read_text("utf-8")) if CF.exists() else []
def save_chars(c): CF.write_text(json.dumps(c, ensure_ascii=False), "utf-8")

def draw_shapes(img, shapes, k, ox=0.0, oy=0.0):
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(img, "RGBA" if img.mode == "RGB" else None)
    P = lambda p: [(float(x) * k + ox, float(y) * k + oy) for x, y in p]

    def smooth(pts):  # срезаем углы: получается плавная форма
        for _ in range(3):
            new = []
            for j in range(len(pts)):
                a, b = pts[j], pts[(j + 1) % len(pts)]
                new += [(.75 * a[0] + .25 * b[0], .75 * a[1] + .25 * b[1]), (.25 * a[0] + .75 * b[0], .25 * a[1] + .75 * b[1])]
            pts = new
        return pts

    def geom(sh):
        t = sh["t"]
        if t == "rect":
            x, y, w, h = float(sh["x"]) * k + ox, float(sh["y"]) * k + oy, float(sh["w"]) * k, float(sh["h"]) * k
            return ("rect", (x, y, x + w, y + h), float(sh.get("r", 0)) * k)
        if t == "circle":
            x, y, r = float(sh["x"]) * k + ox, float(sh["y"]) * k + oy, float(sh["r"]) * k
            return ("ell", (x - r, y - r, x + r, y + r), 0)
        if t == "ellipse":
            x, y, rx, ry = float(sh["x"]) * k + ox, float(sh["y"]) * k + oy, float(sh["rx"]) * k, float(sh["ry"]) * k
            a = math.radians(float(sh.get("rot", 0)))
            return ("poly", [(x + rx * math.cos(q) * math.cos(a) - ry * math.sin(q) * math.sin(a),
                              y + rx * math.cos(q) * math.sin(a) + ry * math.sin(q) * math.cos(a))
                             for q in (j * math.pi / 36 for j in range(72))])
        if t == "poly": return ("poly", P(sh["p"]))
        if t == "blob": return ("poly", smooth(P(sh["p"])))
        if t == "line": return ("line", P(sh["p"]), max(1, int(float(sh.get("w", 1)) * k)))
        raise ValueError(t)

    def fill(dr, g, c):
        if g[0] == "rect": dr.rounded_rectangle(g[1], g[2], fill=c) if g[2] else dr.rectangle(g[1], fill=c)
        elif g[0] == "ell": dr.ellipse(g[1], fill=c)
        elif g[0] == "poly": dr.polygon(g[1], fill=c)
        else: dr.line(g[1], fill=c, width=g[2], joint="curve")

    def outline(g, c, w):
        if g[0] == "rect": d.rounded_rectangle(g[1], g[2], outline=c, width=w) if g[2] else d.rectangle(g[1], outline=c, width=w)
        elif g[0] == "ell": d.ellipse(g[1], outline=c, width=w)
        elif g[0] == "poly": d.line(g[1] + [g[1][0]], fill=c, width=w, joint="curve")

    for sh in shapes[:200]:
        try:
            g, c, c2 = geom(sh), sh["c"], sh.get("c2")
            if c2 and g[0] != "line":  # градиент: рисуем форму маской и заливаем градиентом по её рамке
                m = Image.new("L", img.size, 0); fill(ImageDraw.Draw(m), g, 255)
                bb = m.getbbox()
                if bb:
                    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
                    gr = Image.composite(Image.new("RGBA", (bw, bh), c2), Image.new("RGBA", (bw, bh), c),
                                         Image.linear_gradient("L").resize((bw, bh)))
                    img.paste(gr, bb[:2], m.crop(bb))
            else:
                fill(d, g, c)
            if sh.get("o"): outline(g, sh["o"], max(1, int(float(sh.get("ow", .4)) * k)))
        except Exception:
            continue  # одна кривая фигура не должна ломать весь рисунок

def render_bg(stage):
    from PIL import Image
    SS = 2
    bg = (stage or {}).get("bg") or ["#1e3a8a", "#0ea5e9"]
    img = Image.composite(Image.new("RGB", (W * SS, H * SS), bg[-1]), Image.new("RGB", (W * SS, H * SS), bg[0]),
                          Image.linear_gradient("L").resize((W * SS, H * SS)))
    draw_shapes(img, (stage or {}).get("shapes", []), U * SS)
    return img.resize((W, H), Image.LANCZOS)

def make_sprite(part, size):
    """Часть актёра как прозрачная картинка; вокруг запас в 25 единиц, чтобы часть могла качаться."""
    from PIL import Image
    k = size * U / 100; side = max(8, int(150 * k)); SS = 2 if side <= 1100 else 1
    img = Image.new("RGBA", (side * SS, side * SS), (0, 0, 0, 0))
    draw_shapes(img, part.get("shapes", []), k * SS, 25 * k * SS, 25 * k * SS)
    return img.resize((side, side), Image.LANCZOS) if SS > 1 else img

def build_actors(stage, cast):
    from PIL import Image, ImageOps
    by, out = {a["id"]: a for a in cast}, []
    for ac in (stage or {}).get("actors", [])[:14]:
        a = by.get(ac.get("asset"))
        if not a: continue
        size = max(5.0, min(120.0, float(ac.get("size", 40)))); k = size * U / 100; flip = bool(ac.get("flip")); parts = []
        for p in a["parts"][:14]:
            sp, pv, an = make_sprite(p, size), p.get("pivot", [50, 50]), p.get("anim") or {}
            px, py = (float(pv[0]) + 25) * k, (float(pv[1]) + 25) * k
            if flip: sp, px = ImageOps.mirror(sp), sp.width - px
            bb = sp.getbbox()
            if not bb: continue
            x0, y0, x1, y1 = bb; crop = sp.crop(bb)
            if an.get("type") in ("swing", "spin"):  # холст вокруг оси вращения: крутим только нужный кусок
                R = int(max(math.hypot(cx - px, cy - py) for cx in (x0, x1) for cy in (y0, y1))) + 3
                cv = Image.new("RGBA", (2 * R, 2 * R), (0, 0, 0, 0)); cv.paste(crop, (int(R - px + x0), int(R - py + y0)))
                parts.append({"img": cv, "ox": px - R, "oy": py - R, "rot": R, "an": an})
            else:
                parts.append({"img": crop, "ox": x0, "oy": y0, "rot": None, "an": an, "pv": (px - x0, py - y0)})
        x, y = float(ac.get("x", 50)), float(ac.get("y", 100)); to = ac.get("to") or [x, y]
        bob = ac.get("bob") or ({"amp": 0.8, "speed": 2.2} if math.hypot(float(to[0]) - x, float(to[1]) - y) > 3 else {})
        out.append({"parts": parts, "k": k, "x": x, "y": y, "to": to, "flip": flip, "bob": bob,
                    "show": ac.get("show") or [0, 1], "shadow": ac.get("shadow", True) and y > 75})
    return out

def ease(t): return t * t * (3 - 2 * t)

_VIG = None
def vignette():
    global _VIG
    if _VIG is None:
        from PIL import Image
        a = Image.radial_gradient("L").resize((W, H)).point(lambda v: int(max(0, v - 120) * 0.85))
        _VIG = Image.new("RGBA", (W, H), (0, 0, 0, 0)); _VIG.putalpha(a)
    return _VIG

def make_particles(n, seed):
    r = random.Random(seed)
    return [(r.uniform(0, W), r.uniform(0, H), r.uniform(3, 9), r.uniform(0.4, 1.4), r.uniform(0, 6.28)) for _ in range(n)]

def draw_fx(fr, kind, parts, sec):
    from PIL import ImageDraw
    d = ImageDraw.Draw(fr, "RGBA")
    for x, y, r, sp, ph in parts:
        if kind in ("snow", "leaves"): yy, xx = (y + sec * sp * 70) % H, x + math.sin(sec * 1.3 + ph) * 30
        else: yy, xx = (y - sec * sp * 45) % H, x + math.sin(sec * 1.1 + ph) * 35
        a = int(70 + 150 * abs(math.sin(sec * 2 + ph))) if kind == "sparkles" else 110
        d.ellipse((xx - r, yy - r, xx + r, yy + r), fill=(255, 255, 255, a))

def compose(bg, actors, t, sec, overlay=None, cam=None, fx=None):
    from PIL import Image, ImageDraw
    fr = bg.copy(); e = ease(t)
    for ac in actors:
        if not (ac["show"][0] <= t <= ac["show"][1]): continue
        cx = (ac["x"] + (float(ac["to"][0]) - ac["x"]) * e) * U
        cy = (ac["y"] + (float(ac["to"][1]) - ac["y"]) * e) * U
        if ac["bob"]: cy -= abs(math.sin(math.pi * float(ac["bob"].get("speed", 1)) * sec)) * float(ac["bob"].get("amp", 1)) * U
        if ac["shadow"]:
            r = ac["k"] * 26; fy = cy + 40 * ac["k"]
            ImageDraw.Draw(fr, "RGBA").ellipse((cx - r, fy - r * .2, cx + r, fy + r * .2), fill=(0, 0, 0, 55))
        ax, ay = cx - 75 * ac["k"], cy - 75 * ac["k"]
        for p in ac["parts"]:
            an = p["an"]; ty, amp = an.get("type", "none"), float(an.get("amp", 0))
            ph = 2 * math.pi * (float(an.get("speed", 0)) * sec + float(an.get("phase", 0)))
            ang = dx = dy = 0.0; img, oy = p["img"], p["oy"]
            if ty == "blink" and (float(an.get("speed", .3)) * sec + float(an.get("phase", 0))) % 1 > 0.07: continue
            if ty == "swing": ang = amp * math.sin(ph) * (-1 if ac["flip"] else 1)
            elif ty == "spin": ang = math.degrees(ph)
            elif ty == "sway": dx = amp * math.sin(ph) * U
            elif ty == "bob": dy = amp * math.sin(ph) * U
            elif ty == "talk":  # рот открывается и закрывается
                sy = 0.3 + 0.7 * abs(math.sin(math.pi * float(an.get("speed", 2.5)) * sec))
                img = img.resize((img.width, max(1, int(img.height * sy)))); oy = p["oy"] + p["pv"][1] * (1 - sy)
            if p["rot"] and abs(ang) > 0.3: img = img.rotate(ang, center=(p["rot"], p["rot"]), resample=Image.BICUBIC)
            fr.paste(img, (int(ax + p["ox"] + dx), int(ay + oy + dy)), img)
    if fx: draw_fx(fr, fx[0], fx[1], sec)
    if cam:  # плавный наезд или отъезд камеры с лёгким панорамированием
        z = cam[0] + (cam[1] - cam[0]) * e; cw, ch = W / z, H / z
        x0 = (W - cw) / 2 * (1 + cam[2] * (2 * e - 1)); y0 = (H - ch) / 2
        fr = fr.resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch))
    fr.paste(vignette(), (0, 0), vignette())
    if overlay: fr.paste(overlay, (0, H - overlay.height), overlay)
    return fr

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

def sub_overlay(text, n, total):
    """Нижняя часть кадра: затемнение, маленькие субтитры и тонкая полоса прогресса."""
    from PIL import Image, ImageDraw, ImageFont
    h = 700
    ov = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    shade = Image.new("RGBA", (W, h), (0, 0, 0, 255))
    shade.putalpha(Image.linear_gradient("L").resize((W, h)).point(lambda v: int(v * 0.7)))
    ov.alpha_composite(shade)
    d, f, lines, cur = ImageDraw.Draw(ov), ImageFont.truetype(font_path(), 46), [], ""
    for w in text.split():
        tst = (cur + " " + w).strip()
        if f.getlength(tst) <= 900: cur = tst
        else: lines.append(cur); cur = w
    lines = [l for l in lines + [cur] if l]
    y = h - 330 - len(lines) * 60
    for l in lines:
        d.text(((W - f.getlength(l)) / 2, y), l, font=f, fill="white", stroke_width=3, stroke_fill="black"); y += 60
    d.rounded_rectangle((160, h - 260, 920, h - 254), 3, fill=(255, 255, 255, 60))
    d.rounded_rectangle((160, h - 260, 160 + 760 * (n + 1) / total, h - 254), 3, fill="#fde047")
    return ov

def comfy_json(url, endpoint, payload=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url.rstrip("/") + endpoint, data=data,
        headers={"Content-Type": "application/json"} if data else {}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def comfy_checkpoints(url):
    data = comfy_json(url, "/object_info/CheckpointLoaderSimple")
    return data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]


def generate_comfy_scene(item_id, scene_index, scene, script):
    """Generate a reproducible scene image using the user's local ComfyUI server.
    A missing backend/model is an actionable error; never silently render Paint shapes.
    """
    url = CFG.get("comfy_url", "http://127.0.0.1:8188").rstrip("/")
    folder = Path("visuals") / str(item_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{scene_index+1}.png"
    if target.exists():
        try:
            from PIL import Image
            with Image.open(target) as existing: existing.verify()
            return target
        except Exception:
            target.unlink(missing_ok=True)
    try:
        models = comfy_checkpoints(url)
    except Exception as exc:
        raise RuntimeError("ComfyUI недоступен. Запустите ComfyUI на компьютере и проверьте адрес в настройках. " + str(exc)) from exc
    preferred = next((m for m in models if "sd_xl_turbo" in m.lower()), None)
    checkpoint = CFG.get("comfy_checkpoint", "").strip() or preferred or (models[0] if models else "")
    if checkpoint not in models:
        raise RuntimeError("В ComfyUI нет выбранной модели (checkpoint). Установите модель в ComfyUI/models/checkpoints и перезапустите ComfyUI.")
    style = script.get("style", "colorful high quality 2D animated movie")
    chars = ", ".join(str(x) for x in script.get("characters", [])[:5])
    prompt = (f"Beautiful professional 2D animated film still, cohesive character design, "
              f"{style}. {scene.get('image', scene.get('text', ''))}. "
              f"Recurring characters: {chars}. Strong composition, appealing lighting, "
              "rich background, cinematic storytelling, portrait 9:16, no letters, no text, no watermark")
    negative = "low quality, crude geometry, stick figures, paint drawing, watermark, text, letters, blurry, deformed"
    seed = (item_id * 1000003 + scene_index * 7919) % (2**63-1)
    # SDXL Turbo uses very low step counts and CFG=1; ordinary SDXL uses more steps.
    turbo = "turbo" in checkpoint.lower()
    steps = max(1, min(4, int(CFG.get("image_steps", 2)))) if turbo else max(8, min(40, int(CFG.get("image_steps", 20))))
    cfg_scale = 1.0 if turbo else 7.0
    sampler = "euler_ancestral" if turbo else "euler"
    workflow = {
      "1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":checkpoint}},
      "2":{"class_type":"CLIPTextEncode","inputs":{"text":prompt,"clip":["1",1]}},
      "3":{"class_type":"CLIPTextEncode","inputs":{"text":negative,"clip":["1",1]}},
      "4":{"class_type":"EmptyLatentImage","inputs":{"width":576,"height":1024,"batch_size":1}},
      "5":{"class_type":"KSampler","inputs":{"seed":seed,"steps":steps,"cfg":cfg_scale,"sampler_name":sampler,"scheduler":"normal","denoise":1.0,"model":["1",0],"positive":["2",0],"negative":["3",0],"latent_image":["4",0]}},
      "6":{"class_type":"VAEDecode","inputs":{"samples":["5",0],"vae":["1",2]}},
      "7":{"class_type":"SaveImage","inputs":{"filename_prefix":f"AIOffice_{item_id}_{scene_index}","images":["6",0]}}
    }
    job = comfy_json(url,"/prompt",{"prompt":workflow,"client_id":str(uuid.uuid4())},timeout=30)
    prompt_id = job["prompt_id"]
    for _ in range(180):
        time.sleep(2)
        history = comfy_json(url,"/history/"+urllib.parse.quote(prompt_id),timeout=20)
        if prompt_id not in history:
            continue
        record = history[prompt_id]
        if record.get("status",{}).get("status_str") == "error":
            raise RuntimeError("ComfyUI: генерация завершилась ошибкой. Проверьте журнал ComfyUI и доступную видеопамять.")
        outputs = record.get("outputs",{}).get("7",{}).get("images",[])
        if outputs:
            img = outputs[0]
            query = urllib.parse.urlencode({"filename":img["filename"],"subfolder":img.get("subfolder", ""),"type":img.get("type","output")})
            with urllib.request.urlopen(url+"/view?"+query,timeout=60) as response:
                target.write_bytes(response.read())
            from PIL import Image
            with Image.open(target) as im: im.verify()
            return target
    raise RuntimeError("ComfyUI не завершил генерацию за 6 минут. Сценарий сохранён, можно повторить монтаж.")


def illustration_frames(i, n, parts, total, nframes):
    """Render locally supplied full-frame illustrations with cinematic pans and captions.

    Source: visuals/<video_id>/<scene_number>.png|jpg|webp, 1-based scene numbering.
    Never calls paid image/video APIs and never silently falls back to Paint graphics.
    """
    from PIL import Image, ImageEnhance
    folder = Path("visuals") / str(i)
    source = next((folder / f"{n+1}.{ext}" for ext in ("png", "jpg", "jpeg", "webp")
                   if (folder / f"{n+1}.{ext}").is_file()), None)
    if source is None:
        raise RuntimeError(f"Нет иллюстрации сцены {n+1}: добавьте файл visuals/{i}/{n+1}.png (или jpg/webp). "
                           "Режим 'Иллюстрации' не рисует примитивные фигуры автоматически.")
    with Image.open(source) as im:
        original = im.convert("RGB")
    original = ImageEnhance.Contrast(original).enhance(1.04)
    acc, bounds, count = 0, [], max(1, sum(len(c) for c in parts))
    for c in parts:
        acc += len(c)
        bounds.append((acc / count, sub_overlay(c, n, total)))
    for f in range(nframes):
        t = f / max(1, nframes - 1)
        # Pan horizontally/vertically, without repetitive zoom-in/zoom-out.
        scale = max(W / original.width, H / original.height) * 1.12
        iw, ih = math.ceil(original.width * scale), math.ceil(original.height * scale)
        im = original.resize((iw, ih), Image.Resampling.LANCZOS)
        dx, dy = iw - W, ih - H
        x = int(dx * ((0.1 + 0.8 * ease(t)) if n % 2 else (0.9 - 0.8 * ease(t))))
        y = int(dy * (0.3 + 0.4 * ease(t)))
        fr = im.crop((x, y, x + W, y + H)).convert("RGBA")
        ov = next((o for b, o in bounds if t < b), bounds[-1][1])
        fr.paste(ov, (0, H - ov.height), ov)
        fr.convert("RGB").save(f"videos/{i}_{n}_{f:04d}.jpg", quality=88)

def scene_frames(i, n, stage, cast, parts, total, nframes):
    """Рисует все кадры сцены: фон, актёры с движением частей, частицы, камера и субтитры."""
    bg, actors = render_bg(stage), build_actors(stage, cast)
    kind = (stage or {}).get("fx", "dust")
    fx = (kind, make_particles(26, i * 100 + n)) if kind not in ("none", None, "") else None
    cam = ((1.0, 1.12) if n % 2 == 0 else (1.12, 1.0)) + ((n % 3 - 1) * 0.6,)
    acc, bounds, s = 0, [], sum(len(c) for c in parts)
    for c in parts:
        acc += len(c); bounds.append((acc / s, sub_overlay(c, n, total)))
    for f in range(nframes):
        ov = next((o for b, o in bounds if f / nframes < b), bounds[-1][1])
        compose(bg, actors, f / max(1, nframes - 1), f / FPS, ov, cam, fx).save(f"videos/{i}_{n}_{f:04d}.jpg", quality=90)

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

    async def retry(fn, what, *args):
        for _ in range(2):
            try: return await asyncio.to_thread(fn, *args)
            except Exception as e: log(f"Монтажёр: {what} не получилось ({e})")
        return None

    visual_mode = CFG.get("visual_mode", "auto")
    comfy_mode = visual_mode == "comfy"
    illustration_mode = visual_mode == "illustrations"
    auto_mode = visual_mode == "auto"
    def has_image(n):
        folder = Path("visuals") / str(i)
        return any((folder / f"{n+1}.{ext}").is_file() for ext in ("png", "jpg", "jpeg", "webp"))
    missing = [n for n in range(len(scenes)) if not has_image(n)]
    # Auto mode never requires the user to supply visuals; it falls back to
    # the built-in procedural renderer until a local image model is connected.
    need_procedural = not (illustration_mode or comfy_mode) and (not auto_mode or bool(missing))
    if comfy_mode and missing:
        setag("editor", "working", "Генерирует мультяшные иллюстрации через ComfyUI")
        for n in missing:
            setag("editor", "working", f"Генерирует иллюстрацию {n+1} из {len(scenes)}")
            await asyncio.to_thread(generate_comfy_scene, i, n, scenes[n], d)
    if auto_mode and missing:
        log(f"Монтажёр: нет изображений для {len(missing)} сцен; использую встроенную графику. Это временный режим, не AI-анимация.")
    host = next((c["asset"] for c in load_chars() if c["id"] == CFG.get("character")), None)
    setag("editor", "working", "Придумывает персонажей")
    plan = await retry(plan_cast, "план персонажей", d, bool(host)) if need_procedural else None
    sem, cast, palette = asyncio.Semaphore(3), [], []
    if plan:
        palette = plan.get("palette", [])
        async def one(item):
            async with sem: return await retry(draw_asset, f"рисунок «{item.get('id')}»", item, plan, d)
        setag("editor", "working", "Рисует персонажей и предметы")
        cast = [a for a in await asyncio.gather(*(one(it) for it in plan["assets"][:8])) if a]
    if host and need_procedural: cast.append({**host, "id": "host"})

    async def stage(s):
        async with sem:
            return await retry(make_stage, "постановка кадра", s, d, cast, palette, bool(host)) if cast else None

    setag("editor", "working", "Ставит кадры")
    stages = await asyncio.gather(*(stage(s) for s in scenes)) if need_procedural else [None] * len(scenes)
    segs, last = [], None
    for n, s in enumerate(scenes):
        setag("editor", "working", f"Анимирует сцену {n+1} из {len(scenes)}")
        st = stages[n] or last  # если кадр не поставился, берём предыдущий
        last = st or last
        nframes = max(2, round(dur * len(s["text"]) / total * FPS))
        if illustration_mode or comfy_mode or (auto_mode and has_image(n)):
            await asyncio.to_thread(illustration_frames, i, n, chunks(s["text"]), len(scenes), nframes)
        else:
            await asyncio.to_thread(scene_frames, i, n, st, cast, chunks(s["text"]), len(scenes), nframes)
        seg = f"videos/{i}_{n}.mp4"
        if not await run("-framerate", str(FPS), "-i", f"videos/{i}_{n}_%04d.jpg", "-vf", "format=yuv420p",
                         "-c:v", "libx264", "-preset", "veryfast", seg):
            raise RuntimeError("ffmpeg не смог собрать сцену")
        for x in glob.glob(f"videos/{i}_{n}_*.jpg"): os.remove(x)
        segs.append(Path(seg).name)
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
                q("delete from item_errors where item_id=?", r["id"])
                log("Монтажёр: видео готово «" + r["title"] + "»")
            except Exception as e:
                q("update items set status='render_failed' where id=?", r["id"])
                q("insert into item_errors(item_id,message,updated_at) values(?,?,current_timestamp) on conflict(item_id) do update set message=excluded.message,updated_at=current_timestamp", r["id"], str(e))
                log(f"Монтажёр: ошибка {e}. Сценарий сохранён; исправьте проблему и нажмите «Повторить монтаж».")
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
                INSIGHTS += "\nДополнительные правила аналитика: " + CFG.get("analyst_rules", "")
                log("Аналитик: отчёт передан сценаристу")
            except Exception as e:
                log(f"Аналитик: ошибка {e}")
        setag("analyst", "idle", "Следующий сбор данных позже")
        await asyncio.sleep(STATS_EVERY)

# ---- API и запуск ----
@asynccontextmanager
async def lifespan(_):
    # A terminated application must not strand an approved script in 'rendering'.
    q("update items set status='render_failed' where status='rendering'")
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
                         "text": json.loads(r["data"]).get("narration", ""),
                         "error": (q("select message from item_errors where item_id=?", r["id"])[0]["message"]
                                   if r["status"] == "render_failed" and q("select message from item_errors where item_id=?", r["id"]) else "")}
                        for r in rows if r["status"] in ("script_pending", "video_pending", "render_failed")],
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

@app.post("/api/retry_render/{item_id}")
def retry_render(item_id: int):
    row = q("select status from items where id=?", item_id)
    if not row or row[0]["status"] != "render_failed":
        raise HTTPException(409, "Задание не находится в состоянии ошибки монтажа")
    q("update items set status='script_approved' where id=? and status='render_failed'", item_id)
    return {"ok": True, "message": "Сценарий сохранён и снова передан монтажёру"}

@app.post("/api/delete_failed/{item_id}")
def delete_failed(item_id: int):
    # Only failed renders are deletable here. Never delete an approved,
    # currently rendering, published or pending video accidentally.
    with lock:
        row = db.execute("select status from items where id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Сценарий не найден")
        if row["status"] != "render_failed":
            raise HTTPException(409, "Удалять можно только сценарии с ошибкой монтажа")
        db.execute("delete from item_errors where item_id=?", (item_id,))
        db.execute("delete from items where id=? and status='render_failed'", (item_id,))
        db.commit()
    # Remove only generated scene images belonging to this failed task.
    import shutil
    shutil.rmtree(Path("visuals") / str(item_id), ignore_errors=True)
    log(f"Сценарий #{item_id} удалён пользователем")
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

class CharIn(BaseModel):
    name: str
    description: str

class Sel(BaseModel):
    id: str = ""

@app.get("/api/characters")
def characters():
    return {"items": [{"id": c["id"], "name": c["name"], "description": c["description"]} for c in load_chars()],
            "active": CFG.get("character", "")}

@app.post("/api/characters")
async def create_character(c: CharIn):
    err, asset = None, None
    for _ in range(2):
        try:
            asset = await asyncio.to_thread(design_host, c.description); break
        except Exception as e:
            err = e
    if not asset: return {"ok": False, "error": str(err)}
    cid = "c" + str(int(time.time()))
    save_chars([x for x in load_chars() if x["name"] != c.name] +
               [{"id": cid, "name": c.name, "description": c.description, "asset": asset}])
    return {"ok": True, "id": cid}

@app.post("/api/character_select")
def character_select(s: Sel):
    CFG["character"] = s.id; SF.write_text(json.dumps(CFG, ensure_ascii=False), "utf-8")
    return {"ok": True}

@app.post("/api/character_delete")
def character_delete(s: Sel):
    save_chars([x for x in load_chars() if x["id"] != s.id])
    if CFG.get("character") == s.id: character_select(Sel(id=""))
    return {"ok": True}

@app.get("/api/character_preview/{cid}")
def character_preview(cid: str):
    import io
    c = next((x for x in load_chars() if x["id"] == cid), None)
    if not c: raise HTTPException(404)
    stage = {"bg": ["#e0f2fe", "#bae6fd"], "fx": "none",
             "shapes": [{"t": "rect", "x": 0, "y": 118, "w": 100, "h": 60, "c": "#fde68a", "c2": "#fcd34d"}],
             "actors": [{"asset": "host", "x": 50, "y": 92, "size": 80}]}
    fr = compose(render_bg(stage), build_actors(stage, [{**c["asset"], "id": "host"}]), 0.5, 0.4)
    buf = io.BytesIO(); fr.resize((405, 720)).save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg")

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
    writer_rules: str = ""
    editor_rules: str = ""
    analyst_rules: str = ""
    visual_mode: str = "comfy"
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_checkpoint: str = ""
    image_steps: int = 2
    motion: str = "dynamic"

@app.get("/api/settings")
def get_settings():
    return {**{k: v for k, v in CFG.items() if k != "api_key"}, "has_key": bool(KEY),
            "has_client": os.path.exists("client_secret.json"), "has_token": os.path.exists("token.json")}

@app.post("/api/settings")
def set_settings(s: Settings):
    new = s.model_dump()
    if not new["api_key"]: new["api_key"] = KEY
    new["character"] = CFG.get("character", "")
    SF.write_text(json.dumps(new, ensure_ascii=False), "utf-8"); apply_settings(new)
    return {"ok": True}

@app.get("/api/comfy_status")
def comfy_status():
    try:
        models = comfy_checkpoints(CFG.get("comfy_url", "http://127.0.0.1:8188"))
        return {"ok": bool(models), "models": models, "message": "Готов к генерации" if models else "Не установлены модели"}
    except Exception as exc:
        return {"ok": False, "models": [], "message": str(exc)}

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
