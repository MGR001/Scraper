from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_key: str
    openai_api_key: str
    scrape_timeout: int = 30
    embedding_model: str = "text-embedding-3-small"
    chat_model: str = "gpt-4o"

    model_config = {"env_file": ".env"}


settings = Settings()
