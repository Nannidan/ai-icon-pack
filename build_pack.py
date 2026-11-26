#!/usr/bin/env python3
"""AI Art icon pack builder.

Reads apps_catalog.json (priority order), generates missing icons with
Cloudflare Workers AI FLUX-1-schnell under a hard 9,500 Neurons/UTC-day cap
(95% of the 10,000 free tier, tracked in usage_ledger.json), renders the three
local fallback masks with Pillow, and packages a signed icon-pack APK with
aapt2 + javac + d8 + zipfile + zipalign + apksigner.
"""
import argparse
import base64
import datetime
import io
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

from PIL import Image, ImageDraw, ImageFilter

HOME = Path.home()
PROJECT_DIR = Path(__file__).resolve().parent
CATALOG_FILE = PROJECT_DIR / "apps_catalog.json"
LEDGER_FILE = PROJECT_DIR / "usage_ledger.json"
RAW_DIR = PROJECT_DIR / "raw_icons"
BUILD_DIR = PROJECT_DIR / "build"
ENV_FILE = HOME / ".config" / "ai-icon-pack" / "cf.env"
SHARED_APK = Path("/mnt/shared/Download/AIArtIcons-Pixel.apk")

SDK_DIR = HOME / "android-sdk"
ANDROID_JAR = SDK_DIR / "platforms" / "android-37.1" / "android.jar"
AAPT2 = Path("/usr/local/bin/aapt2")
D8 = SDK_DIR / "cmdline-tools" / "latest" / "bin" / "d8"
ZIPALIGN = Path("/usr/bin/zipalign")
APKSIGNER = Path("/usr/bin/apksigner")

KS_DIR = HOME / ".android"
KS_FILE = KS_DIR / "aiarticons_debug.jks"
KS_PASS_FILE = KS_DIR / ".aiarticons_pass"  # chmod 600, never tracked
KS_ALIAS = "aiarticons"

PACKAGE_NAME = "com.nannidan.aiarticons"
MIN_SDK, TARGET_SDK = 31, 36

FREE_TIER_NEURONS = 10_000
HARD_CAP_NEURONS = int(FREE_TIER_NEURONS * 0.95)  # 9,500 - never exceeded, whatever cf.env says
FLUX_MODEL = "@cf/black-forest-labs/flux-1-schnell"
# Workers AI list price for flux-1-schnell: 4.80 Neurons per 512x512 tile + 9.60 per step.
# Charged per tile so a larger-than-512 output is never under-counted.
NEURONS_PER_TILE = 4.80
NEURONS_PER_STEP = 9.60
ICON_SIZE = 512

STYLE_PROMPTS = {
    "cyberpunk": (
        "glowing neon cyan #00F5FF and hot magenta #FF007F rim lighting, dark anodized obsidian metal, "
        "ultra crisp edges, cyberpunk 3D render"
    ),
    "3d": (
        "tactile 3D studio render, matte obsidian and brushed titanium surfaces, subtle soft rim light, "
        "clean geometric silhouette"
    ),
    "painted": (
        "hand-painted digital gouache and impasto, rich textured brushwork, luminous jewel-tone highlights, "
        "crisp readable silhouette"
    ),
    "retro": (
        "1980s synthwave chrome and CRT neon, warm amber, teal and magenta glow, dark reflective surface, "
        "clean iconic silhouette"
    ),
}
PROMPT_TEMPLATE = (
    "Single centered minimalist app icon emblem for the {app_name} app, depicting {subject}. "
    "The emblem sits entirely inside the central 58 percent safe zone of a square frame, "
    "surrounded by a wide uniform flat solid #050508 near-black backdrop that fills every edge. "
    "{style}. Symmetric composition, high contrast, no text, no letters, no words, no numbers, "
    "no watermark, no border, no frame, no rounded-square tile."
)

STYLE_ACCENTS = {
    "cyberpunk": ((0, 245, 255), (255, 0, 127)),
    "3d": ((160, 185, 210), (90, 110, 135)),
    "painted": ((255, 175, 65), (130, 95, 255)),
    "retro": ((255, 140, 40), (0, 220, 200)),
}

