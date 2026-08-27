from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Токен бота, выданный @BotFather
    bot_token: str
    # Юзернейм бота БЕЗ @, например "SchoolerAuthBot"
    bot_username: str

    # Публичный HTTPS-адрес ЭТОГО бэкенда после деплоя — например
    # "https://api.myschooler.ru" или домен, который выдаёт Vercel.
    # Telegram будет слать сюда апдейты вебхука:
    #   {public_base_url}/api/telegram/webhook
    # ⚠️ Обязателен и для локальной разработки после переезда на вебхуки —
    # localhost Telegram не достучится, нужен туннель (ngrok и т.п.) с
    # HTTPS-адресом, который сюда и подставляется на время разработки.
    public_base_url: str

    # Секрет, которым подтверждается, что вебхук дёргает действительно
    # Telegram, а не кто-то посторонний, узнавший URL. Сгенерировать один раз:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    # и положить в переменные окружения — то же значение передаётся в
    # set_webhook(secret_token=...) при старте (см. lifespan в main.py) и
    # сверяется на каждый входящий запрос в /api/telegram/webhook.
    telegram_webhook_secret: str

    # Строка подключения к PostgreSQL (async-драйвер asyncpg)
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/schooler"

    # Сколько минут живёт код авторизации, пока пользователь не нажал Start в боте
    login_code_ttl_minutes: int = 10

    # Домены, которым разрешено дёргать API (для веб-версии Flet-приложения).
    # Простая строка через запятую, БЕЗ скобок и кавычек — так надёжнее: некоторые
    # панели переменных окружения (в т.ч. Amvera) не всегда корректно сохраняют
    # квадратные скобки/кавычки, из-за чего JSON-список ломался при парсинге.
    # Примеры значений: "*"  или  "https://a.amvera.io,https://b.amvera.io"
    cors_origins: str = "*"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    vk_client_id: str = ""
    vk_redirect_uri: str = ""
    yandex_client_id: str = ""
    yandex_client_secret: str = ""
    yandex_redirect_uri: str = ""

    # URL JWKS вашего Supabase-проекта — вида
    # https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
    # Нужен только для входа email+паролем через Supabase Auth
    # (supabase_auth.py). Пустая строка по умолчанию — чтобы не ломать
    # запуск бэкенда для тех, кто эту фичу ещё не подключил.
    supabase_jwks_url: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        value = self.cors_origins.strip()
        if value == "*" or not value:
            return ["*"]
        return [origin.strip() for origin in value.split(",") if origin.strip()]


settings = Settings()
