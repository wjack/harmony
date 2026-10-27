"""The Temporal client, configured from settings, with the Pydantic data converter.

- Locally, `TEMPORAL_ADDRESS` is the Compose dev server (`localhost:7233`, namespace
  `default`), plaintext and unauthenticated.
- On Temporal Cloud, `TEMPORAL_ADDRESS` is Terraform's `endpoints.grpc_address` (for
  an API-key namespace, the regional endpoint `<region>.<cloud>.api.temporal.io:7233`,
  or the namespace endpoint `<namespace>.<account>.tmprl.cloud:7233`),
  `TEMPORAL_NAMESPACE` is `<namespace>.<account>`, and `TEMPORAL_API_KEY` is set: the
  key switches on TLS and bearer-token auth, and every call also carries the
  `temporal-namespace` header the regional endpoint routes API-key requests by.
"""

from __future__ import annotations

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from harmony.config import Settings, get_settings


async def connect_client(
    settings: Settings | None = None,
    *,
    lazy: bool = False,
    identity: str | None = None,
) -> Client:
    """Connect to Temporal.

    `lazy=True` defers the connection to the first call, so the API can build its
    client at startup even while Temporal is down. A worker needs an eager client.
    """
    settings = settings or get_settings()
    api_key = settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        api_key=api_key,
        tls=api_key is not None,
        # sdk-core already adds this header to namespaced calls; setting it here also
        # covers the connect-time calls that carry no namespace (GetSystemInfo).
        rpc_metadata={"temporal-namespace": settings.temporal_namespace} if api_key else {},
        data_converter=pydantic_data_converter,
        identity=identity,
        lazy=lazy,
    )
