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
            "writer_rules": "", "editor_rules": ""}

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

FPS = 15
W, H, U = 1080, 1920, 10.8  # кадр в пикселях; холст художника 100 x 178 условных единиц, 1 единица = 10,8 px

SHAPES_DOC = """Фигуры (поздние рисуются поверх ранних; цвет "#rrggbb" или с прозрачностью "#rrggbbaa", только плоские цвета, без текста и букв):
{"t":"rect","x":0,"y":0,"w":10,"h":10,"c":"#hex","r":2}  прямоугольник (r: скругление, необязательно)
{"t":"circle","x":50,"y":50,"r":5,"c":"#hex"}
{"t":"ellipse","x":50,"y":50,"rx":8,"ry":4,"c":"#hex","rot":0}  rot в градусах, необязательно
{"t":"poly","p":[[x,y],[x,y],[x,y]],"c":"#hex"}  многоугольник с острыми углами
{"t":"blob","p":[[x,y],[x,y],...],"c":"#hex"}  замкнутая плавная форма по опорным точкам (тела, головы, облака, холмы)
{"t":"line","p":[[x,y],[x,y]],"w":1,"c":"#hex"}  линия или ломаная толщиной w"""

CAST_RULES = """Ты арт-директор мультфильма для YouTube Shorts. Создай набор повторно используемых актёров (3-7 штук): главные персонажи и ключевые предметы этого видео (например: фараон, пирамида, верблюд, облако, солнце, пальма). Каждый актёр рисуется ОДИН раз и затем используется во всех сценах, поэтому выглядит везде одинаково.
Актёр рисуется в собственном квадрате 100x100 (центр 50,50; ноги и основание примерно на y=90, не выходи за пределы квадрата). Актёр состоит из частей (parts), которые двигаются независимо: у персонажа туловище, голова, каждая рука, каждая нога, хвост (у животных); у неподвижного предмета обычно одна часть. Порядок частей = порядок рисования (задние первыми). Не больше 10 частей и 15 фигур в части.
Формат ответа, ТОЛЬКО JSON: {"assets":[{"id":"pharaoh","parts":[{"name":"body","pivot":[50,50],"anim":{"type":"none"},"shapes":[...]},{"name":"arm_r","pivot":[62,48],"anim":{"type":"swing","amp":30,"speed":0.7,"phase":0},"shapes":[...]}]}]}
pivot: точка вращения части (плечо, бедро, основание хвоста). anim.type: "none"; "swing" (качание вокруг pivot на ±amp градусов, speed колебаний в секунду, phase от 0 до 1); "sway" (покачивание вбок на amp единиц); "bob" (вверх-вниз на amp единиц); "spin" (непрерывное вращение, speed оборотов в секунду). У персонажей машут руками и шагают ногами (ноги swing в противофазе: phase 0 и 0.5), качается хвост, голова слегка; облака плывут (sway), у солнца вращаются лучи (spin)."""

STAGE_RULES = """Холст: ширина 100, высота 178 условных единиц (вертикальный кадр 9:16), (0,0) в левом верхнем углу, y растёт вниз. Нижние 40 единиц закрывают субтитры: ничего важного туда не ставь.
Верни ТОЛЬКО JSON: {"bg":["#цвет верха","#цвет низа"],"shapes":[неподвижный фон: небо, земля, холмы, вода, дальние объекты; координаты холста],"actors":[...]}. 10-30 фигур в shapes.
actors: актёры из cast, рисуются по порядку (дальние первыми). Поля: {"asset":"id из cast","x":50,"y":100,"size":40,"flip":false,"to":[70,100],"bob":{"amp":1.5,"speed":2},"show":[0,1]}
x,y: где центр актёра в начале сцены; size: ширина актёра в единицах холста; to: где он окажется в конце сцены (если двигается, иначе не указывай); flip: true, если персонаж идёт или смотрит влево; bob: подпрыгивание всего актёра (необязательно); show: доля времени сцены [от, до], когда актёр виден (так предметы появляются и исчезают).
Кадр обязан показывать то, о чём говорит фраза, с помощью актёров из cast. Всё живое движется: персонажи идут (to) и жестикулируют (их части качаются сами), предметы переезжают, облака плывут. Ставь 3-8 актёров, главный крупнее всех и ближе к центру. Один и тот же актёр обязан быть одним и тем же во всех сценах."""

def rules(kind):
    r = (CFG.get("editor_rules" if kind == "e" else "writer_rules") or "").strip()
    return f"\n\nПостоянные правила владельца канала (соблюдай строго, они важнее остальных пожеланий):\n{r}" if r else ""

def ask_json(prompt, max_tokens):
    r = Anthropic(api_key=KEY).messages.create(model=MODEL, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    txt = "".join(b.text for b in r.content if b.type == "text")
    return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])

