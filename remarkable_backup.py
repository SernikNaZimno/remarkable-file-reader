#!/usr/bin/env python3
"""
Pobiera wszystkie dokumenty z reMarkable przez interfejs USB (10.11.99.1)
i zapisuje je jako pliki .rmdoc w strukturze folderów takiej jak na tablecie.

Wymagania:
  - Python 3.8+ (bez dodatkowych bibliotek)
  - tablet podłączony kablem USB
  - na tablecie: Ustawienia > Pamięć (Storage) > USB web interface = WŁ.
  - oprogramowanie tabletu 3.9+ (format .rmdoc)

Użycie:
  python remarkable_backup.py                 # zapis do ./remarkable_backup
  python remarkable_backup.py -o D:\\Backup    # własny folder docelowy
  python remarkable_backup.py --format pdf    # zamiast .rmdoc pobierz PDF-y

Uruchomienie ponowne pomija pliki, które już istnieją (użyj --overwrite,
żeby pobrać wszystko od nowa).
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "http://10.11.99.1"
TIMEOUT = 120          # sekundy na jedno żądanie (duże notatniki potrafią trwać)
RETRIES = 3
PAUSE = 0.3            # krótka przerwa między żądaniami, żeby nie zawiesić serwera tabletu


def request(url, method="GET"):
    """Wykonuje żądanie HTTP z ponawianiem; zwraca bajty odpowiedzi."""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(2 * attempt)
    raise RuntimeError(f"Nie udało się pobrać {url}: {last_err}")


def list_folder(folder_id=""):
    """Zwraca listę elementów w folderze ('' = folder główny)."""
    url = f"{BASE_URL}/documents/{folder_id}"
    data = request(url, method="POST")
    time.sleep(PAUSE)
    return json.loads(data)


def safe_name(name):
    """Usuwa znaki niedozwolone w nazwach plików (Windows/macOS/Linux)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name or "bez_nazwy"


def unique_path(path):
    """Jeśli plik o tej nazwie już powstał w tej sesji, dodaje (2), (3)..."""
    if path not in used_paths:
        used_paths.add(path)
        return path
    i = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
        i += 1


used_paths = set()
stats = {"pobrane": 0, "pominięte": 0, "błędy": 0}


def walk(folder_id, local_dir, fmt, overwrite):
    local_dir.mkdir(parents=True, exist_ok=True)
    for item in list_folder(folder_id):
        item_id = item.get("ID")
        name = safe_name(item.get("VissibleName") or item.get("VisibleName") or item_id)
        item_type = item.get("Type")

        if item_type == "CollectionType":
            print(f"[folder] {local_dir / name}")
            walk(item_id, local_dir / name, fmt, overwrite)
            continue

        target = unique_path(local_dir / f"{name}.{fmt}")
        if target.exists() and not overwrite:
            stats["pominięte"] += 1
            print(f"  pomijam (istnieje): {target}")
            continue

        print(f"  pobieram: {target}")
        try:
            data = request(f"{BASE_URL}/download/{item_id}/{fmt}")
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
            stats["pobrane"] += 1
        except Exception as e:  # noqa: BLE001 – chcemy kontynuować mimo błędu
            stats["błędy"] += 1
            print(f"  BŁĄD: {name}: {e}", file=sys.stderr)
        time.sleep(PAUSE)


def main():
    parser = argparse.ArgumentParser(description="Kopia zapasowa reMarkable przez USB.")
    parser.add_argument("-o", "--output", default="remarkable_backup",
                        help="folder docelowy (domyślnie ./remarkable_backup)")
    parser.add_argument("--format", choices=["rmdoc", "pdf"], default="rmdoc",
                        help="format plików (domyślnie rmdoc)")
    parser.add_argument("--overwrite", action="store_true",
                        help="pobierz ponownie także pliki, które już istnieją")
    args = parser.parse_args()

    try:
        list_folder("")
    except Exception:
        sys.exit("Brak połączenia z 10.11.99.1. Sprawdź kabel USB i czy "
                 "'USB web interface' jest włączony na tablecie.")

    out = Path(args.output).resolve()
    print(f"Zapisuję do: {out}\n")
    walk("", out, args.format, args.overwrite)

    print(f"\nGotowe. Pobrane: {stats['pobrane']}, pominięte: {stats['pominięte']}, "
          f"błędy: {stats['błędy']}")
    if stats["błędy"]:
        print("Uruchom skrypt ponownie – pobierze tylko brakujące pliki.")


if __name__ == "__main__":
    main()
