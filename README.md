# Backend

[![Coverage](https://img.shields.io/endpoint?url=https://six7-click-n-deploy.github.io/backend/badge.json)](https://six7-click-n-deploy.github.io/backend/)

FastAPI-Backend des App Stores. Nimmt REST-Anfragen vom Frontend entgegen, validiert Keycloak-Tokens, persistiert in PostgreSQL und dispatcht Deployment-Tasks an den Celery-Worker via RabbitMQ.

## Setup

Dieses Repository wird nicht eigenständig gestartet. Der gesamte Stack — inklusive Backend — wird über das deployment-Repository hochgefahren. Vollständige Anleitung: [deployment/README.md](https://github.com/six7-click-n-deploy/deployment#readme).

Voraussetzung für alle folgenden Befehle: `make dev-up` aus dem `deployment/`-Verzeichnis wurde ausgeführt und der Stack läuft.

## Entwicklung

Alle `make`-Befehle werden aus dem `deployment/`-Verzeichnis des [deployment-Repos](https://github.com/six7-click-n-deploy/deployment) ausgeführt — dort liegt das Makefile.

```bash
# in app-store/deployment
make test-backend       # pytest im Backend-Container
make lint-backend       # ruff check
make lint-backend-fix   # ruff check --fix
make format-backend     # ruff format
make shell-backend      # interaktive Shell im Container
```

## Datenbank-Migrationen

Ebenfalls aus `deployment/`:

```bash
# in app-store/deployment
make migrate-dev                              # Schema auf head bringen
make migration-create MSG="add foo column"    # Autogenerate aus Models
make migration-history                        # Migrations-Historie
make migration-current                        # aktuelle Revision
make migration-downgrade                      # eine Revision zurück
```

## API-Dokumentation

Swagger-UI mit allen Endpoints: http://localhost:8000/docs (nach `make dev-up`).

## Technologie-Stack

- **FastAPI** + **Uvicorn** (ASGI, 4 Worker)
- **SQLAlchemy 2.0** ORM, **Alembic** für Migrationen
- **Pydantic** für Request-/Response-Validierung
- **python-keycloak** für OIDC-Token-Validierung
- **Celery** als Producer (Tasks gehen an Worker via RabbitMQ)
- **Ruff** für Linting und Formatting
- **pytest** mit `unit`/`integration`/`api`-Markern

## Code-Struktur

Der Code liegt in `app/` und folgt einem Schichtenmodell: ein Request läuft `routers/` (HTTP + Auth) → `crud/` (DB-Zugriff) → `models.py` (ORM), fachliche Logik steckt in `services/`.

```
app/
├── main.py          # FastAPI-App, Router-Registrierung, Lifespan (startet u.a. den Event-Listener-Thread)
├── config.py        # Pydantic-Settings aus Env-Variablen
├── database.py      # SQLAlchemy-Engine + Session-Dependency
├── models.py        # ORM-Modelle (eine Tabelle = eine Klasse)
├── schemas.py       # Pydantic Request-/Response-Schemas
├── celery_app.py    # Celery-Producer (dispatcht Tasks an den Worker)
├── routers/         # HTTP-Endpoints, ein File pro Ressource (siehe unten)
├── crud/            # DB-Operationen, keine HTTP- oder Business-Logik (siehe unten)
├── services/        # Fachlogik & externe Integrationen (siehe unten)
└── utils/           # Querschnitts-Helfer (Auth, Crypto, Permissions, Zeit)
```

**routers/** — jeweils unter dem gezeigten Prefix eingebunden (`main.py`):

| Router | Prefix | Zweck |
|---|---|---|
| `auth_keycloak` | `/auth` | Token-Validierung, User-Anlage bei erstem Login |
| `users` | `/users` | User-Verwaltung |
| `courses` | `/courses` | Kurse (Lehrveranstaltungen) |
| `apps` | `/apps` | App-Katalog: Registrierung, Variablen, Versionen |
| `admin_apps` | `/admin` | Admin-Freigabe von App-Versionen |
| `deployments` | `/deployments` | Deployments anlegen/steuern (Kern-Ressource) |
| `tasks` | `/tasks` | Celery-Task-Status |
| `teams` | `/teams` | Teams innerhalb eines Kurses |
| `quotas` | `/quotas` | OpenStack-Quota-Abfrage |
| `dashboard` | `/dashboard` | Aggregierte Übersichten fürs Frontend |
| `openstack_credentials` | `/me/openstack-credentials` | OpenStack-Zugangsdaten pro User |
| `openstack_resources` | `/me/openstack/resources` | Read-API für Networks/Flavors/Images (Wizard-Dropdowns) |

**crud/** — je ein File pro Ressource (`users`, `apps`, `courses`, `teams`, `tasks`, `app_version_approvals`, `openstack_credentials`), das reine DB-Operationen für das gleichnamige Modell kapselt — keine HTTP- oder Business-Logik. Zwei fallen aus dem Muster:

- `deployments` — mit Abstand am umfangreichsten, da Deployments die Kern-Ressource sind (Status-Übergänge, Task-Verknüpfung, Redeploy)
- `locks` — keine Ressource, sondern Per-User-Serialisierung über Postgres Advisory Locks (verhindert parallele Deployments desselben Users)

**services/** — Fachlogik und externe Integrationen:

| Service | Zweck |
|---|---|
| `celery_event_listener` | Daemon-Thread: konsumiert Celery-Events aus RabbitMQ, schreibt Task-Status in die DB |
| `reconciler` | Fängt hängende Tasks ab, deren Events verloren gingen (Fallback zum Listener) |
| `deployment_pubsub` | In-Process-Bridge Listener → SSE-Endpoint (Live-Status) |
| `deployment_status` | Verheiratet gecachten Terraform-State mit Live-OpenStack-Daten (Infrastructure-Tab) |
| `deployment_notifier` | Baut & versendet Post-Deploy-Mails (User- und Owner-Variante) |
| `email_service` | SMTP-Versand + Jinja2-Templating für die Mails |
| `task_service` | Zwei-Phasen-Dispatch: PENDING-Row in Tx anlegen, dann Celery-Enqueue |
| `lifecycle` | Single Source of Truth, welche Aktion in welchem Deployment-Status erlaubt ist |
| `git_service` | Git-Clone/Release-Infos, Sparse-Checkout für den Variablen-Scan |
| `openstack_client` | Gemeinsamer OpenStack-Client-Layer (per-User-`Connection`) |
| `openstack_validator` | Prüft Credentials gegen das Ziel-Keystone (Upsert + `/test`) |
| `clouds_yaml_parser` | Parst rohes `clouds.yaml` in ein Credential-Schema |
| `tf_state_parser` | Liest den in `Task.tf_state` persistierten Terraform-State |

**utils/** — `keycloak_auth` (Token-Validierung), `permissions` + `capabilities` (rollenbasierte `can_*`/`ensure_*`-Checks), `crypto` (Fernet-Verschlüsselung at-rest, Key geteilt mit dem Worker), `app_image` (Data-URL ↔ Bytes), `time`.

## Mehr

- Architektur und projektübergreifende Doku: [.github-Repo](https://github.com/six7-click-n-deploy/.github)
- Worker-Service: [worker-Repo](https://github.com/six7-click-n-deploy/worker)

## IPv4-/IPv6-Adressen in der Ressourcen-API

Die Ressourcen-Endpunkte liefern pro Netzwerk unter `addresses[].ips` alle
von OpenStack gemeldeten Adressen. Jeder Eintrag enthält `address`, `version`
(`4`, `6` oder `null` bei nicht erkennbarer Adresse), `type` und `mac`.
Fehlender Adresstyp wird wie bisher als `fixed` behandelt. Die IP-Version
wird aus der Adresse bestimmt, auch wenn OpenStack keine Version mitsendet.

Die bisherigen Felder bleiben zur Kompatibilität erhalten: `fixed_ip` enthält
weiterhin die erste Fixed IP, `floating_ip` die letzte Floating IP. Diese
Einzelfelder sind keine Empfehlung für die Erreichbarkeit oder IP-Version;
Clients für Dual Stack verwenden die vollständige `ips`-Liste. Die Reihenfolge
entspricht der OpenStack-Antwort. Fehlende oder leere Adressen werden ignoriert.

Port-Details enthalten entsprechend `ports[].fixed_ips` mit `address`,
`version` und `subnet_id` für jede Adresse. Das bestehende `ports[].fixed_ip`
bleibt die erste gültige Adresse; `fixed_ips` ist bei Ports ohne Adressen leer.

### Adressen in Terraform-Zugangsoutputs

`team_vms.value[team]` und `user_accounts.value[account]` können zusätzlich
`ips: list(string)` enthalten, z. B. `["192.0.2.10", "2001:db8::10"]`.
Die Benachrichtigungen übernehmen diese Adressen zusätzlich zu den bisherigen
`floating_ip`/`fixed_ip` beziehungsweise `ip` (ohne Duplikate). Alte Outputs
bleiben gültig. Ein Account-`port` gilt für alle seine Adressen. IPv6 mit Port
wird als `[2001:db8::10]:8080` dargestellt; reine Adressen bleiben unverändert.

Die Vorlagen legen mit `ip` und `url` weiterhin den bevorzugten Zugang fest;
das Backend wählt nicht automatisch eine IP-Familie aus. Fertige URLs werden
unverändert übernommen und müssen bei IPv6-Literalen bereits korrekt
geklammert sein, z. B. `http://[2001:db8::10]:8080`.

## HTTP-Listener konfigurieren

Die Docker-Images starten über `python -m app.server` (Dev zusätzlich
`--reload`). `HOST` ist standardmäßig `0.0.0.0` für bisherigen IPv4-Betrieb.
Mit `HOST=::` öffnet der Startcode ausdrücklich einen Dual-Stack-Socket;
IPv4 und IPv6 funktionieren damit auch bei `WORKERS=1`, mehreren Workern und
Dev-Reload gleich. Wenn der Host keine Dual-Stack-Sockets unterstützt, schlägt
der Start sichtbar fehl. Eine konkrete IPv6-Adresse bindet nur IPv6.

`PORT` (Standard 8000), `WORKERS` (Standard 4; mit Reload immer 1), `LOG_LEVEL`
und `FORWARDED_ALLOW_IPS` konfigurieren den Start. Das Produktionsimage behält
seine bisherige Proxy-Vertrauenskonfiguration `*`; außerhalb des Images gilt
standardmäßig `127.0.0.1`. Ein Compose-`command` muss den neuen Einstieg
verwenden, sonst überschreibt es diese Konfiguration.

`tests/unit/test_server.py` startet echte Backend-Prozesse und ruft `/health`
über explizite IPv4-/IPv6-Verbindungen auf. Der Test benötigt IPv6-Loopback,
aber keine Cloud-Verbindung und keine Datenbank-Schreibzugriffe.

Image-Startbefehle separat prüfen (temporäre Container, synthetische Settings,
keine Cloud-/Datenbank-Schreibzugriffe):

```sh
docker build -t backend-listener-test .
python3 scripts/check_listener_image.py backend-listener-test
docker build -t backend-listener-dev-test -f Dockerfile.dev .
python3 scripts/check_listener_image.py backend-listener-dev-test
```

Das Image-Prüfskript wählt mit `postgresql+psycopg2://` den tatsächlich
installierten Treiber explizit. Bei frischer Auflösung kann SQLAlchemy 2.1
installiert werden; dort erwartet die unspezifische URL `postgresql://` den
hier nicht installierten Treiber `psycopg`. Für solche Umgebungen muss
`DATABASE_URL` ausdrücklich `postgresql+psycopg2://...` verwenden. Dieser
Unterschied betrifft die Datenbankkonfiguration, nicht die IP-Familie.
