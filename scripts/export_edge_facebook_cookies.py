from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


CHROME_EPOCH_DELTA = 11644473600


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def crypt_unprotect_data(data: bytes) -> bytes:
    in_buffer = ctypes.create_string_buffer(data, len(data))
    in_blob = DATA_BLOB(len(data), in_buffer)
    out_blob = DATA_BLOB()

    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise OSError("CryptUnprotectData failed")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def edge_user_data_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Edge" / "User Data"


def load_edge_key(user_data_dir: Path) -> bytes:
    local_state = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8"))
    encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
    if encrypted_key.startswith(b"DPAPI"):
        encrypted_key = encrypted_key[5:]
    return crypt_unprotect_data(encrypted_key)


def decrypt_cookie(encrypted_value: bytes, key: bytes) -> str:
    if not encrypted_value:
        return ""
    if encrypted_value.startswith((b"v10", b"v11", b"v20")):
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:]
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8", errors="replace")
    return crypt_unprotect_data(encrypted_value).decode("utf-8", errors="replace")


def chrome_time_to_unix(expires_utc: int) -> float | None:
    if not expires_utc:
        return None
    return max(0, (expires_utc / 1_000_000) - CHROME_EPOCH_DELTA)


@dataclass(frozen=True)
class Candidate:
    profile: Path
    cookies_db: Path


def profile_candidates(user_data_dir: Path, profile: str | None) -> list[Candidate]:
    names = [profile] if profile else ["Default"] + [f"Profile {index}" for index in range(1, 80)]
    candidates: list[Candidate] = []
    for name in names:
        if not name:
            continue
        db = user_data_dir / name / "Network" / "Cookies"
        if db.exists():
            candidates.append(Candidate(user_data_dir / name, db))
    return candidates


def read_profile_cookies(candidate: Candidate, key: bytes) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_db = Path(temp_dir) / "Cookies"
        shutil.copy2(candidate.cookies_db, temp_db)
        connection = sqlite3.connect(temp_db)
        try:
            rows = connection.execute(
                """
                SELECT host_key, name, path, expires_utc, is_secure, is_httponly,
                       samesite, value, encrypted_value
                FROM cookies
                WHERE host_key LIKE '%facebook.com'
                   OR host_key LIKE '%.facebook.com'
                   OR host_key LIKE '%messenger.com'
                   OR host_key LIKE '%.messenger.com'
                """
            ).fetchall()
        finally:
            connection.close()

    cookies: list[dict[str, Any]] = []
    for host, name, path, expires_utc, secure, httponly, same_site, value, encrypted in rows:
        cookie_value = value or decrypt_cookie(encrypted, key)
        if not cookie_value:
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": cookie_value,
            "domain": host,
            "path": path or "/",
            "secure": bool(secure),
            "httpOnly": bool(httponly),
        }
        expires = chrome_time_to_unix(int(expires_utc or 0))
        if expires:
            cookie["expirationDate"] = expires
        if same_site == 1:
            cookie["sameSite"] = "Lax"
        elif same_site == 2:
            cookie["sameSite"] = "Strict"
        elif same_site == -1:
            cookie["sameSite"] = "None"
        cookies.append(cookie)
    return cookies


def score(cookies: list[dict[str, Any]]) -> tuple[int, int, int]:
    names = {str(cookie.get("name") or "") for cookie in cookies}
    required = int("c_user" in names) + int("xs" in names)
    useful = sum(1 for name in names if name in {"fr", "datr", "sb", "m_page_voice", "presence"})
    return (required, useful, len(cookies))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Facebook cookies from Microsoft Edge.")
    parser.add_argument("--profile", help="Edge profile folder name, e.g. 'Profile 26'.")
    parser.add_argument("--output", default=os.getenv("FB_COOKIES_PATH", "cookies.json"))
    args = parser.parse_args()

    user_data_dir = edge_user_data_dir()
    key = load_edge_key(user_data_dir)

    best: tuple[tuple[int, int, int], Candidate, list[dict[str, Any]]] | None = None
    for candidate in profile_candidates(user_data_dir, args.profile):
        try:
            cookies = read_profile_cookies(candidate, key)
        except Exception as exc:
            print(f"Skipped {candidate.profile.name}: {type(exc).__name__}")
            continue
        current_score = score(cookies)
        names = {str(cookie.get("name") or "") for cookie in cookies}
        print(
            f"{candidate.profile.name}: facebook_cookies={len(cookies)} "
            f"c_user={'c_user' in names} xs={'xs' in names}"
        )
        if best is None or current_score > best[0]:
            best = (current_score, candidate, cookies)

    if best is None or best[0][0] < 2:
        raise SystemExit("No Edge profile with both c_user and xs Facebook cookies found.")

    _, candidate, cookies = best
    output = Path(args.output)
    output.write_text(json.dumps(cookies, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    names = {str(cookie.get("name") or "") for cookie in cookies}
    print(
        f"Exported {len(cookies)} Facebook/Messenger cookies from {candidate.profile.name} "
        f"to {output}. Required cookies present: c_user={'c_user' in names}, xs={'xs' in names}."
    )


if __name__ == "__main__":
    main()
