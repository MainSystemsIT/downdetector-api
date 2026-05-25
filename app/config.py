from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    downdetector_locale: str = "br"
    downdetector_status_path: str | None = None
    browser_locale: str = "pt-BR"
    cache_ttl_seconds: int = 120
    request_timeout_seconds: int = 90
    headless: bool = True
    api_token: str | None = None

    def downdetector_base_url(self) -> str:
        locale = self.downdetector_locale.strip().lower()
        locale = locale.removeprefix("https://").removeprefix("http://")
        locale = locale.removeprefix("www.")
        locale = locale.removeprefix("downdetector.")
        locale = locale.removeprefix(".")
        locale = locale.rstrip("/")

        if locale in ("", "com", "us", "global"):
            domain = "downdetector.com"
        elif locale == "br":
            domain = "downdetector.com.br"
        elif "." in locale:
            domain = f"downdetector.{locale}"
        else:
            domain = f"downdetector.{locale}"

        return f"https://{domain}"

    def _status_path_template(self) -> str:
        if self.downdetector_status_path:
            return self.downdetector_status_path

        if self.downdetector_base_url().endswith(".com.br"):
            return "/fora-do-ar/{service}/"

        return "/status/{service}/"

    def urls_for_service(self, service: str) -> list[str]:
        slug = service.lower().strip()
        path = self._status_path_template().format(service=slug)
        return [f"{self.downdetector_base_url().rstrip('/')}/{path.lstrip('/')}"]


settings = Settings()
