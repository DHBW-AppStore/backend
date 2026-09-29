
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # Celery (optional - only needed for API runtime, not for migrations)
    CELERY_BROKER_URL: str = "amqp://admin:admin@rabbitmq:5672/"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/0"

    # Git
    TEMP_REPO_BASE_PATH: str = "/tmp/worker_repos"
    GIT_ACCESS_TOKEN: str = ""  # Token for HTTPS git authentication

    # Keycloak — single source of truth for authentication
    KEYCLOAK_SERVER_URL: str = "http://keycloak:8080"
    KEYCLOAK_REALM: str = "dhbw"
    KEYCLOAK_CLIENT_ID: str = "appstore-backend"
    KEYCLOAK_CLIENT_SECRET: str = ""  # Set via environment variable

    # LTI 1.3 — Moodle as a second identity source (JIT-provisions the
    # same ``users`` table as Keycloak, see app/utils/lti_auth.py).
    # LTI_PLATFORM_ISSUER/CLIENT_ID/DEPLOYMENT_ID come from the tool
    # registration on the Moodle side (m_lti_types); JWKS_URL is Moodle's
    # public-key endpoint used to verify the launch id_token's signature.
    LTI_PLATFORM_ISSUER: str = ""
    LTI_PLATFORM_JWKS_URL: str = ""
    # Optional Host header override for the JWKS fetch. Needed whenever
    # the network path to the platform (e.g. a container-to-host hop in
    # local dev, or an internal LB hostname in prod) differs from the
    # platform's public hostname that its vhost/routing actually matches
    # on — without this, the request 404s even though LTI_PLATFORM_ISSUER
    # is correct for token validation.
    LTI_PLATFORM_JWKS_HOST_HEADER: str = ""
    LTI_CLIENT_ID: str = ""
    LTI_DEPLOYMENT_ID: str = ""
    # Symmetric secret used to sign/verify the short-lived backend-issued
    # session JWT minted after a successful LTI launch (HS256). Separate
    # from CREDENTIAL_ENCRYPTION_KEY — different purpose, different
    # rotation schedule.
    LTI_SESSION_SECRET: str = ""
    LTI_SESSION_TOKEN_TTL_SECONDS: int = 3600
    # Shared store for the OIDC state/nonce pairs minted in /lti/login and
    # consumed in /lti/launch. Must be shared across processes — uvicorn
    # runs multiple workers, and Moodle's login and launch requests can
    # land on different ones, so an in-memory dict here would randomly
    # 401 launches that hit a worker that never saw the /lti/login call.
    # Separate DB index from CELERY_RESULT_BACKEND (db 0) on the same
    # Redis instance so this never collides with Celery's keys.
    LTI_NONCE_REDIS_URL: str = "redis://redis:6379/1"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Symmetric Fernet key shared with the worker. Used to encrypt OpenStack
    # credentials at rest and to seal the envelope shipped through Celery.
    # Generate: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
    CREDENTIAL_ENCRYPTION_KEY: str

    # SMTP (Gmail). Required for the post-deploy notification mails.
    # Use a Google "App password" (the regular password won't work with
    # 2FA enabled).
    #
    # SMTP_ENABLED is the explicit kill-switch — set it to False to turn
    # mail delivery into a no-op even when credentials are populated. It
    # lives separately from the credentials so operators can keep the
    # app-password in .env while disabling mail in dev/CI, and so the
    # resend-access endpoint can distinguish "we chose not to send"
    # (HTTP 503) from "SMTP refused" (HTTP 502).
    SMTP_ENABLED: bool = False
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_FROM_NAME: str = "Click-n-Deploy"

    # Public URL the deployment detail page is reachable under, used in
    # the owner-summary mail to deep-link back into the UI. No trailing
    # slash. Falls back to the first CORS origin in dev.
    APP_BASE_URL: str = "http://localhost:5173"

    # Public URL of this API as reachable from the outside world, *including*
    # any path prefix a reverse proxy adds (e.g. "/api"). Needed for URLs we
    # hand to a third party (like the LTI redirect_uri Moodle validates
    # against its registered tool config) — ``request.base_url`` can't be
    # used for that since the deployment nginx strips the "/api/" prefix
    # before proxying to this service and never forwards it back
    # (no X-Forwarded-Prefix), so the app has no way to see it was reached
    # via "/api" from inside a request.
    API_BASE_URL: str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
