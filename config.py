from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str
    business_name: str = "客服助理"
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    collection_name: str = "service_bot"
    embedding_model: str = "text-embedding-3-small"
    llm_model: str = "gpt-4o"
    top_k: int = 10

    # Telegram
    telegram_token: str = ""
    # LINE
    line_channel_secret: str = ""
    line_channel_access_token: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
