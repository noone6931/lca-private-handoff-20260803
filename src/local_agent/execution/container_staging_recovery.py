from __future__ import annotations

from .container_plan import ContainerExecutionPlan
from .container_staging_contracts import ContainerStagingContainerBinding
from .container_types import ContainerEngineIdentity


def build_staging_container_binding(
    plan: ContainerExecutionPlan,
) -> ContainerStagingContainerBinding:
    identity = plan.identity
    staging = plan.staging
    if staging is None:
        raise ValueError("staging binding requires staged workspace roots")
    return ContainerStagingContainerBinding(
        instance_name=plan.instance_name,
        prep_instance_name=f"{plan.instance_name}-prep",
        volume_names=tuple(root.volume_name for root in staging.roots),
        runtime_image=plan.runtime_image,
        executable=identity.executable,
        executable_sha256=identity.executable_identity.sha256,
        socket_path=identity.endpoint.socket_path,
        socket_identity=identity.endpoint.socket_identity,
        client_config_directory=identity.endpoint.client_config_directory,
        client_config_identity=identity.endpoint.client_config_identity,
        gate_image_reference=identity.gate_image.reference,
        gate_image_digest=identity.gate_image.digest,
    )


def staging_container_binding_matches(
    binding: ContainerStagingContainerBinding,
    identity: ContainerEngineIdentity,
) -> bool:
    return (
        identity.control_authority_is_current()
        and binding.prep_instance_name
        == f"{binding.instance_name}-prep"
        and binding.executable == identity.executable
        and binding.executable_sha256
        == identity.executable_identity.sha256
        and binding.socket_path == identity.endpoint.socket_path
        and binding.socket_identity == identity.endpoint.socket_identity
        and binding.client_config_directory
        == identity.endpoint.client_config_directory
        and binding.client_config_identity
        == identity.endpoint.client_config_identity
        and binding.gate_image_reference == identity.gate_image.reference
        and binding.gate_image_digest == identity.gate_image.digest
    )


__all__ = [
    "build_staging_container_binding",
    "staging_container_binding_matches",
]
