from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_key: str           # service-role key — background jobs only
    supabase_anon_key: str = ""  # anon key — used for user-scoped DB clients
    openai_api_key: str
    match_threshold: float = 0.35
    scrape_timeout: int = 30
    embedding_model: str = "text-embedding-3-small"
    chat_model: str = "gpt-5.6-terra"

    model_config = {"env_file": ".env"}


settings = Settings()
