"""Render the narrated demo video: real command output + edge-tts narration.

Usage: python demo/render.py   (needs ffmpeg, edge-tts, Pillow, restoredrill on PATH, RESTOREDRILL_PG_BIN with Postgres 17 + 15)
Every terminal scene runs its commands for real and shows the actual output;
each scene lasts exactly as long as its narration.
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import textwrap

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
OUT = os.path.join(ROOT, "demo.mp4")
W, H, FPS = 1280, 720, 24
VOICE = "en-US-AndrewNeural"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BG, FG = (24, 26, 33), (220, 223, 228)
ANSI = {"31": (255, 95, 95), "32": (95, 215, 120), "33": (240, 200, 80), "2": (130, 135, 145)}
PROMPT = "acme $ "

SCENES = [
    {"kind": "title", "title": "restoredrill", "sub": "prove your Postgres backups\nactually restore",
     "say": "This is restoredrill, a small open source command line tool. It takes your newest Postgres backup, "
            "restores it into a throwaway server, runs a few checks, and fails loudly when the backup would not "
            "come back."},
    {"kind": "bullets", "title": "The problem",
     "bullets": ["A backup you never restored is a guess",
                 "Cron jobs die halfway and nobody notices",
                 "Missing roles, extensions, version mismatches",
                 "Supabase #40625: a Postgres 17.6 dump won't load into 15",
                 "You find out during the outage"],
     "say": "Most teams have backups. Very few ever restore them. A cron job runs out of disk halfway through, a dump "
            "needs a role that does not exist on the new server, or it was made by a newer pg dump than the target. "
            "There is a Supabase issue from people whose Postgres seventeen dump would not load into fifteen. "
            "You usually find out during the outage."},
    {"kind": "term", "cmds": ["ls backups/", "restoredrill inspect backups/shop-2026-11-02.dump"],
     "say": "Here is a demo project with nightly pg dump files, made from a real Postgres seventeen database. "
            "restoredrill inspect reads the archive header without restoring anything: format, source version, "
            "tables, extensions and the roles it needs."},
    {"kind": "term", "cmds": ["restoredrill run backups/*.dump; echo \"exit code: $?\""],
     "say": "restoredrill run picks the newest file, starts a scratch Postgres on a private socket, and restores "
            "into it. This backup fails: the role that owns the tables does not exist on a fresh server. "
            "That is exactly what would bite you on a new machine, so it exits with code one and tells you how to fix it."},
    {"kind": "term", "cmds": ["cat drill.toml", "restoredrill run -c drill.toml; echo \"exit code: $?\""],
     "say": "A small config file adds the role, a freshness limit, a minimum size, row counts per table, and your own "
            "S Q L checks, like the newest order being less than a day old. Now all nine checks pass, and the scratch "
            "server is thrown away."},
    {"kind": "term", "cmds": ["restoredrill run backups/nov-01-failed-job/*.sql.gz --role app; echo \"exit code: $?\""],
     "say": "This plain S Q L backup comes from a job that ran out of disk. The file exists and is not empty, "
            "so a simple size check would pass. restoredrill sees the cut off stream and the missing dump complete "
            "footer, and only forty eight hundred rows came back instead of almost sixty five thousand."},
    {"kind": "term", "cmds": ["restoredrill run backups/*.dump --role app --target-version 15; echo \"exit code: $?\""],
     "say": "Planning a move to another server? Pass the target version. Restoring this Postgres seventeen dump "
            "into fifteen fails before you ever touch production, with the real pg restore error."},
    {"kind": "bullets", "title": "Run it every night",
     "bullets": ["GitHub Action: uses: hamarshehmhmd/restoredrill@v0.1.0",
                 "fetch = \"aws s3 cp s3://bucket/latest.dump {out}\"",
                 "Local binaries or Docker postgres:<major>",
                 "Text, JSON, Markdown summary, JUnit",
                 "Exit 0 pass · 1 backup is bad · 2 could not run"],
     "say": "Run it from cron or as a GitHub Action every night. A fetch command can pull the backup from S3 or "
            "anywhere else. It uses local Postgres binaries or a Docker image of the right major version, and writes "
            "text, jason, a markdown job summary or JUnit, with clear exit codes."},
    {"kind": "bullets", "title": "Try it",
     "bullets": ["curl -fsSL https://raw.githubusercontent.com/",
                 "   hamarshehmhmd/restoredrill/main/install.sh | sh",
                 "restoredrill inspect  ·  run  ·  init  ·  doctor",
                 "", "Python 3.9+  ·  Linux, macOS  ·  custom, directory, tar, plain, .sql.gz",
                 "AGPL-3.0 licensed (commercial licenses available)"],
     "say": "restoredrill needs Python three point nine or newer and works with custom, directory, tar and plain S Q L "
            "dumps. It is licensed under the A G P L version 3, with commercial licenses available from the author. "
            "It's on GitHub at hamarsheh m h m d slash restoredrill."},
]


async def tts(text, path):
    import edge_tts
    for attempt in range(4):  # the edge-tts service occasionally returns no audio; retry
        try:
            await edge_tts.Communicate(text, VOICE, rate="+4%").save(path)
            return
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            if attempt == 3:
                raise
            await asyncio.sleep(3)


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", path], text=True)
    return float(out.strip())


def run_cmd(cmd, cwd):
    env = dict(os.environ, NO_COLOR="")
    env.pop("NO_COLOR")
    env["PATH"] = os.path.dirname(shutil.which("restoredrill")) + os.pathsep + env["PATH"]
    full = re.sub(r"\brestoredrill (run|inspect)\b", r"restoredrill \1 --color always", cmd)
    p = subprocess.run(["bash", "-c", "shopt -s nullglob; " + full], cwd=cwd, capture_output=True, text=True, env=env)
    out = (p.stdout + p.stderr).rstrip("\n")
    # pretty-print hook JSON
    if out.startswith("{"):
        import json
        try:
            out = json.dumps(json.loads(out), indent=2)
        except ValueError:
            pass
    return out


def parse_ansi(line):
    """Return [(text, color, bold)] spans for a line with SGR codes."""
    spans, color, bold = [], FG, False
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        m = re.match(r"\x1b\[([0-9;]*)m", part)
        if m:
            for code in (m.group(1) or "0").split(";"):
                if code == "0":
                    color, bold = FG, False
                elif code == "1":
                    bold = True
                elif code in ANSI:
                    color = ANSI[code]
        elif part:
            spans.append((part, color, bold))
    return spans


class Term:
    def __init__(self):
        self.font = ImageFont.truetype(MONO, 19)
        self.bold = ImageFont.truetype(MONO_B, 19)
        self.cw = self.font.getlength("M")
        self.lh = 27
        self.cols = int((W - 80) / self.cw)

    def wrap(self, spans):
        lines, cur, n = [], [], 0
        for text, c, b in spans:
            while text:
                room = self.cols - n
                if len(text) <= room:
                    cur.append((text, c, b)); n += len(text); text = ""
                else:
                    cut = text.rfind(" ", 0, room + 1)
                    cut = cut if cut > 0 else room
                    cur.append((text[:cut], c, b)); lines.append(cur); cur, n = [], 0
                    text = "    " + text[cut:].lstrip(" ")
        lines.append(cur)
        return lines

    def frame(self, lines, cursor=False):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([20, 14, W - 20, H - 14], 12, fill=(30, 33, 41), outline=(60, 64, 76))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([40 + i * 24, 30, 54 + i * 24, 44], fill=c)
        d.text((W // 2, 37), "~/acme — bash", font=ImageFont.truetype(SANS, 15), fill=(150, 155, 165), anchor="mm")
        visible = lines[-23:]
        y = 64
        for ln in visible:
            x = 40
            for text, c, b in ln:
                d.text((x, y), text, font=self.bold if b else self.font, fill=c)
                x += self.cw * len(text)
            y += self.lh
        if cursor:
            last = visible[-1] if visible else []
            x = 40 + self.cw * sum(len(t) for t, _, _ in last)
            d.rectangle([x, y - self.lh + 3, x + self.cw - 2, y - 4], fill=FG)
        return img


def card(scene):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if scene["kind"] == "title":
        d.text((W // 2, 280), scene["title"], font=ImageFont.truetype(MONO_B, 84), fill=(95, 215, 120), anchor="mm")
        d.multiline_text((W // 2, 420), scene["sub"], font=ImageFont.truetype(SANS, 34), fill=FG, anchor="mm",
                         align="center", spacing=12)
        d.text((W // 2, 620), "github.com/hamarshehmhmd/restoredrill  ·  AGPL-3.0", font=ImageFont.truetype(SANS, 22),
               fill=(150, 155, 165), anchor="mm")
    else:
        d.text((90, 90), scene["title"], font=ImageFont.truetype(SANS_B, 52), fill=(95, 215, 120))
        f = ImageFont.truetype(SANS, 29)
        y = 200
        for b in scene["bullets"]:
            if b and not b.startswith("   "):
                d.text((90, y), "•", font=f, fill=(240, 200, 80))
            d.text((130, y), b.strip(), font=f, fill=FG)
            y += 58 if b.startswith("   ") or not b else 62
    return img


class Writer:
    def __init__(self, path, audio):
        self.p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                   "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", audio,
                                   "-af", "apad", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                                   "-shortest", path], stdin=subprocess.PIPE)
        self.n = 0

    def put(self, img, count=1):
        data = img.tobytes()
        for _ in range(count):
            self.p.stdin.write(data)
        self.n += count

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        if self.p.returncode:
            sys.exit("ffmpeg failed")


def render_scene(i, scene, workdir):
    audio = os.path.join(BUILD, f"s{i}.mp3")
    if not os.path.exists(audio):
        asyncio.run(tts(scene["say"], audio))
    total = int((duration(audio) + 0.7) * FPS)
    path = os.path.join(BUILD, f"s{i}.mp4")
    w = Writer(path, audio)
    if scene["kind"] != "term":
        w.put(card(scene), total)
        w.close()
        return path
    t = Term()
    shown = scene.get("show", scene["cmds"])
    outputs = [run_cmd(c, workdir) for c in scene["cmds"]]
    # When a scene's displayed command differs from what runs (heredoc), show
    # the display lines; the last displayed line always maps to the last command.
    steps = []
    if len(shown) != len(scene["cmds"]):
        steps = [(s, "") for s in shown[:-1]] + [(shown[-1], outputs[-1])]
    else:
        steps = list(zip(shown, outputs))
    # timing budget: typing ~ 30 chars/s, then pause, spread over the narration
    typing = sum(len(s) for s, _ in steps) / 32 * FPS
    pause = max(6, int((total - typing) / max(1, len(steps))) - 4)
    lines = []
    for cmd, out in steps:
        prompt = [(PROMPT, (95, 215, 120), True)]
        for k in range(0, len(cmd) + 1, 2):
            frame = t.frame(lines + t.wrap(prompt + [(cmd[:k], FG, False)]), cursor=True)
            w.put(frame)
        lines += t.wrap(prompt + [(cmd, FG, False)])
        for ol in out.splitlines():
            lines += t.wrap(parse_ansi(ol))
        lines.append([])
        hold = min(pause, max(0, total - w.n))
        w.put(t.frame(lines), hold)
    final = t.frame(lines + t.wrap([(PROMPT, (95, 215, 120), True)]), cursor=True)
    if w.n < total:
        w.put(final, total - w.n)
    w.close()
    return path


def main():
    os.makedirs(BUILD, exist_ok=True)
    workdir = os.environ.get("DEMO_DIR", "/home/dev/acme")
    subprocess.run([sys.executable, os.path.join(HERE, "make_demo.py"), workdir], check=True)
    parts = [render_scene(i, s, workdir) for i, s in enumerate(SCENES)]
    with open(os.path.join(BUILD, "list.txt"), "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
                    os.path.join(BUILD, "list.txt"), "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", OUT], check=True)
    with open(os.path.join(BUILD, "narration.txt"), "w") as fh:
        fh.write("\n\n".join(s["say"] for s in SCENES) + "\n")
    print(OUT, round(duration(OUT), 1), "s")


if __name__ == "__main__":
    main()
