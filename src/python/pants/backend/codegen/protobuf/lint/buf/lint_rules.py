# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
import os.path
from dataclasses import dataclass
from typing import Any

from pants.backend.codegen.protobuf.buf.skip_field import SkipBufLintField
from pants.backend.codegen.protobuf.buf.subsystem import BufSubsystem
from pants.backend.codegen.protobuf.target_types import (
    ProtobufDependenciesField,
    ProtobufSourceField,
)
from pants.core.goals.lint import LintResult, LintTargetsRequest, Partitions
from pants.core.goals.package import (
    EnvironmentAwarePackageRequest,
    PackageFieldSet,
    environment_aware_package,
)
from pants.core.util_rules.config_files import find_config_file
from pants.core.util_rules.external_tool import download_external_tool
from pants.core.util_rules.source_files import SourceFilesRequest
from pants.core.util_rules.stripped_source_files import strip_source_roots
from pants.engine.addresses import Addresses, UnparsedAddressInputs
from pants.engine.fs import AddPrefix, MergeDigests
from pants.engine.internals.graph import (
    find_valid_field_sets,
    resolve_targets,
    resolve_unparsed_address_inputs,
)
from pants.engine.internals.graph import transitive_targets as transitive_targets_get
from pants.engine.internals.native_engine import EMPTY_DIGEST, Digest
from pants.engine.intrinsics import add_prefix, execute_process, merge_digests
from pants.engine.platform import Platform
from pants.engine.process import Process
from pants.engine.rules import collect_rules, concurrently, implicitly, rule
from pants.engine.target import (
    FieldSet,
    FieldSetsPerTargetRequest,
    Target,
    TransitiveTargetsRequest,
)
from pants.util.logging import LogLevel
from pants.util.meta import classproperty
from pants.util.strutil import pluralize


@dataclass(frozen=True)
class BufFieldSet(FieldSet):
    required_fields = (ProtobufSourceField,)

    sources: ProtobufSourceField
    dependencies: ProtobufDependenciesField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipBufLintField).value


class BufLintRequest(LintTargetsRequest):
    field_set_type = BufFieldSet
    tool_subsystem = BufSubsystem  # type: ignore[assignment]

    @classproperty
    def tool_name(cls) -> str:
        return "buf lint"

    @classproperty
    def tool_id(cls) -> str:
        return "buf-lint"


@rule
async def partition_buf(
    request: BufLintRequest.PartitionRequest[BufFieldSet], buf: BufSubsystem
) -> Partitions[BufFieldSet, Any]:
    return Partitions() if buf.lint_skip else Partitions.single_partition(request.field_sets)


# Check plugin binaries are gathered here rather than left at their `output_path`, so that PATH
# names only them. Pointing PATH at the sandbox root instead would make the protos, the config and
# buf itself executable from it; see the `BinaryShims` docstring.
_PLUGIN_DIR = "_buf_check_plugins"


async def _build_check_plugins(buf: BufSubsystem) -> Digest:
    """Build the `[buf].plugins` targets into `_PLUGIN_DIR` so `buf lint` can exec them by name."""
    if not buf.plugins:
        return EMPTY_DIGEST

    addresses = await resolve_unparsed_address_inputs(
        UnparsedAddressInputs(
            buf.plugins,
            owning_address=None,
            description_of_origin=f"the `[{BufSubsystem.options_scope}].plugins` option",
        ),
        **implicitly(),
    )
    targets = await resolve_targets(**implicitly({addresses: Addresses}))
    field_sets_per_target = await find_valid_field_sets(
        FieldSetsPerTargetRequest(PackageFieldSet, targets), **implicitly()
    )
    for address, field_sets in zip(addresses, field_sets_per_target.collection):
        if not field_sets:
            raise ValueError(
                f"`[{BufSubsystem.options_scope}].plugins` names {address.spec}, which cannot be "
                "packaged. Name a target that produces a binary, such as `go_binary`."
            )

    packages = await concurrently(
        environment_aware_package(EnvironmentAwarePackageRequest(field_set))
        for field_set in field_sets_per_target.field_sets
    )
    merged = await merge_digests(MergeDigests(package.digest for package in packages))
    return await add_prefix(AddPrefix(merged, _PLUGIN_DIR))


@rule(desc="Lint with buf lint", level=LogLevel.DEBUG)
async def run_buf(
    request: BufLintRequest.Batch[BufFieldSet, Any],
    buf: BufSubsystem,
    platform: Platform,
) -> LintResult:
    transitive_targets = await transitive_targets_get(
        TransitiveTargetsRequest(field_set.address for field_set in request.elements),
        **implicitly(),
    )

    all_stripped_sources_request = strip_source_roots(
        **implicitly(
            SourceFilesRequest(
                tgt[ProtobufSourceField]
                for tgt in transitive_targets.closure
                if tgt.has_field(ProtobufSourceField)
            )
        )
    )
    target_stripped_sources_request = strip_source_roots(
        **implicitly(
            SourceFilesRequest(
                (field_set.sources for field_set in request.elements),
                for_sources_types=(ProtobufSourceField,),
                enable_codegen=True,
            )
        )
    )

    download_buf_get = download_external_tool(buf.get_request(platform))

    config_files_get = find_config_file(buf.config_request)

    (
        target_sources_stripped,
        all_sources_stripped,
        downloaded_buf,
        config_files,
    ) = await concurrently(
        target_stripped_sources_request,
        all_stripped_sources_request,
        download_buf_get,
        config_files_get,
    )

    plugins_digest = await _build_check_plugins(buf)

    input_digest = await merge_digests(
        MergeDigests(
            (
                target_sources_stripped.snapshot.digest,
                all_sources_stripped.snapshot.digest,
                downloaded_buf.digest,
                config_files.snapshot.digest,
                plugins_digest,
            )
        )
    )

    config_arg = ["--config", buf.config] if buf.config else []

    # Buf resolves a bare `plugins: - plugin: <name>` entry off PATH, and refuses to exec a binary
    # found via a relative PATH entry. `{chroot}` is replaced with the sandbox's absolute path at
    # execution time, as in `BinaryShims.path_component`.
    env = {"PATH": os.path.join("{chroot}", _PLUGIN_DIR)} if buf.plugins else {}

    process_result = await execute_process(
        Process(
            argv=[
                downloaded_buf.exe,
                "lint",
                *config_arg,
                *buf.lint_args,
                "--path",
                ",".join(target_sources_stripped.snapshot.files),
            ],
            input_digest=input_digest,
            env=env,
            description=f"Run buf lint on {pluralize(len(request.elements), 'file')}.",
            level=LogLevel.DEBUG,
        ),
        **implicitly(),
    )
    return LintResult.create(request, process_result)


def rules():
    return [
        *collect_rules(),
        *BufLintRequest.rules(),
    ]
