from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SAVE_INTERVAL = 5


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    url: str
    domains: tuple[str, ...]

    def allows_host(self, host: str) -> bool:
        host = host.lower().lstrip(".")
        return any(host == domain or host.endswith("." + domain) for domain in self.domains)

    def allows_url(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme in ("https", "http") and self.allows_host(parts.hostname or "")


SITES = (
    Site("jushuitan", "聚水潭", "https://dopre.jushuitan.com/", ("jushuitan.com", "erp321.com")),
    Site("banniu", "班牛", "https://work.bytenew.com/", ("bytenew.com",)),
)
