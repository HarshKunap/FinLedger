class ProviderTransientError(Exception):
    """Retryable provider failure; maps to extraction category provider_transient."""

    def __init__(self, category: str = "provider_transient"):
        self.category = category
        super().__init__(category)
