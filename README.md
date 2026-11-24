# 3CX Bulk Tool

Narzędzie do **masowych zmian w centrali 3CX (V20) przez API**, na podstawie pliku CSV (np. z Excela).
Jest w dwóch wersjach:

| Wersja | Dla kogo | Plik |
|---|---|---|
| **Okienko (GUI)** | wszyscy w firmie, bez terminala | `tcx-bulk-gui.exe` (Windows) / `tcx-bulk-gui` (Linux) |
| **Terminal (CLI)** | administratorzy, skrypty, automatyzacja | `tcx-bulk.exe` / `tcx-bulk` |

Pliki do pobrania budują się automatycznie w GitHubie: zakładka **Actions → ostatni przebieg „Build executables" → Artifacts** (osobno Windows i Linux).

---

## Najważniejsze zasady bezpieczeństwa (przeczytaj raz)

1. **Podgląd zawsze przed zmianą.** Wczytanie CSV niczego nie zmienia. Pokazuje tylko listę „było → będzie". Zmiany dzieją się dopiero po kliknięciu **ZASTOSUJ ZMIANY** (w CLI: flaga `--execute`).
2. **Backup przed każdą zmianą.** Stare wartości zapisują się w `backup.json` (GUI: folder `tcx-bulk-runs` w Twoim katalogu domowym; CLI: folder `tcx-runs`).
3. **Cofanie.** Zmiany można cofnąć z backupu (przycisk *Cofnij zmiany z backupu* albo `tcx-bulk rollback`). Dotyczy to zmian typu „update". Usuniętych rekordów program nie odtworzy sam. Ich dane zostają w `backup.json`.
4. **Tworzenie i usuwanie są domyślnie zablokowane.** Trzeba je jawnie odblokować (checkboxy w GUI, flagi `--allow-create` / `--allow-delete`).
5. **Pierwszy raz zawsze na 1 osobie.** Zrób CSV z jednym wierszem, zastosuj go i sprawdź w panelu 3CX, czy wynik jest taki, jak chcesz. Dopiero potem puść cały plik. W CLI służy do tego `--limit 1`.
6. **Dane osobowe (RODO / GDPR).** Eksport z centrali zawiera imiona, e-maile i numery telefonów. Nie wysyłaj go mailem, nie wrzucaj do gita ani do ogólnodostępnego folderu. Pliki `export_*` są w `.gitignore`.

---

## Krok 0: utwórz dostęp API w 3CX (jednorazowo)

Narzędzie loguje się do centrali „kluczem API", a nie Twoim hasłem do panelu.

