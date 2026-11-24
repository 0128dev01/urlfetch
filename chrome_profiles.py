from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

CHROME_EXE_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Google"
    / "Chrome"
    / "Application"
    / "chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Google"
    / "Chrome"
    / "Application"
    / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Google"
    / "Chrome"
    / "Application"
    / "chrome.exe",
]


def chrome_user_data_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"


def find_chrome_exe() -> Path:
    for path in CHROME_EXE_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("Google Chrome was not found on this PC.")


def _has_jobright_session(profile_dir: Path) -> bool | None:
    cookie_path = profile_dir / "Network" / "Cookies"
    if not cookie_path.exists():
        cookie_path = profile_dir / "Cookies"
    if not cookie_path.exists():
        return False
    try:
        data = cookie_path.read_bytes()
    except OSError:
        return None
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    try:
        tmp.write_bytes(data)
        con = sqlite3.connect(str(tmp))
        try:
            row = con.execute(
                "SELECT 1 FROM cookies "
                "WHERE name='SESSION_ID' AND host_key LIKE '%jobright%' LIMIT 1"
            ).fetchone()
            return bool(row)
        finally:
            con.close()
    except OSError:
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except OSError:
            pass


def _chrome_color(value: object) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "#5f6368"
    number &= 0xFFFFFFFF
    red = (number >> 16) & 255
    green = (number >> 8) & 255
    blue = number & 255
    return f"#{red:02x}{green:02x}{blue:02x}"


_AVATAR_FILES = {
    27: "avatar_origami_cat.png",
    28: "avatar_origami_corgi.png",
    29: "avatar_origami_dragon.png",
    30: "avatar_origami_elephant.png",
    31: "avatar_origami_fox.png",
    32: "avatar_origami_monkey.png",
    33: "avatar_origami_panda.png",
    34: "avatar_origami_penguin.png",
    35: "avatar_origami_pinkbutterfly.png",
    36: "avatar_origami_rabbit.png",
    37: "avatar_origami_unicorn.png",
    38: "avatar_illustration_basketball.png",
    39: "avatar_illustration_bike.png",
    40: "avatar_illustration_bird.png",
    41: "avatar_illustration_cheese.png",
    42: "avatar_illustration_football.png",
    43: "avatar_illustration_ramen.png",
    44: "avatar_illustration_sunglasses.png",
    45: "avatar_illustration_sushi.png",
    46: "avatar_illustration_tamagotchi.png",
    47: "avatar_illustration_vinyl.png",
    48: "avatar_abstract_avocado.png",
    49: "avatar_abstract_cappuccino.png",
    50: "avatar_abstract_icecream.png",
    51: "avatar_abstract_icewater.png",
    52: "avatar_abstract_melon.png",
    53: "avatar_abstract_onigiri.png",
    54: "avatar_abstract_pizza.png",
    55: "avatar_abstract_sandwich.png",
}


def _profile_meta(directory: str) -> dict:
    directory = Path(directory or "").name
    try:
        payload = json.loads((chrome_user_data_dir() / "Local State").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return ((payload.get("profile") or {}).get("info_cache") or {}).get(directory) or {}


def _avatar_icon_index(icon: object) -> int | None:
    text = str(icon or "")
    marker = "IDR_PROFILE_AVATAR_"
    if marker not in text:
        return None
    tail = text.rsplit(marker, 1)[-1]
    return int(tail) if tail.isdigit() else None


def profile_picture_path(directory: str) -> Path | None:
    directory = Path(directory or "").name
    if not directory or directory in {".", ".."}:
        return None
    folder = chrome_user_data_dir() / directory
    meta = _profile_meta(directory)
    gaia_name = str(meta.get("gaia_picture_file_name") or "Google Profile Picture.png")
    gaia_path = folder / Path(gaia_name).name
    if meta.get("use_gaia_picture") is True and gaia_path.is_file():
        return gaia_path
    index = _avatar_icon_index(meta.get("avatar_icon"))
    if index is not None and index != 26:
        filename = _AVATAR_FILES.get(index)
        if filename:
            avatar = chrome_user_data_dir() / "Avatars" / filename
            if avatar.is_file():
                return avatar
    for name in (gaia_name, "Google Profile Picture.png", "Google Profile Picture.jpg"):
        path = folder / Path(name).name
        if path.is_file():
            return path
    return None


def profile_display_name(directory: str) -> str:
    directory = Path(directory or "").name
    return str(_profile_meta(directory).get("name") or directory)


def profile_picture_color(directory: str) -> str:
    info = _profile_meta(directory)
    return _chrome_color(info.get("profile_highlight_color") or info.get("default_avatar_fill_color"))


def list_profiles() -> list[dict]:
    user_data = chrome_user_data_dir()
    local_state = user_data / "Local State"
    if not local_state.exists():
        raise FileNotFoundError("Chrome Local State was not found.")

    payload = json.loads(local_state.read_text(encoding="utf-8"))
    info = payload.get("profile", {}).get("info_cache", {})
    last_used = payload.get("profile", {}).get("last_used", "")

    profiles = []
    for directory, meta in info.items():
        profile_dir = user_data / directory
        profiles.append(
            {
                "directory": directory,
                "name": meta.get("name") or directory,
                "email": meta.get("user_name") or meta.get("gaia_name") or "",
                "last_used": directory == last_used,
                "jobright_cookie": _has_jobright_session(profile_dir),
                "picture": f"/api/profiles/picture?dir={directory}",
                "has_photo": profile_picture_path(directory) is not None,
            }
        )
    profiles.sort(key=lambda item: (not item["last_used"], item["name"].lower()))
    return profiles


def open_in_profile(directory: str, url: str) -> None:
    open_many_in_profile(directory, [url])


def open_many_in_profile(directory: str, urls: list[str]) -> None:
    if not directory:
        raise ValueError("Select a Chrome profile first.")
    links = [url for url in urls if url]
    if not links:
        raise ValueError("This job has no company link.")
    chrome = find_chrome_exe()
    subprocess.Popen(
        [str(chrome), f"--profile-directory={directory}", *links],
        close_fds=True,
    )