def make_cast(d):
    scenes = "\n".join(f"{n+1}. {s['text']} [{s.get('image', '')}]" for n, s in enumerate(d["scenes"]))
    chars = "; ".join(d.get("characters", [])) or "определи сам"
    cast = ask_json(f"Видео: «{d['title']}». Стиль: {d.get('style') or 'flat cartoon'}.\nГлавные персонажи и предметы: {chars}.\n"
                    f"Сцены:\n{scenes}\n\n" + CAST_RULES + "\n" + SHAPES_DOC + rules("e"), 16000)["assets"]
    if not cast or not all(a.get("parts") for a in cast): raise RuntimeError("пустой набор актёров")
    return cast

def make_stage(s, d, cast):
    brief = "; ".join(f"{a['id']} (части: {', '.join(p.get('name', '?') for p in a['parts'])})" for a in cast)
    return ask_json(f"Ты режиссёр мультфильма для YouTube Shorts. Видео: «{d['title']}». Поставь кадр к фразе диктора: «{s['text']}»\n"
                    f"Что показать: {s.get('image') or s['text']}\nСтиль: {d.get('style') or 'flat cartoon'}\n"
                    f"Доступные актёры (cast): {brief}\n\n" + SHAPES_DOC + "\n" + STAGE_RULES + rules("e"), 8000)

def draw_shapes(d, shapes, k, ox=0.0, oy=0.0):
    import math
    P = lambda p: [(float(x) * k + ox, float(y) * k + oy) for x, y in p]

    def smooth(pts):  # срезаем углы: получается плавная форма
        for _ in range(3):
            new = []
            for j in range(len(pts)):
                a, b = pts[j], pts[(j + 1) % len(pts)]
                new += [(.75 * a[0] + .25 * b[0], .75 * a[1] + .25 * b[1]), (.25 * a[0] + .75 * b[0], .25 * a[1] + .75 * b[1])]
            pts = new
        return pts

    for sh in shapes[:150]:
        try:
            t, c = sh["t"], sh["c"]
            if t == "rect":
                x, y = float(sh["x"]) * k + ox, float(sh["y"]) * k + oy; w, h = float(sh["w"]) * k, float(sh["h"]) * k
                r = float(sh.get("r", 0)) * k
                d.rounded_rectangle((x, y, x + w, y + h), r, fill=c) if r else d.rectangle((x, y, x + w, y + h), fill=c)
            elif t == "circle":
                x, y, r = float(sh["x"]) * k + ox, float(sh["y"]) * k + oy, float(sh["r"]) * k
                d.ellipse((x - r, y - r, x + r, y + r), fill=c)
            elif t == "ellipse":
                x, y, rx, ry = float(sh["x"]) * k + ox, float(sh["y"]) * k + oy, float(sh["rx"]) * k, float(sh["ry"]) * k
                a = math.radians(float(sh.get("rot", 0)))
                d.polygon([(x + rx * math.cos(q) * math.cos(a) - ry * math.sin(q) * math.sin(a),
                            y + rx * math.cos(q) * math.sin(a) + ry * math.sin(q) * math.cos(a))
                           for q in (j * math.pi / 30 for j in range(60))], fill=c)
            elif t == "poly": d.polygon(P(sh["p"]), fill=c)
            elif t == "blob": d.polygon(smooth(P(sh["p"])), fill=c)
            elif t == "line": d.line(P(sh["p"]), fill=c, width=max(1, int(float(sh.get("w", 1)) * k)), joint="curve")
        except Exception:
            continue  # одна кривая фигура не должна ломать весь рисунок

def render_bg(stage):
    from PIL import Image, ImageDraw
    SS = 2
    bg = (stage or {}).get("bg") or ["#1e3a8a", "#0ea5e9"]
    img = Image.composite(Image.new("RGB", (W * SS, H * SS), bg[-1]), Image.new("RGB", (W * SS, H * SS), bg[0]),
                          Image.linear_gradient("L").resize((W * SS, H * SS)))
    draw_shapes(ImageDraw.Draw(img, "RGBA"), (stage or {}).get("shapes", []), U * SS)
    return img.resize((W, H), Image.LANCZOS)

def make_sprite(part, size):
    """Часть актёра как прозрачная картинка; вокруг запас в 25 единиц, чтобы часть могла качаться."""
    from PIL import Image, ImageDraw
    k = size * U / 100; side = max(8, int(150 * k)); SS = 2 if side <= 1100 else 1
    img = Image.new("RGBA", (side * SS, side * SS), (0, 0, 0, 0))
    draw_shapes(ImageDraw.Draw(img), part.get("shapes", []), k * SS, 25 * k * SS, 25 * k * SS)
    return img.resize((side, side), Image.LANCZOS) if SS > 1 else img

