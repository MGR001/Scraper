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
    summary_model: str = "gpt-4o-mini"  # cheap, non-reasoning model — per-page volume x
                                          # reasoning-token cost is the trap already hit
                                          # with gpt-5.6-terra budgets elsewhere
    max_summaries_per_sweep: int = 400
    contact_email: str = "contact@example.com"  # Reddit User-Agent contact — required by their API rules
    classifier_model: str = "gpt-4o-mini"  # cheap model for high-volume mention classification
    max_mention_classifications_per_sweep: int = 200

    model_config = {"env_file": ".env"}


settings = Settings()
