"""Central Mode and Storage Backend components for CAPE-tenancy."""

from cape_tenancy.central.storage_backend import (
    ArtifactNotFound,
    ArtifactStore,
    LocalFSStore,
    S3Store,
    get_artifact_store,
)

StorageBackend = ArtifactStore
LocalStorageBackend = LocalFSStore
S3StorageBackend = S3Store

__all__ = [
    "ArtifactNotFound",
    "ArtifactStore",
    "LocalFSStore",
    "S3Store",
    "get_artifact_store",
    "StorageBackend",
    "LocalStorageBackend",
    "S3StorageBackend",
]
