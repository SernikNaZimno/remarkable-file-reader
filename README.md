# Czytnik reMarkable

Program do przeglądania notatek z tabletu reMarkable na komputerze, bez chmury i bez aplikacji reMarkable.

## Co otwiera

- **`.rmdoc`**: pliki pobrane przez interfejs USB, np. dołączonym skryptem `remarkable_backup.py`
- **surowe dane z tabletu** (folder z plikami `<uuid>.metadata`, `<uuid>.content`, `<uuid>/…rm`), np. skopiowane przez SSH z `/home/root/.local/share/remarkable/xochitl`
- **pojedyncze strony `.rm`**

Obsługiwany jest format stron v6 (oprogramowanie tabletu 3.0 i nowsze), w tym reMarkable 2 i Paper Pro.
Wyświetla notatniki, PDF-y i EPUB-y razem z notatkami: wszystkie narzędzia, kolory, zakreślacz, tekst wpisany z klawiatury, szablony linii, kratki i kropek (przybliżone) oraz długie, przewijane strony.

## Uruchomienie

Potrzebny jest Python 3.9+ i PyQt6:

```bash
pip install PyQt6
```

Następnie kliknij dwukrotnie `uruchom.bat` albo:

```bash
python remarkable_reader.py
```

Przy pierwszym uruchomieniu program sam otworzy folder `remarkable_backup` (obok programu albo piętro wyżej), jeśli istnieje. Inny folder wybierzesz przyciskiem nad listą. Plik możesz też przeciągnąć do okna albo na `uruchom.bat`.

## Kopia notatek z tabletu (remarkable_backup.py)

Skrypt pobiera wszystkie dokumenty z tabletu przez kabel USB i zapisuje je jako pliki `.rmdoc` w takich samych folderach jak na tablecie. Nie potrzebuje dodatkowych bibliotek.

1. Podłącz tablet kablem USB.
2. Na tablecie włącz: Ustawienia → Pamięć (Storage) → **USB web interface**.
3. Uruchom:

```bash
python remarkable_backup.py
```

Pliki trafią do folderu `remarkable_backup`, który czytnik otworzy automatycznie. Ponowne uruchomienie pobiera tylko nowe dokumenty (`--overwrite` pobiera wszystko od nowa). Opcja `-o FOLDER` zapisuje w innym miejscu, a `--format pdf` pobiera PDF-y zamiast `.rmdoc`. Folder `remarkable_backup` jest w `.gitignore`, więc notatki nie trafią przypadkiem do repozytorium.

## Skróty

| Klawisz | Działanie |
|---|---|
| PgDn / PgUp, ← / →, Spacja | następna / poprzednia strona |
| Home / End | pierwsza / ostatnia strona |
| Ctrl + kółko myszy, Ctrl +/- | powiększenie |
| Ctrl+1 / Ctrl+2 | dopasuj do szerokości / cała strona |
| Ctrl+O | otwórz plik |
| Ctrl+E | eksport całego dokumentu do PDF |
| Ctrl+Shift+E | eksport bieżącej strony do PNG |
| Ctrl+B | pokaż / ukryj bibliotekę |
| F11 | pełny ekran |

## Eksport z wiersza poleceń

```bash
python remarkable_reader.py --export-pdf "Biologia.rmdoc" Biologia.pdf
```

```bash
python remarkable_reader.py --export-png "Biologia.rmdoc" obrazy --scale 1.5 --pages 1,2,3
```

Eksport PDF zapisuje notatki jako grafikę wektorową, więc są ostre przy każdym powiększeniu. Strony oryginalnego PDF-a lub EPUB-a trafiają tam jako obraz.

## Pliki

- `remarkable_reader.py`: okno programu i eksport
- `rmlines.py`: parser plików `.rm` v6 (bez dodatkowych bibliotek)
- `rmdoc.py`: wczytywanie `.rmdoc`, folderów z tabletu i biblioteki
- `render.py`: rysowanie stron (pióra, kolory, tekst, szablony)
- `remarkable_backup.py`: kopia dokumentów z tabletu przez USB

## Ograniczenia

- Starsze pliki `.rm` (v3/v5, oprogramowanie sprzed 3.0) nie są obsługiwane. Program pokaże wtedy komunikat.
- Szablony stron są rysowane w przybliżeniu, bo oryginalne pliki szablonów są tylko na tablecie.
- Położenie notatek na stronach PDF jest liczone tak jak na tablecie (strona dopasowana do ekranu). Przy nietypowych proporcjach PDF-a może być lekko przesunięte.