LAUNCHER_ACTIONS = (
    "org.adw.launcher.THEMES",                   # ADW, Lawnchair, most launchers
    "org.adw.launcher.icons.ACTION_PICK_ICON",   # per-icon picker
    "com.novalauncher.THEME",                    # Nova
    "com.teslacoilsw.launcher.THEME",            # Nova (legacy)
    "ginlemon.smartlauncher.THEMES",             # Smart Launcher
    "ch.deletescape.lawnchair.ICONPACK",         # Lawnchair (legacy)
    "com.anddoes.launcher.THEME",                # Apex
    "com.gau.go.launcherex.theme",               # GO
    "com.fede.launcher.THEME_ICONPACK",          # LauncherPro
)


# ----------------------------------------------------------------- config / state
def load_env():
    cfg = {
        "CF_ACCOUNT_ID": os.environ.get("CF_ACCOUNT_ID", ""),
        "CF_API_TOKEN": os.environ.get("CF_API_TOKEN", ""),
        "ICON_STYLE": os.environ.get("ICON_STYLE", "cyberpunk"),
        "MAX_DAILY_NEURONS": os.environ.get("MAX_DAILY_NEURONS", str(HARD_CAP_NEURONS)),
        "NEURONS_PER_ICON": os.environ.get("NEURONS_PER_ICON", "44"),
        "FLUX_STEPS": os.environ.get("FLUX_STEPS", "4"),
    }
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def utc_today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def load_ledger():
    if LEDGER_FILE.exists():
        try:
            return {k: int(v) for k, v in json.loads(LEDGER_FILE.read_text()).items()}
        except Exception as e:
            sys.exit(f"Refusing to run: {LEDGER_FILE} is unreadable ({e}); fix it so the cap stays enforced.")
    return {}


def save_ledger(ledger):
    tmp = LEDGER_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    tmp.replace(LEDGER_FILE)


def neurons_for(width, height, steps):
    tiles = math.ceil(width / 512) * math.ceil(height / 512)
    return math.ceil(tiles * (NEURONS_PER_TILE + NEURONS_PER_STEP * steps) - 1e-9)


