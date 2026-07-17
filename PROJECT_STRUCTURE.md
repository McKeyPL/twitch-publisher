# Struktura projektu

```text
twitch-publisher/
|-- auth/
|   |-- __init__.py
|   `-- browser_session.py      # storage_state -> Firefox -> login reczny
|-- data/                       # SQLite; dane runtime ignorowane przez Git
|-- logs/                       # logi runtime ignorowane przez Git
|-- uploaders/
|   |-- __init__.py
|   |-- base.py                 # interfejs UploadResult/BaseUploader i retry
|   |-- youtube.py              # OAuth2, resumable upload, SRT, playlisty
|   |-- browser_form.py         # wspolne operacje formularzy Playwright
|   |-- cda.py                  # uploader formularza CDA
|   `-- rumble.py               # uploader formularza Rumble
|-- tests/                      # testy jednostkowe i integracyjne bez sieci
|-- .env.example
|-- .gitignore
|-- config.yaml
|-- config.py                   # YAML, dotenv, ekspansja i walidacja
|-- watcher.py                  # zaimplementowany: cykl bez sleep
|-- meta_parser.py              # parser wieloliniowych metadanych
|-- title_cleaner.py            # czyszczenie i limity tytulow
|-- duration_check.py           # stabilnosc pliku i ffprobe
|-- state.py                    # SQLite/WAL, statusy i quota
|-- mover.py                    # bezpieczne przenoszenie do _uploaded
|-- cleanup.py                  # zaimplementowany: osobne CLI, dry-run
|-- main.py                     # zaimplementowany: orkiestracja
|-- start.ps1                   # zaimplementowany: launcher i restart
|-- requirements.txt
|-- requirements-dev.txt
`-- README.md
```

## Granice modulow

- `watcher.py` tylko skanuje i korzysta z trackera przekazanego przez caller; nie
  usypia procesu i zwraca wszystkie statusy gotowosci.
- `meta_parser.py` odpowiada za format metadanych i walidacje domenowe.
- `duration_check.py` kwalifikuje zakonczone nagranie i odczytuje czas przez
  `ffprobe`.
- `state.py` jest jedynym miejscem zapisujacym statusy uploadow i quote SQLite.
- `main.py` sklada zaleznosci, utrzymuje tracker miedzy cyklami i izoluje wyjatki
  per nagranie oraz per platforma.
- `mover.py` dziala dopiero po terminalnym statusie wszystkich wymaganych platform.
- `cleanup.py` jest niezalezna, recznie uruchamiana komenda z domyslnym dry-run;
  `main.py` nigdy jej nie wywoluje.
- `start.ps1` uruchamia proces recznie i restartuje tylko po niezerowym kodzie.
