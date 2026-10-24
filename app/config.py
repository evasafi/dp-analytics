from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DP_")

    database_url: str  # required: set DP_DATABASE_URL in .env
    secret_key: str  # required: set DP_SECRET_KEY in .env
    epsilon_per_count: float = 0.1
    epsilon_per_histogram: float = 0.1
    epsilon_per_mean: float = 0.2   # noisy sum + noisy count
    field_daily_cap: int = 200


settings = Settings()


