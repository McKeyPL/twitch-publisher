# Twitch Publisher

Recznie uruchamiany watcher dla Windows Server 2019. Po zakonczeniu nagrania
publikuje MKV na wlaczonych platformach (YouTube, CDA i Rumble), zapisuje stan w
SQLite i przenosi kompletny zestaw plikow do katalogu `_uploaded`.

## Wymagania i instalacja

1. Zainstaluj 64-bitowy Python 3.11 lub nowszy i zaznacz `Add Python to PATH`.
2. Zainstaluj FFmpeg/ffprobe i dodaj jego katalog `bin` do `PATH`.
3. Nadaj kontu procesu prawo odczytu, zapisu, tworzenia katalogow i przenoszenia
   plikow w katalogu nagran oraz w katalogu projektu.
4. W katalogu projektu wykonaj:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   playwright install firefox chromium
   Copy-Item .env.example .env
   ```

5. Ustaw `paths.recordings_root` w `config.yaml`, a sekrety i identyfikatory
   playlist w `.env`. Sekretow, tokenow i `storage_state` nie dodawaj do Git.

## YouTube OAuth

W Google Cloud Console utworz projekt, wlacz YouTube Data API v3, skonfiguruj
OAuth consent screen i utworz OAuth Client ID typu **Desktop app**. Pobrany plik
zapisz jako `credentials.json` (lub ustaw sciezke w
`YOUTUBE_CLIENT_SECRETS_FILE`). Przy pierwszym uploadzie otworzy sie przegladarka;
token zostanie zapisany lokalnie w `auth/youtube_token.json`.

Lokalne liczniki quoty w SQLite sa konserwatywnym zabezpieczeniem. Dzien licznika
zmienia sie o polnocy Pacific Time. Rzeczywiste limity sprawdzaj w Google Cloud
Console. Gdy `YT_PLAYLIST_<NICK>` jest pusty, uploader moze utworzyc playliste i
wypisze jej ID; zapisz je w `.env`, aby kolejny proces nie utworzyl duplikatu.

## CDA, Rumble i Firefox

Firefox musi byc zainstalowany, a uzytkownik powinien przed pierwszym uruchomieniem
zalogowac sie w nim na cda.pl i rumble.com. Zamknij Firefox przed odczytem cookies,
bo otwarta baza profilu moze byc zablokowana. Kolejnosc autoryzacji to zapisany
`storage_state`, cookies Firefoksa, a nastepnie widoczne okno logowania recznego.
Po recznym zalogowaniu nacisnij Enter w konsoli.

Zweryfikowane formularze CDA i Rumble nie udostepniaja pola SRT. Log poda wtedy
sciezke napisow wymagajacych manualnego dodania. Automatyzacja moze wymagac
aktualizacji selektorow po zmianach stron.

Przy duzym pliku CDA moze przez dluzszy czas wysylac lub przetwarzac nagranie,
zanim pokaze formularz metadanych. Uploader zapisuje wtedy heartbeat co 30 sekund.
Heartbeat oznacza jedynie, ze Playwright nadal czeka; nie potwierdza przeplywu
danych. Do diagnostyki uruchom:

```powershell
.\start.ps1 -BrowserDebug
# albo bez launchera:
python main.py --config config.yaml --browser-debug
```

Tryb debug wymusza widoczne okno Firefoksa, loguje bledy konsoli, nieudane
requesty, odpowiedzi HTTP 4xx/5xx, stan inputu pliku i elementy postepu. Zapisuje
tez screenshoty oraz trace Playwright w `logs/browser_debug`. Trace mozna otworzyc:

```powershell
playwright show-trace logs\browser_debug\cda_*_trace.zip
```

Trace moze zawierac dane sesji i adresy requestow, dlatego nie nalezy go
publikowac ani dodawac do Git.

Formularz CDA jest wypelniany semantycznie, bez zaleznosci od jednego historycznego
ID pola tytulu. Domyslne `platforms.cda.form_options` publikuja film publicznie,
akceptuja regulamin i oswiadczenie o prawach oraz odpowiadaja `Nie` na pytania o
przemoc, seks, uzaleznienia, wulgaryzmy i sponsoring. Wartosci mozna zmienic per
instalacja w `config.yaml`.

Rumble wymaga jawnego wyboru licencji w `.env`:

```dotenv
# 0=Personal Use; 5=Video Management exclusive;
# 6=Rumble Only non-exclusive; 7=Video Management excluding YouTube
RUMBLE_LICENSE_OPTION=6
```

Kod nie wybiera tej opcji samodzielnie, poniewaz kazda wartosc przekazuje inny
zakres praw. Na podstawie formularza z 2026-07-20 uploader ustawia kategorie
glowna `Gaming`, probuje dopasowac gre jako kategorie dodatkowa i odrzuca pliki
wieksze niz 15 GB przed otwarciem przegladarki. Sukces jest rozpoznawany przez
formularz `#form3` i pole `textarea#direct`, a nie przez zmiane URL strony.

## Uruchamianie

Proces uruchamiany jest wylacznie recznie, bez Task Scheduler/cron:

```powershell
.\start.ps1
```

Launcher aktywuje istniejace `.venv`, ostrzega o braku `.env`, zapisuje zdarzenia
do `logs/start_ps1.log` i po awarii Pythona restartuje proces po 10 sekundach.
Poprawne wyjscie lub Ctrl+C nie powoduje restartu. Przydatne warianty:

```powershell
.\start.ps1 -Config "config.yaml" -Once
python main.py --config config.yaml --once
```

Watcher utrzymuje jeden nieblokujacy tracker stabilnosci rozmiaru MKV przez caly
czas procesu. Plik bez `_meta.txt`, bez pola `Ended` albo ze zmieniajacym sie
rozmiarem nie jest wysylany. Wyjatek jednego pliku lub platformy nie zatrzymuje
pozostalych. Stan `SUCCESS`/`SKIPPED` w SQLite zapobiega duplikacji po restarcie.
Ctrl+C ustawia token anulowania sprawdzany co najwyzej co sekunde i przerywa
aktywna operacje bez wykonywania kolejnych prob. Jesli serwis
przyjal formularz, ale nie da sie potwierdzic wyniku, status otrzymuje znacznik
`[NO_AUTO_RETRY]`; nagranie wymaga wtedy sprawdzenia w panelu platformy i nie jest
ponawiane automatycznie.

## Reczne czyszczenie

Cleanup nie jest wywolywany z `main.py`. Domyslnie wykonuje tylko podglad:

```powershell
python cleanup.py --config config.yaml --dry-run
python cleanup.py --config config.yaml --no-dry-run --retention-days 30
```

Usuwane sa wylacznie zestawy w `<recordings_root>/<nick>/_uploaded`, starsze niz
retencja liczona od `mtime` pliku MKV.

## Testy

```powershell
pip install -r requirements-dev.txt
pytest -q
```
