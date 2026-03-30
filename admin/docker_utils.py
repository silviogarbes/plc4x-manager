"""
Shared Docker utilities for PLC4X Manager.

Uses label-based container lookup to be compatible with Coolify and other
orchestrators that suffix container names (e.g., plc4x-server-abc123).
"""

import docker

_docker_client = None


def get_docker_client() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    return _docker_client


def get_container_by_service(client: docker.DockerClient, service_name: str):
    """Find a container by its compose service name.

    Works with Coolify and other orchestrators that rename containers by
    filtering on the 'com.docker.compose.service' label instead of container name.

    Returns the first matching container, or None if not found.
    """
    containers = client.containers.list(
        all=True,
        filters={"label": f"com.docker.compose.service={service_name}"}
    )
    if not containers:
        return None
    return containers[0]
