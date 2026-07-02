# OTOMOTO Monitor - darmowy wariant GitHub Actions

Ten wariant odpala `python main.py --once` cyklicznie przez GitHub Actions.
Nie potrzebuje Rendera ani karty platniczej.

## Ograniczenia

- GitHub Actions schedule dziala najczesciej co 5 minut, nie co 2 minuty.
- Harmonogram moze czasem wystartowac z opoznieniem.
- Stan SQLite jest zapisywany do repo w pliku `data/otomoto_monitor.sqlite3`.
- Nie wrzucaj `.env` do repo. Linki i webhook ustaw jako GitHub Secrets.

## Co wrzucic na GitHub

Wrzuc cala zawartosc tego folderu do nowego repozytorium GitHub.

## Secrets w GitHubie

Wejdz w repozytorium -> Settings -> Secrets and variables -> Actions -> New repository secret.

Dodaj:

```text
DISCORD_WEBHOOK_URL
```

Wartosc: Twoj webhook Discorda.

Dodaj drugi sekret:

```text
OTOMOTO_URLS
```

Wartosc: wszystkie linki OTOMOTO oddzielone pionowa kreska `|`, np.:

```text
https://link-1|https://link-2|https://link-3
```

## Wlaczenie zapisu stanu

W repozytorium wejdz w Settings -> Actions -> General.
W sekcji Workflow permissions ustaw Read and write permissions.
To pozwala workflow zapisac baze SQLite z widzianymi ogloszeniami.

## Pierwsze uruchomienie

Wejdz w zakladke Actions -> OTOMOTO Monitor -> Run workflow.
Pierwszy run zapisze aktualne ogloszenia jako baze i nic nie wysle, bo `FIRST_RUN_NOTIFY=false`.
Potem workflow bedzie uruchamial sie automatycznie co 5 minut.