# ----------------------------------------------------------------- catalog
def safe_drawable(name):
    s = re.sub(r"[^a-z0-9_]", "_", name.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "ic_" + s
    return s


def load_catalog():
    if not CATALOG_FILE.exists():
        sys.exit(f"Missing {CATALOG_FILE}. Run build_catalog.py first.")
    data = json.loads(CATALOG_FILE.read_text())
    apps = []
    for i, a in enumerate(data.get("apps", [])):
        pkg = a.get("package", "").strip()
        comp = a.get("component", "").strip()
        if not pkg or "/" not in comp:
            print(f"  skip malformed catalog entry #{i}: {a}", file=sys.stderr)
            continue
        app_name = (a.get("app_name") or pkg.split(".")[-1]).strip()
        apps.append({
            "priority": int(a.get("priority", 10**6 + i)),
            "tier": a.get("tier", ""),
            "is_user_app": bool(a.get("is_user_app")),
            "package": pkg,
            "component": comp,
            "app_name": app_name,
            "drawable": safe_drawable(a.get("drawable") or f"ic_{pkg}"),
            "prompt_subject": (a.get("prompt_subject") or f"{app_name} app icon symbol").strip(),
        })
    apps.sort(key=lambda x: x["priority"])
    return apps


def build_prompt(app, style):
    return PROMPT_TEMPLATE.format(app_name=app["app_name"], subject=app["prompt_subject"], style=STYLE_PROMPTS[style])


# ----------------------------------------------------------------- Cloudflare
class QuotaStop(Exception):
    pass


def fetch_actual_neurons(account_id, token, day):
    """Account-wide Workers AI Neurons for a UTC day via Cloudflare GraphQL analytics.

    Returns (total_neurons, flux_calls, flux_neurons) or None if analytics are unavailable.
    Analytics can lag a few minutes, so callers only ever use it to raise the local ledger.
    """
    query = (
        "query($acc: string!, $d: Date!) { viewer { accounts(filter: {accountTag: $acc}) {"
        " aiInferenceAdaptiveGroups(limit: 100, filter: {date: $d}) {"
        " count sum { totalNeurons } dimensions { modelId } } } } }"
    )
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/graphql", method="POST",
        data=json.dumps({"query": query, "variables": {"acc": account_id, "d": day}}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        groups = data["data"]["viewer"]["accounts"][0]["aiInferenceAdaptiveGroups"]
    except Exception as e:
        print(f"  (analytics unavailable: {e}; using local ledger only)", file=sys.stderr)
        return None
    total = sum(g["sum"]["totalNeurons"] or 0 for g in groups)
    flux = [g for g in groups if g["dimensions"]["modelId"] == FLUX_MODEL]
    return total, sum(g["count"] for g in flux), sum(g["sum"]["totalNeurons"] or 0 for g in flux)


def call_cloudflare_flux(account_id, token, prompt, steps):
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{FLUX_MODEL}"
    payload = json.dumps({"prompt": prompt, "steps": steps}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare API error: {data.get('errors')}")
    img = Image.open(io.BytesIO(base64.b64decode(data["result"]["image"])))
    native = img.size
    img = img.convert("RGB")
    if img.size != (ICON_SIZE, ICON_SIZE):
        img = img.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
    return img, native


def generate_icons(apps, args, cfg):
    acc, tok = cfg["CF_ACCOUNT_ID"], cfg["CF_API_TOKEN"]
    if not args.dry_run and (not acc or "PASTE_" in acc or not tok or "PASTE_" in tok):
        sys.exit(f"Set CF_ACCOUNT_ID and CF_API_TOKEN in {ENV_FILE} (or use --build-only).")

    cap = min(int(cfg["MAX_DAILY_NEURONS"]), HARD_CAP_NEURONS)
    steps = max(1, min(8, int(cfg["FLUX_STEPS"])))
    per_icon = max(int(cfg["NEURONS_PER_ICON"]), neurons_for(ICON_SIZE, ICON_SIZE, steps))

    ledger = load_ledger()
    day = utc_today()
    used = ledger.get(day, 0)

    calibrated = False

    def sync_actual():
        """Raise the local ledger to Cloudflare's own account-wide Neuron total (never lowers it)."""
        nonlocal used, per_icon, calibrated
        actual = fetch_actual_neurons(acc, tok, day)
        if actual is None:
            return
        total, fcount, fneurons = actual
        print(f"  Cloudflare analytics: {total:.1f} Neurons used today account-wide ({fcount} FLUX calls)")
        if math.ceil(total) > used:
            used = math.ceil(total)
            ledger[day] = used
            save_ledger(ledger)
        if fcount:
            per_icon = math.ceil(fneurons / fcount)
            calibrated = True

    if not args.dry_run:
        sync_actual()

    pending, seen = [], set()
    for a in apps:
        if args.tier == "user" and not a["is_user_app"]:
            continue
        if args.filter:
            hay = f"{a['package']} {a['component']} {a['app_name']}".lower()
            if args.filter.lower() not in hay:
                continue
        if a["drawable"] in seen:
            continue
        seen.add(a["drawable"])
        if (RAW_DIR / f"{a['drawable']}.png").exists():
            continue
        pending.append(a)
    if args.limit is not None:
        pending = pending[: max(0, args.limit)]

    remaining = max(0, cap - used)
    need = len(pending) * per_icon
    print(f"UTC {day}: {used}/{cap} Neurons used (95% of {FREE_TIER_NEURONS:,} free tier). "
          f"Remaining {remaining}. Estimate {per_icon}/icon x {len(pending)} pending = {need}.")
    if need <= remaining:
        print(f"Budget OK: all {len(pending)} pending icons fit under today's cap.")
    else:
        print(f"Budget WARNING: only ~{remaining // per_icon} of {len(pending)} fit today; "
              f"generating in priority order, rerun after 00:00 UTC for the rest.")

    if args.dry_run:
        for a in pending:
            print(f"  #{a['priority']} {a['drawable']}: {build_prompt(a, args.style)}")
        return 0

    made, fails = 0, 0
    for idx, a in enumerate(pending, 1):
        if used + per_icon > cap:
            print(f"Cap reached ({used}/{cap}); stopping before {a['drawable']}.")
            break
        out_png = RAW_DIR / f"{a['drawable']}.png"
        print(f"[{idx}/{len(pending)}] #{a['priority']} {a['app_name']} -> {a['drawable']} ({a['prompt_subject']})")
        img = None
        for attempt in range(3):
            try:
                img, native = call_cloudflare_flux(acc, tok, build_prompt(a, args.style), steps)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")
                print(f"  HTTP {e.code}: {body[:300]}", file=sys.stderr)
                if e.code in (401, 403) or "4006" in body or "daily free allocation" in body.lower():
                    save_ledger(ledger)
                    raise QuotaStop(f"Stopping: HTTP {e.code} (auth or daily allocation).")
                if e.code == 429 or e.code >= 500:
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            except (urllib.error.URLError, TimeoutError) as e:
                # The request may have run server-side; charge it so the cap stays conservative.
                used += per_icon
                ledger[day] = used
                save_ledger(ledger)
                print(f"  network error ({e}); charged {per_icon} conservatively", file=sys.stderr)
                if used + per_icon > cap:
                    break
                time.sleep(3)
            except Exception as e:
                print(f"  error: {e}", file=sys.stderr)
                break
        if img is None:
            fails += 1
            if fails >= 3:
                print("3 consecutive failures; stopping batch.", file=sys.stderr)
                break
            continue
        fails = 0
        cost = per_icon if calibrated else max(per_icon, neurons_for(native[0], native[1], steps))
        if cost > per_icon:
            print(f"  native output {native[0]}x{native[1]} -> re-estimating at {cost} Neurons/icon")
            per_icon = cost
        img.save(out_png, "PNG", optimize=True)
        used += cost
        ledger[day] = used
        save_ledger(ledger)
        made += 1
        if made % 10 == 0:
            sync_actual()
        time.sleep(0.5)

    print(f"Batch complete: {made} generated. UTC {day} total {used}/{cap} Neurons.")
    return made


# ----------------------------------------------------------------- resources
def generate_fallback_masks(drawable_dir, style):
    """iconback / iconmask / iconupon, 512x512, rendered locally (0 Neurons)."""
    c1, c2 = STYLE_ACCENTS.get(style, STYLE_ACCENTS["cyberpunk"])
    size = ICON_SIZE

    back = Image.new("RGBA", (size, size), (5, 5, 8, 255))
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.rounded_rectangle([26, 26, size - 26, size - 26], radius=112, outline=(*c1, 140), width=8)
    gdraw.rounded_rectangle([38, 38, size - 38, size - 38], radius=102, outline=(*c2, 110), width=5)
    back = Image.alpha_composite(back, glow.filter(ImageFilter.GaussianBlur(10)))
    ImageDraw.Draw(back).rounded_rectangle([28, 28, size - 28, size - 28], radius=110, outline=(*c1, 190), width=3)
    back.convert("RGB").save(drawable_dir / "iconback.png", "PNG")

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([36, 36, size - 36, size - 36], radius=104, fill=255)
    mask.save(drawable_dir / "iconmask.png", "PNG")

    upon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    udraw = ImageDraw.Draw(upon)
    udraw.rounded_rectangle([28, 28, size - 28, size - 28], radius=110, outline=(255, 255, 255, 38), width=2)
    udraw.arc([44, 40, size - 44, 180], start=200, end=340, fill=(*c1, 45), width=4)
    upon.save(drawable_dir / "iconupon.png", "PNG")


def write_resources(apps, style, res_dir, assets_dir):
    drawable_dir = res_dir / "drawable-nodpi"
    xml_dir = res_dir / "xml"
    values_dir = res_dir / "values"
    for d in (drawable_dir, xml_dir, values_dir, assets_dir):
        d.mkdir(parents=True, exist_ok=True)

    generate_fallback_masks(drawable_dir, style)

    mapped, drawables = [], []
    for a in apps:
        src = RAW_DIR / f"{a['drawable']}.png"
        if not src.exists():
            continue
        if a["drawable"] not in drawables:
            shutil.copy2(src, drawable_dir / f"{a['drawable']}.png")
            drawables.append(a["drawable"])
        mapped.append(a)

    launcher_src = drawable_dir / (f"{drawables[0]}.png" if drawables else "iconback.png")
    shutil.copy2(launcher_src, drawable_dir / "ic_launcher.png")

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<resources>",
        '    <iconback img1="iconback" />',
        '    <iconmask img1="iconmask" />',
        '    <iconupon img1="iconupon" />',
        '    <scale factor="0.72" />',
    ]
    for a in mapped:
        lines.append(f"    <!-- #{a['priority']} {a['app_name']} -->")
        lines.append(f"    <item component={quoteattr('ComponentInfo{' + a['component'] + '}')} "
                     f"drawable={quoteattr(a['drawable'])} />")
    lines.append("</resources>")
    appfilter = "\n".join(lines) + "\n"
    (xml_dir / "appfilter.xml").write_text(appfilter)
    (assets_dir / "appfilter.xml").write_text(appfilter)

    dxml = ['<?xml version="1.0" encoding="utf-8"?>', "<resources>", "    <version>1</version>",
            '    <category title="AI Art" />']
    dxml += [f"    <item drawable={quoteattr(d)} />" for d in drawables]
    dxml.append("</resources>")
    (xml_dir / "drawable.xml").write_text("\n".join(dxml) + "\n")
    (assets_dir / "drawable.xml").write_text("\n".join(dxml) + "\n")

    (values_dir / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        f'    <string name="app_name">AI Art Icons ({style.title()})</string>\n</resources>\n'
    )
    return mapped, drawables


def write_manifest(path, version_code):
    filters = "".join(
        "            <intent-filter>\n"
        f'                <action android:name="{act}" />\n'
        '                <category android:name="android.intent.category.DEFAULT" />\n'
        "            </intent-filter>\n"
        for act in LAUNCHER_ACTIONS
    )
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
        f'    package="{PACKAGE_NAME}"\n'
        f'    android:versionCode="{version_code}"\n'
        '    android:versionName="1.0">\n'
        '    <application android:label="@string/app_name" android:icon="@drawable/ic_launcher"\n'
        '        android:hasCode="true" android:allowBackup="false">\n'
        '        <activity android:name=".MainActivity" android:exported="true">\n'
        "            <intent-filter>\n"
        '                <action android:name="android.intent.action.MAIN" />\n'
        '                <category android:name="android.intent.category.LAUNCHER" />\n'
        "            </intent-filter>\n"
        "            <intent-filter>\n"
        '                <action android:name="android.intent.action.MAIN" />\n'
        '                <category android:name="com.anddoes.launcher.THEME" />\n'
        "            </intent-filter>\n"
        f"{filters}"
        "        </activity>\n"
        "    </application>\n"
        "</manifest>\n"
    )


def write_activity(src_dir, style, count):
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "MainActivity.java").write_text(
        f"package {PACKAGE_NAME};\n"
        "import android.app.Activity;\n"
        "import android.graphics.Color;\n"
        "import android.os.Bundle;\n"
        "import android.view.Gravity;\n"
        "import android.widget.TextView;\n"
        "public class MainActivity extends Activity {\n"
        "    @Override protected void onCreate(Bundle b) {\n"
        "        super.onCreate(b);\n"
        "        TextView tv = new TextView(this);\n"
        "        tv.setBackgroundColor(Color.BLACK);\n"
        "        tv.setTextColor(Color.rgb(0, 245, 255));\n"
        "        tv.setGravity(Gravity.CENTER);\n"
        "        tv.setTextSize(16f);\n"
        f'        tv.setText("AI Art Icon Pack ({style.title()})\\nMapped components: {count}\\n'
        'Apply it from your launcher\'s icon pack settings.");\n'
        "        setContentView(tv);\n"
        "    }\n"
        "}\n"
    )