1. Zaloguj się do panelu administratora 3CX.
2. Wejdź w **Integrations → API** (w zależności od wersji nazwa menu może się nieco różnić).
3. Kliknij **Add** i utwórz klienta API. Ustaw:
   - **Client ID**: dowolna nazwa/numer (to jest login),
   - **Role / uprawnienia**: tyle, ile potrzeba, najlepiej administracyjne (patrz „Znane problemy" niżej),
   - zapisz i **skopiuj API Key (Client Secret)**. Pokazuje się zwykle tylko raz.
4. Adres centrali (FQDN) to to, co wpisujesz w przeglądarce, np. `pbx.firma.ie`.

> Nie wpisuj hasła do panelu ani hasła SSH do tego narzędzia. Nie są potrzebne.

---

## Okienko (GUI): krok po kroku

1. Uruchom `tcx-bulk-gui`. Wpisz **Adres PBX**, **Client ID**, **Client Secret** i kliknij **Połącz i sprawdź**.
   Adres i Client ID zapamiętają się na następny raz, **Secret nigdy nie jest zapisywany na dysku**.
2. W sekcji 2 wybierz **Encję**. Domyślnie `Users` (numery wewnętrzne / użytkownicy). Po połączeniu na liście są **wszystkie encje, które udostępnia Twoja centrala**.
3. Kliknij **Eksportuj stan do CSV…**. Dostaniesz plik z aktualnymi danymi. To jest Twój wzór i dodatkowa kopia zapasowa.
4. Otwórz CSV w Excelu. **Zostaw kolumnę klucza** (dla `Users` to `Number`) i **tylko te kolumny, które chcesz zmienić**. Resztę kolumn usuń. Popraw wartości, zapisz jako CSV.
5. **Wczytaj CSV (tylko podgląd)…** pokaże, co się zmieni, a co jest błędem. Nic jeszcze nie jest zmieniane.
6. Jeśli podgląd jest dobry, kliknij **ZASTOSUJ ZMIANY** i potwierdź. Wiersze z błędami zostaną pominięte.
7. Coś poszło nie tak? **Cofnij zmiany z backupu…** i wskaż `backup.json` z folderu `tcx-bulk-runs`.

## Terminal (CLI)

Skopiuj `.env.example` do `.env` i uzupełnij (albo ustaw zmienne środowiskowe `TCX_FQDN`, `TCX_CLIENT_ID`, `TCX_CLIENT_SECRET`).

```bash
tcx-bulk check                              # czy logowanie działa
tcx-bulk entities                           # lista wszystkich encji w centrali
tcx-bulk export -o export_users.csv         # aktualny stan -> CSV
tcx-bulk apply zmiany.csv                   # PODGLĄD (dry-run), nic nie zmienia
tcx-bulk apply zmiany.csv --execute --limit 1   # test na 1 zmianie
tcx-bulk apply zmiany.csv --execute         # wszystko (zapyta o potwierdzenie)
tcx-bulk rollback tcx-runs/20261124-103000/backup.json --execute
```

Inne encje: `--entity Groups`, `--entity Queues` itd. Kolumnę klucza można wymusić przez `--key Name`. Domyślnie program wybiera po kolei: `Number`, `Name`, `Id`. Przydatne flagi: `--skip-errors` (wykonaj poprawne wiersze mimo błędnych), `--stop-on-error`, `--delay 0.5` (pauza między zmianami, gdy centrala jest obciążona), `-v` (pokaż też wiersze bez zmian).

---

## Jak przygotować plik CSV

Przykład (`examples/users_update.csv`):

```csv
Number,FirstName,LastName,EmailAddress,MobileNumber
100,Jan,Kowalski,jan.kowalski@firma.ie,0851234567
101,Anna,Nowak,anna.nowak@firma.ie,
102,,,nowy.adres@firma.ie,
```

| Zasada | Co to znaczy |
|---|---|
| Kolumna klucza | Po niej program znajduje rekord w centrali (dla `Users`: `Number`). Musi być w pliku. |
| **Pusta komórka** | **Nie zmieniaj tego pola.** (Wiersz 102 zmienia tylko e-mail.) |
| `__EMPTY__` | Ustaw pole na pustą wartość (kasowanie). |
| Nazwy kolumn | Muszą dokładnie pasować do nazw pól w centrali (zob. nagłówki z eksportu). **Literówka w nagłówku przerywa całość przed jakąkolwiek zmianą**, żeby nic się nie zepsuło po cichu. |
| Separator | Przecinek, średnik lub tabulator. Wykrywany automatycznie (polski Excel często zapisuje średnik). |
| Prawda/fałsz | `true/false`, `tak/nie`, `1/0`. |
| Numery telefonów | Zostają tekstem, więc zero na początku nie ginie. |
| Pola zagnieżdżone | Wpisz JSON w komórce, np. `[{"GroupId":1}]`. Dla zaawansowanych. |
| Kolumna `action` | Opcjonalna: `update` (domyślnie), `create`, `delete`. Wymaga flag zezwalających (zob. wyżej). Wzór: `examples/users_create_delete.csv`. |

Program jest **idempotentny**. Jeśli wartość w centrali już jest taka, jak w pliku, wiersz jest oznaczony „bez zmian". Tego samego pliku można więc użyć dwa razy.

---

## Znane ograniczenia i problemy (szczerze)

- **To narzędzie nie było jeszcze testowane na prawdziwej centrali 3CX.** Zostało napisane na podstawie [oficjalnej specyfikacji Configuration API](https://www.3cx.com/docs/configuration-rest-api-endpoints/) i przetestowane na symulatorze centrali (22 testy automatyczne). Dlatego **pierwszy test zrób na jednym testowym użytkowniku**, np. zmieniając tylko `FirstName`.
- **Błąd 403 na `/Users`.** Część osób na forum 3CX zgłasza, że klient API dostaje token z rolą `user` zamiast administratora i wtedy `/Users` zwraca 403. Sprawdź rolę klienta API w panelu. Jeśli problem zostaje, napisz do mnie, co zwraca `tcx-bulk check`.
- **Nie wszystko da się zmienić przez API.** Niektóre pola są tylko referencjami lub są zablokowane po stronie 3CX (np. bieżący status użytkownika `CurrentProfileName` nie zmienia się przez PATCH). Taki błąd zobaczysz w wynikach (`result.csv`) przy konkretnym wierszu.
- **Lista `Users` może nie zawierać wszystkich kont** (np. kont z rolą System Owner).
- **Wszystkie encje = operacje CRUD.** Narzędzie czyta, zmienia, tworzy i usuwa rekordy każdej encji z `$metadata` centrali. Specjalnych akcji API (np. restart usługi, wywołania funkcji OData) nie obsługuje.
- Certyfikat TLS jest domyślnie **weryfikowany**. Gdy masz własne CA, wskaż plik w polu *Plik CA* (albo `TCX_CA_BUNDLE`). Opcja „nie sprawdzaj certyfikatu" jest ostatecznością.

---

## Dla programisty

```bash
pip install -e ".[dev]"
pytest -q                     # testy (GUI-owe wymagają tkinter + DISPLAY, w CI: xvfb-run)
python -m tcx_bulk.gui        # uruchom okienko ze źródeł
```

Budowa plików wykonywalnych lokalnie (na danym systemie dla niego samego):

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name tcx-bulk-gui --paths src packaging/gui_entry.py
pyinstaller --onefile --name tcx-bulk --paths src packaging/cli_entry.py
```

Struktura: `src/tcx_bulk/client.py` (HTTP, token, ponawianie), `bulk.py` (CSV, plan zmian, backup, rollback), `cli.py`, `gui.py`; `tests/fakepbx.py` to symulator centrali.
