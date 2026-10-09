"""Django AppConfig for CAPE-tenancy."""

try:
    from django.apps import AppConfig
except ImportError:

    class AppConfig:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass


class CapeTenancyConfig(AppConfig):
    name = "cape_tenancy"
    label = "cape_tenancy"
    verbose_name = "CAPE Multi-Tenancy & Central Mode"

    def ready(self) -> None:
        from cape_tenancy.hooks import default_mongo_analysis_filter, register_mongo_filter, wrap_mongodb_module

        register_mongo_filter(default_mongo_analysis_filter)
        try:
            import dev_utils.mongodb as mongodb_mod

            wrap_mongodb_module(mongodb_mod)
        except ImportError:
            pass