# ----------------------------------------------------------------- packaging
def run(cmd, **kw):
    print("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def ensure_keystore():
    KS_DIR.mkdir(parents=True, exist_ok=True)
    if KS_FILE.exists():
        if not KS_PASS_FILE.exists():
            sys.exit(f"{KS_FILE} exists but {KS_PASS_FILE} is missing; write its password there (chmod 600).")
        return
    old = os.umask(0o077)
    try:
        KS_PASS_FILE.write_text(secrets.token_urlsafe(18))
    finally:
        os.umask(old)
    run(["keytool", "-genkeypair", "-keystore", KS_FILE, "-storetype", "PKCS12",
         "-storepass:file", KS_PASS_FILE, "-alias", KS_ALIAS,
         "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
         "-dname", "CN=AIArtIcons,OU=Android,O=Nannidan,L=Local,ST=ON,C=CA"],
        stdout=subprocess.DEVNULL)


def verify_apk(apk):
    res = subprocess.run([str(APKSIGNER), "verify", "--verbose", str(apk)], capture_output=True, text=True)
    out = (res.stdout + res.stderr).strip()
    print(f"apksigner verify --verbose {apk}\n{out}")
    if res.returncode != 0 or not out.startswith("Verifies"):
        sys.exit(f"VERIFY_FAILED: {apk}")


def build_apk(apps, style):
    for tool in (AAPT2, D8, ZIPALIGN, APKSIGNER, ANDROID_JAR):
        if not tool.exists():
            sys.exit(f"Missing toolchain component: {tool}")
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    res_dir, assets_dir = BUILD_DIR / "res", BUILD_DIR / "assets"
    gen_dir, cls_dir, dex_dir, apk_dir = (BUILD_DIR / n for n in ("gen", "classes", "dex", "apk"))
    for d in (gen_dir, cls_dir, dex_dir, apk_dir):
        d.mkdir(parents=True, exist_ok=True)

    mapped, drawables = write_resources(apps, style, res_dir, assets_dir)
    manifest = BUILD_DIR / "AndroidManifest.xml"
    write_manifest(manifest, int(time.time()) // 60)
    write_activity(BUILD_DIR / "src" / PACKAGE_NAME.replace(".", "/"), style, len(mapped))

    res_zip = BUILD_DIR / "res.zip"
    unaligned, aligned = apk_dir / "unaligned.apk", apk_dir / "aligned.apk"
    signed = BUILD_DIR / "AIArtIcons-Pixel.apk"

    run([AAPT2, "compile", "--dir", res_dir, "-o", res_zip])
    run([AAPT2, "link", "-I", ANDROID_JAR, "--manifest", manifest, "-A", assets_dir,
         "--java", gen_dir, "--min-sdk-version", MIN_SDK, "--target-sdk-version", TARGET_SDK,
         "-o", unaligned, res_zip])

    java_files = sorted(BUILD_DIR.joinpath("src").rglob("*.java")) + sorted(gen_dir.rglob("*.java"))
    run(["javac", "--release", "11", "-nowarn", "-cp", ANDROID_JAR, "-d", cls_dir, *java_files])
    run([D8, "--release", "--min-api", MIN_SDK, "--lib", ANDROID_JAR, "--output", dex_dir,
         *sorted(cls_dir.rglob("*.class"))])

    dex_files = sorted(dex_dir.glob("classes*.dex"))
    if not dex_files:
        sys.exit("d8 produced no classes*.dex")
    with zipfile.ZipFile(unaligned, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        existing = set(zf.namelist())
        for dex in dex_files:
            if dex.name in existing:
                sys.exit(f"{dex.name} already present in {unaligned}")
            zf.write(dex, arcname=dex.name)
            print(f"  zipfile: injected {dex.name} ({dex.stat().st_size} bytes)")

    run([ZIPALIGN, "-f", "-p", "4", unaligned, aligned])
    run([ZIPALIGN, "-c", "-p", "4", aligned])
    ensure_keystore()
    run([APKSIGNER, "sign", "--ks", KS_FILE, "--ks-key-alias", KS_ALIAS,
         "--ks-pass", f"file:{KS_PASS_FILE}",  # PKCS12: key password == store password
         "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
         "--v3-signing-enabled", "true", "--v4-signing-enabled", "false",
         "--out", signed, aligned])
    verify_apk(signed)

    if not SHARED_APK.parent.is_dir():
        sys.exit(f"{SHARED_APK.parent} not mounted; signed APK left at {signed}")
    shutil.copy2(signed, SHARED_APK)
    verify_apk(SHARED_APK)
    print(f"APK_OK: {SHARED_APK} ({SHARED_APK.stat().st_size} bytes) - "
          f"{len(mapped)} components -> {len(drawables)} drawables + 3 fallback masks")
    unmapped = [a for a in apps if not (RAW_DIR / f"{a['drawable']}.png").exists()]
    if unmapped:
        print(f"  {len(unmapped)} catalog components have no icon yet (launcher fallback masks apply).")


# ----------------------------------------------------------------- main
def main():
    cfg = load_env()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--style", default=cfg.get("ICON_STYLE", "cyberpunk"), choices=list(STYLE_PROMPTS))
    p.add_argument("--tier", default="all", choices=("all", "user"), help="user = only is_user_app entries")
    p.add_argument("--filter", default="", help="substring match on package / component / app_name")
    p.add_argument("--limit", type=int, default=None, help="max new icons to generate this run")
    p.add_argument("--build-only", action="store_true", help="no API calls; package existing icons")
    p.add_argument("--dry-run", action="store_true", help="print budget plan and prompts; no API calls, no build")
    args = p.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    apps = load_catalog()
    print(f"Loaded {len(apps)} components from {CATALOG_FILE.name} (priority order).")

    if not args.build_only:
        try:
            generate_icons(apps, args, cfg)
        except QuotaStop as e:
            print(e, file=sys.stderr)
        if args.dry_run:
            return
    build_apk(apps, args.style)


if __name__ == "__main__":
    main()