def build_actors(stage, cast):
    from PIL import ImageOps
    by, out = {a["id"]: a for a in cast}, []
    for ac in (stage or {}).get("actors", [])[:12]:
        a = by.get(ac.get("asset"))
        if not a: continue
        size = max(5.0, min(120.0, float(ac.get("size", 40)))); k = size * U / 100; flip = bool(ac.get("flip")); parts = []
        for p in a["parts"][:12]:
            sp, pv = make_sprite(p, size), p.get("pivot", [50, 50])
            px, py = (float(pv[0]) + 25) * k, (float(pv[1]) + 25) * k
            if flip: sp, px = ImageOps.mirror(sp), sp.width - px
            parts.append((sp, (px, py), p.get("anim") or {}))
        x, y = float(ac.get("x", 50)), float(ac.get("y", 100))
        out.append({"parts": parts, "k": k, "x": x, "y": y, "to": ac.get("to") or [x, y], "flip": flip,
                    "bob": ac.get("bob") or {}, "show": ac.get("show") or [0, 1]})
    return out

def ease(t): return t * t * (3 - 2 * t)

def compose(bg, actors, t, sec, overlay):
    import math
    from PIL import Image
    fr = bg.copy()
    for ac in actors:
        if not (ac["show"][0] <= t <= ac["show"][1]): continue
        e = ease(t)
        cx = (ac["x"] + (float(ac["to"][0]) - ac["x"]) * e) * U
        cy = (ac["y"] + (float(ac["to"][1]) - ac["y"]) * e) * U
        if ac["bob"]: cy -= abs(math.sin(math.pi * float(ac["bob"].get("speed", 1)) * sec)) * float(ac["bob"].get("amp", 1)) * U
        ox, oy = cx - 75 * ac["k"], cy - 75 * ac["k"]
        for sp, (px, py), an in ac["parts"]:
            ty, amp = an.get("type", "none"), float(an.get("amp", 0))
            ph = 2 * math.pi * (float(an.get("speed", 0)) * sec + float(an.get("phase", 0)))
            ang = dx = dy = 0.0
            if ty == "swing": ang = amp * math.sin(ph) * (-1 if ac["flip"] else 1)
            elif ty == "spin": ang = math.degrees(ph)
            elif ty == "sway": dx = amp * math.sin(ph) * U
            elif ty == "bob": dy = amp * math.sin(ph) * U
            img = sp.rotate(ang, center=(px, py), resample=Image.BICUBIC) if abs(ang) > 0.3 else sp
            fr.paste(img, (int(ox + dx), int(oy + dy)), img)
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
    shade.putalpha(Image.linear_gradient("L").resize((W, h)).point(lambda v: int(v * 0.75)))
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

def scene_frames(i, n, stage, cast, parts, total, nframes):
    """Рисует все кадры сцены: фон, актёры с движением частей и субтитры."""
    bg, actors = render_bg(stage), build_actors(stage, cast)
    acc, bounds, s = 0, [], sum(len(c) for c in parts)
    for c in parts:
        acc += len(c); bounds.append((acc / s, sub_overlay(c, n, total)))
    for f in range(nframes):
        ov = next((o for b, o in bounds if f / nframes < b), bounds[-1][1])
        compose(bg, actors, f / max(1, nframes - 1), f / FPS, ov).save(f"videos/{i}_{n}_{f:04d}.jpg", quality=90)

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

    setag("editor", "working", "Придумывает и рисует персонажей")
    cast = await retry(make_cast, "создание персонажей", d) or []
    sem = asyncio.Semaphore(3)

    async def stage(s):
        async with sem:
            return await retry(make_stage, "постановка кадра", s, d, cast) if cast else None

    stages = await asyncio.gather(*(stage(s) for s in scenes))
    segs, last = [], None
    for n, s in enumerate(scenes):
        setag("editor", "working", f"Анимирует сцену {n+1} из {len(scenes)}")
        st = stages[n] or last  # если кадр не поставился, берём предыдущий
        last = st or last
        nframes = max(2, round(dur * len(s["text"]) / total * FPS))
        await asyncio.to_thread(scene_frames, i, n, st, cast, chunks(s["text"]), len(scenes), nframes)
        seg = f"videos/{i}_{n}.mp4"
        if not await run("-framerate", str(FPS), "-i", f"videos/{i}_{n}_%04d.jpg", "-vf", "fps=30,format=yuv420p",
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
    writer_rules: str = ""
    editor_rules: str = ""

@app.get("/api/settings")
def get_settings():
    return {**{k: v for k, v in CFG.items() if k != "api_key"}, "has_key": bool(KEY),
            "has_client": os.path.exists("client_secret.json"), "has_token": os.path.exists("token.json")}

@app.post("/api/settings")
def set_settings(s: Settings):
    new = s.model_dump()
    if not new["api_key"]: new["api_key"] = KEY
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
