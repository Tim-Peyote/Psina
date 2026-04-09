from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/zalutka"
    database_url_sync: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/zalutka"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Celery
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # LLM
    llm_provider: str = "qwen"
    llm_model: str = "qwen-turbo"
    llm_api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_fallback_provider: str = "mock"
    llm_fallback_model: str = "mock-v1"

    # Token budget
    daily_token_budget: int = 100000

    # Bot identity
    bot_name: str = "Бот"
    bot_aliases: list[str] = ["ботяра", "ботик", "кореш", "корешок"]
    bot_telegram_username: str = ""  # заполняется автоматически при старте

    # Bot behavior
    bot_mode: str = "observer"  # observer | assistant | social | game_master
    proactive_cooldown_seconds: int = 300
    proactive_max_per_hour: int = 5
    quiet_hours_start: int = 23
    quiet_hours_end: int = 7

    # Trigger system
    trigger_high_threshold: float = 0.7
    trigger_medium_threshold: float = 0.3

    # Session management
    session_timeout_seconds: int = 180
    session_max_messages: int = 8
    max_sessions_per_chat: int = 5

    # Anti-chaos
    anti_chaos_cooldown: int = 30
    anti_chaos_max_per_hour: int = 20
    anti_chaos_max_consecutive: int = 3

    # Retrieval
    max_context_tokens: int = 3000
    max_memory_items: int = 10
    embedding_dimension: int = 768

    # Memory system upgrade
    memory_extraction_interval: int = 30  # messages between extractions
    memory_extraction_delay: int = 60  # seconds between extractions
    max_memory_items_per_user: int = 100
    max_memory_items_per_chat: int = 500
    memory_ttl_weak_items: int = 168  # hours (7 days)
    memory_decay_half_life: int = 72  # hours (3 days)
    max_context_pack_tokens: int = 4000
    max_context_recent_messages: int = 20
    max_context_memory_items: int = 10
    max_context_summary_tokens: int = 500
    memory_retrieval_top_k: int = 10
    compaction_threshold_tokens: int = 3000
    compaction_messages_per_batch: int = 50
    compact_after_hours: int = 24
    embedding_model: str = "text-embedding-3-small"

    # Censorship
    default_censorship_level: str = "moderate"

    # Web search
    web_search_enabled: bool = True
    web_search_provider: str = "searxng"  # searxng | mock
    searxng_url: str = "http://searxng:8080"  # SearXNG instance URL
    web_search_unlimited: bool = True
    web_search_max_per_hour: int = 100
    web_search_max_per_day: int = 500
    web_search_cache_ttl: int = 300  # 5 минут
    web_search_max_results: int = 5

    # Admin API
    admin_api_host: str = "0.0.0.0"
    admin_api_port: int = 8000
    admin_api_secret: str = "change_me_in_production"


settings = Settings()
