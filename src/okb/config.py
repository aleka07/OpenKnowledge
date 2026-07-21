from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KB_", env_file=".env", extra="ignore")

    db_dsn: str = "postgresql://okb:okb@127.0.0.1:15432/okb"
    data_dir: Path = Path("~/kb-data")

    gen_url: str = "http://10.66.66.15:8000/v1"
    gen_model: str = "nvidia/Qwen3.6-35B-A3B-NVFP4"
    emb_url: str = "http://10.66.66.15:8001/v1"
    emb_model: str = "Qwen/Qwen3-Embedding-8B"
    emb_dim: int = 1536

    concurrency: int = 16

    mcp_host: str = "0.0.0.0"
    mcp_port: int = 17000

    @property
    def raw_dir(self) -> Path:
        return self.data_dir.expanduser() / "raw"

    @property
    def embedding_model_tag(self) -> str:
        return f"{self.emb_model}@{self.emb_dim}"


settings = Settings()
