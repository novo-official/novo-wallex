"""Django settings for the Wallex trading project.

Everything environment-specific comes from env vars (see .env.example).
The market policy files named below are validated at startup by the
`exchange.E001` system check, and the trading engine refuses to run if they
are invalid.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")  # real environment variables still take precedence


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or ("insecure-dev-only-key" if DEBUG else "")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false")

ALLOWED_HOSTS = [
    host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "exchange",
    "market",
    "backtest",
    "ml",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "wallex"),
        "USER": os.environ.get("POSTGRES_USER", "wallex"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"  # all timestamps in the journal are UTC
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    # Closed by default. Read-only API endpoints are opened explicitly in the API phase.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAdminUser"],
}

# --- Wallex exchange client ---------------------------------------------------
WALLEX_BASE_URL = os.environ.get("WALLEX_BASE_URL", "https://api.wallex.ir")
WALLEX_API_KEY = os.environ.get("WALLEX_API_KEY", "")
WALLEX_TIMEOUT_SECONDS = float(os.environ.get("WALLEX_TIMEOUT_SECONDS", "10"))
WALLEX_MAX_RETRIES = int(os.environ.get("WALLEX_MAX_RETRIES", "3"))
WALLEX_REQUESTS_PER_SECOND = float(os.environ.get("WALLEX_REQUESTS_PER_SECOND", "5"))

# --- Market policy (default-deny) ---------------------------------------------
TRADING_CONFIG_FILE = Path(os.environ.get("TRADING_CONFIG_FILE", BASE_DIR / "config" / "trading.toml"))
BLOCKED_MARKETS_FILE = Path(os.environ.get("BLOCKED_MARKETS_FILE", BASE_DIR / "blocked_markets.txt"))

# Live trading is double-gated: `--mode live` AND this flag. Never true by default.
LIVE_TRADING_CONFIRMED = _bool("LIVE_TRADING_CONFIRMED", False)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
