#!/usr/bin/env python3
import json
import re
import shutil
import sys
from pathlib import Path

RAW_DUMP = Path("/mnt/shared/Download/raw_pm_dump.txt")
OUT_LOCAL = Path.home() / "projects" / "ai-icon-pack" / "apps_catalog.json"
OUT_SHARED = Path("/mnt/shared/Download/apps_catalog.json")

APP_DICTIONARY = {
    "com.android.chrome": ("Chrome", "web browser globe"),
    "org.mozilla.fenix": ("Firefox Nightly", "glowing fox orbiting globe"),
    "org.mozilla.firefox": ("Firefox", "glowing fox orbiting globe"),
    "com.google.android.youtube": ("YouTube", "rounded video play button"),
    "com.morphe.android.youtube": ("YouTube Morphe", "cyberpunk video play prism"),
    "app.revanced.android.youtube": ("YouTube ReVanced", "cyberpunk video play prism"),
    "com.google.android.apps.youtube.music": ("YouTube Music", "vinyl disc with play triangle"),
    "com.google.android.gm": ("Gmail", "sleek M mail envelope"),
    "com.google.android.apps.maps": ("Google Maps", "3D navigation location pin"),
    "com.google.android.apps.photos": ("Google Photos", "four-blade luminous pinwheel"),
    "com.google.android.GoogleCamera": ("Pixel Camera", "precision multi-element camera lens"),
    "com.android.settings": ("Settings", "precision mechanical gear cog"),
    "com.google.android.dialer": ("Phone", "ergonomic telephone handset"),
    "com.google.android.apps.messaging": ("Messages", "glowing chat speech bubble"),
    "com.google.android.contacts": ("Contacts", "minimalist user silhouette badge"),
    "com.google.android.deskclock": ("Clock", "minimalist analog clock face"),
    "com.google.android.calculator": ("Calculator", "plus minus multiply divide grid"),
    "com.google.android.calendar": ("Calendar", "sleek calendar tile"),
    "com.google.android.keep": ("Google Keep", "glowing lightbulb note"),
    "com.google.android.apps.docs": ("Google Drive", "geometric three-sided prism triangle"),
    "com.google.android.apps.nbu.files": ("Files by Google", "sleek digital folder"),
    "com.google.android.apps.walletnfcrel": ("Google Wallet", "layered contactless payment cards"),
    "com.google.android.apps.recorder": ("Pixel Recorder", "audio waveform microphone"),
    "com.google.android.apps.weather": ("Pixel Weather", "radiant sun and cloud emblem"),
    "com.android.vending": ("Play Store", "faceted play triangle gem"),
    "com.termux": ("Termux", "terminal command prompt chevron and cursor"),
    "moe.shizuku.privileged.api": ("Shizuku", "geometric origami paper crane"),
    "com.tailscale.ipn": ("Tailscale", "3x3 glowing mesh network dots"),
    "ginlemon.flowerfree": ("Smart Launcher", "minimalist geometric flower prism"),
    "com.teslacoilsw.launcher": ("Nova Launcher", "luminous star house emblem"),
    "app.lawnchair": ("Lawnchair", "minimalist modern lounge chair silhouette"),
    "com.discord": ("Discord", "futuristic game controller visor"),
    "com.spotify.music": ("Spotify", "curved acoustic soundwaves"),
    "com.reddit.frontpage": ("Reddit", "futuristic antenna mascot head"),
    "org.telegram.messenger": ("Telegram", "sleek origami paper airplane"),
    "com.whatsapp": ("WhatsApp", "speech bubble with phone handset"),
    "com.instagram.android": ("Instagram", "minimalist square camera aperture"),
    "com.github.android": ("GitHub", "silhouette cat octopus emblem"),
    "org.jellyfin.mobile": ("Jellyfin", "crystalline water drop triangle"),
    "com.adguard.android": ("AdGuard", "cyber shield with checkmark"),
    "net.dinglisch.android.taskerm": ("Tasker", "interlocking automation lightning gears"),
    "com.llamalab.automate": ("Automate", "connected flowchart nodes"),
    "org.kustom.widget": ("KWGT", "layered modular UI widget blocks"),
    "com.mixplorer": ("MiXplorer", "sleek dual-tone file explorer folder"),
    "com.roblox.client": ("Roblox", "tilted hollow square cube"),
    "com.epicgames.fortnite": ("Fortnite", "bold storm llama silhouette"),
    "com.supercell.brawlstars": ("Brawl Stars", "stylized skull star emblem"),
    "com.jagex.oldscape.android": ("Old School RuneScape", "medieval crossed sword and shield"),
    "com.mojang.minecraftpe": ("Minecraft", "isometric pixel grass block"),
    "com.riotgames.league.wildrift": ("Wild Rift", "crossed arcane blades"),
    "com.openai.chatgpt": ("ChatGPT", "interwoven hexagonal neural knot"),
    "com.anthropic.claude": ("Claude", "radiant starburst asterisk"),
    "com.google.android.apps.bard": ("Gemini", "four-point sparkling star"),
    "com.wealthsimple": ("Wealthsimple", "bold minimalist W monogram"),
    "ca.koodomobile.selfserve": ("Koodo", "stylized speech spark emblem"),
    "com.fitbit.FitbitMobile": ("Fitbit", "diamond constellation of glowing dots"),
}

PRIORITY_DOCK = [
    "com.google.android.dialer",
    "com.google.android.apps.messaging",
    "com.android.chrome",
    "org.mozilla.fenix",
    "com.google.android.GoogleCamera",
    "com.google.android.apps.photos",
    "com.morphe.android.youtube",
    "com.google.android.youtube",
    "com.google.android.gm",
    "com.google.android.apps.maps",
    "com.termux",
    "moe.shizuku.privileged.api",
    "com.discord",
    "com.spotify.music",
    "com.android.settings",
    "com.android.vending",
]

NOISE_TOKENS = {
    "com", "org", "net", "io", "app", "apps", "android", "google", "mobile",
    "client", "free", "pro", "lite", "main", "ui", "launcher", "activity",
    "home", "splash", "alias", "privileged", "api", "selfserve", "frontpage"
}


def sanitize_drawable(pkg: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", pkg.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "app_" + s
    return f"ic_{s}"


def infer_name_and_subject(pkg: str, raw_label: str):
    if pkg in APP_DICTIONARY:
        return APP_DICTIONARY[pkg]
    clean_label = raw_label.strip() if raw_label and raw_label.lower() != "null" else ""
    parts = [p for p in re.split(r"[._-]+", pkg) if p.lower() not in NOISE_TOKENS and len(p) > 1]
    fallback_name = " ".join(p.capitalize() for p in parts[-2:]) if parts else pkg.split(".")[-1].capitalize()
    app_name = clean_label if clean_label else fallback_name
    subject = f"{app_name} app icon symbol"
    return app_name, subject


def main():
    if not RAW_DUMP.exists():
        print(f"Missing {RAW_DUMP}. Run the Termux export step first.", file=sys.stderr)
        sys.exit(1)

    text = RAW_DUMP.read_text(encoding="utf-8", errors="ignore")
    sections = {"USER_PACKAGES": "", "LAUNCHER_ACTIVITIES": "", "SHORTCUT_LABELS": ""}
    current = None
    for line in text.splitlines():
        if line.startswith("===") and line.endswith("==="):
            key = line.strip("=")
            if key in sections:
                current = key
                continue
        if current:
            sections[current] += line + "\n"

    user_pkgs = set()
    for line in sections["USER_PACKAGES"].splitlines():
        line = line.strip()
        if line.startswith("package:"):
            user_pkgs.add(line.split(":", 1)[1].strip())

    shortcut_labels = {}
    cur_pkg = None
    for line in sections["SHORTCUT_LABELS"].splitlines():
        m_pkg = re.search(r"Package:\s*([A-Za-z0-9_.-]+)", line)
        if m_pkg:
            cur_pkg = m_pkg.group(1)
        m_lbl = re.search(r"(?:shortLabel|longLabel|title)=([^,\}\n]+)", line)
        if cur_pkg and m_lbl and cur_pkg not in shortcut_labels:
            val = m_lbl.group(1).strip().strip('"')
            if val and val != "null" and not val.startswith("@"):
                shortcut_labels[cur_pkg] = val

    entries = []
    seen_components = set()
    cur_comp = None
    cur_pkg = None
    cur_cls = None
    cur_label = ""
    cur_app_label = ""
    in_app_info = False

    def flush():
        nonlocal cur_comp, cur_pkg, cur_cls, cur_label
        if not cur_pkg or not cur_cls:
            return
        cls = (cur_pkg + cur_cls) if cur_cls.startswith(".") else cur_cls
        comp = f"{cur_pkg}/{cls}"
        if comp in seen_components:
            return
        seen_components.add(comp)

        # Shortcut labels are shortcut names (e.g. "Favourites"), not app names; don't use them here.
        best_raw_label = cur_label or cur_app_label
        app_name, subject = infer_name_and_subject(cur_pkg, best_raw_label)
        is_user = cur_pkg in user_pkgs
        is_google = cur_pkg.startswith(("com.google.", "com.android."))

        if cur_pkg in PRIORITY_DOCK:
            tier = "1_dock"
            rank = (0, PRIORITY_DOCK.index(cur_pkg), comp)
        elif is_user:
            tier = "2_user"
            rank = (1, 0, app_name.lower(), comp)
        elif is_google:
            tier = "3_google"
            rank = (2, 0, app_name.lower(), comp)
        else:
            tier = "4_system"
            rank = (3, 0, app_name.lower(), comp)

        entries.append({
            "rank": rank,
            "tier": tier,
            "is_user_app": is_user,
            "package": cur_pkg,
            "activity": cls,
            "component": comp,
            "app_name": app_name,
            "drawable": sanitize_drawable(cur_pkg),
            "prompt_subject": subject,
        })

    for line in sections["LAUNCHER_ACTIVITIES"].splitlines():
        m_res = re.search(r"ResolveInfo\{[^\s]+\s+([A-Za-z0-9_.$]+)/(\.?[A-Za-z0-9_.$]+)", line)
        if m_res:
            flush()
            cur_pkg = m_res.group(1)
            cur_cls = m_res.group(2)
            cur_comp = f"{cur_pkg}/{cur_cls}"
            cur_label = cur_app_label = ""
            in_app_info = False
            continue
        s = line.strip()
        # `dumpsys`/`cmd package query-activities` blocks: "Activity #N:" -> ActivityInfo -> ApplicationInfo
        if re.match(r"Activity #\d+:", s):
            flush()
            cur_pkg = cur_cls = cur_comp = None
            cur_label = cur_app_label = ""
            in_app_info = False
            continue
        if s == "ApplicationInfo:":
            in_app_info = True
            continue
        m_lbl = re.search(r"nonLocalizedLabel=(.*?)\s+icon=", s)
        if m_lbl:
            val = m_lbl.group(1).strip()
            if val and val != "null":
                if in_app_info:
                    cur_app_label = cur_app_label or val
                else:
                    cur_label = cur_label or val
            continue
        if in_app_info:
            continue  # ApplicationInfo name= is the Application class, not the launcher activity
        if s.startswith("packageName=") and not cur_pkg:
            cur_pkg = s.split("=", 1)[1].strip()
        elif s.startswith("name=") and not cur_cls:
            cur_cls = s.split("=", 1)[1].strip()
    flush()

    entries.sort(key=lambda x: x["rank"])
    for idx, item in enumerate(entries, start=1):
        del item["rank"]
        item["priority"] = idx

    catalog = {
        "total_components": len(entries),
        "user_app_count": sum(1 for e in entries if e["is_user_app"]),
        "apps": entries,
    }

    OUT_LOCAL.write_text(json.dumps(catalog, indent=2))
    if OUT_SHARED.parent.exists():
        shutil.copy2(OUT_LOCAL, OUT_SHARED)

    print(f"Built {OUT_LOCAL}: {catalog['total_components']} total launcher components ({catalog['user_app_count']} user apps).")
    print("\nTop 25 prioritized apps for agent:")
    for e in entries[:25]:
        print(f"  #{e['priority']:02d} [{e['tier']}] {e['app_name']} -> {e['component']} ({e['prompt_subject']})")


if __name__ == "__main__":
    main()
