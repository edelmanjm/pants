# Copyright 2026 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from pants.backend.codegen.protobuf.buf.config import (
    LanguageGenTemplate,
    gen_template_request_for_target,
    resolved_template_path,
)
from pants.backend.codegen.protobuf.buf.subsystem import BufSubsystem
from pants.backend.codegen.protobuf.protoc import Protoc
from pants.backend.codegen.protobuf.target_types import ProtobufSourceField
from pants.backend.codegen.protobuf.typescript.subsystem import TypeScriptProtobufSubsystem
from pants.backend.javascript.install_node_package import (
    InstalledNodePackageRequest,
    add_sources_to_installed_node_package,
)
from pants.backend.javascript.subsystems.nodejs import NodeJSProcessEnvironment
from pants.backend.typescript.target_types import TypeScriptSourceField
from pants.core.util_rules.config_files import find_config_file
from pants.core.util_rules.external_tool import download_external_tool
from pants.core.util_rules.source_files import SourceFilesRequest, determine_source_files
from pants.engine.addresses import AddressInput
from pants.engine.fs import CreateDigest, Directory, MergeDigests, RemovePrefix
from pants.engine.internals.graph import (
    resolve_address,
    transitive_targets as transitive_targets_get,
)
from pants.engine.intrinsics import (
    create_digest,
    digest_to_snapshot,
    merge_digests,
    remove_prefix,
)
from pants.engine.platform import Platform
from pants.engine.process import Process, execute_process_or_raise
from pants.engine.rules import Rule, collect_rules, concurrently, implicitly, rule
from pants.engine.target import (
    GeneratedSources,
    GenerateSourcesRequest,
    TransitiveTargetsRequest,
)
from pants.engine.unions import UnionRule
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


class GenerateTypeScriptFromProtobufRequest(GenerateSourcesRequest):
    input = ProtobufSourceField
    output = TypeScriptSourceField


@rule(desc="Generate TypeScript from Protobuf via `buf generate`", level=LogLevel.DEBUG)
async def generate_typescript_from_protobuf(
    request: GenerateTypeScriptFromProtobufRequest,
    buf: BufSubsystem,
    typescript_protobuf: TypeScriptProtobufSubsystem,
    protoc: Protoc,
    platform: Platform,
    node_environment: NodeJSProcessEnvironment,
) -> GeneratedSources:
    language_template = LanguageGenTemplate.from_option(typescript_protobuf, "buf_gen_template")
    if language_template is None:
        raise ValueError(
            f"`[{typescript_protobuf.options_scope}].buf_gen_template` is unset, so there is no "
            "template to generate TypeScript with. Point it at a `buf.gen.yaml` whose plugins "
            "emit TypeScript."
        )
    if not typescript_protobuf.buf_node_package_address:
        raise ValueError(
            f"`[{typescript_protobuf.options_scope}].buf_node_package_address` is unset. Set it "
            "to the `package_json` target whose `node_modules` provides the `protoc-gen-*` plugin "
            f"binaries named by `{language_template.path}`."
        )

    target = request.protocol_target
    package_address = await resolve_address(
        **implicitly(
            {
                AddressInput.parse(
                    typescript_protobuf.buf_node_package_address,
                    description_of_origin="the `[typescript-protobuf].buf_node_package_address` option",
                ): AddressInput
            }
        )
    )
    output_dir = "_generated_files"

    # Buf needs all transitive `.proto` sources to resolve imports, even though only the
    # target's own files are passed via `--path`.
    transitive_targets = await transitive_targets_get(
        TransitiveTargetsRequest([target.address]), **implicitly()
    )

    (
        downloaded_buf,
        installed_package,
        all_sources,
        target_sources,
        config_files,
        gen_template_files,
        empty_output_dir,
    ) = await concurrently(
        download_external_tool(buf.get_request(platform)),
        # The plugin binaries (`protoc-gen-es`, and any repo-local plugin that shells out
        # to `tsx`) live in this package's `node_modules`, and a repo-local plugin also
        # needs the package's own sources, so take both. Codegen is off: the package
        # depends on the protobuf targets, and generating them would re-enter this rule.
        add_sources_to_installed_node_package(
            InstalledNodePackageRequest(package_address, enable_codegen=False)
        ),
        determine_source_files(
            SourceFilesRequest(
                tgt[ProtobufSourceField]
                for tgt in transitive_targets.closure
                if tgt.has_field(ProtobufSourceField)
            )
        ),
        determine_source_files(SourceFilesRequest([target[ProtobufSourceField]])),
        find_config_file(buf.config_request),
        # Same resolution chain as the Python rule: per-target `buf_gen_template` field ->
        # this language's option -> `[buf].gen_template` -> build-root discovery. Going through
        # `find_config_file` also means a missing template is reported against the option that
        # named it, rather than silently producing an empty digest.
        find_config_file(
            gen_template_request_for_target(target, buf, language_template)
        ),
        create_digest(CreateDigest([Directory(output_dir)])),
    )

    input_digest = await merge_digests(
        MergeDigests(
            (
                all_sources.snapshot.digest,
                installed_package.digest,
                config_files.snapshot.digest,
                gen_template_files.snapshot.digest,
                empty_output_dir,
                downloaded_buf.digest,
            )
        )
    )

    # Mirrors the Python buf path: with dependency inference on, each sandbox holds only
    # this target's imports, so `--path` scopes generation to its files; with inference
    # off, every sandbox holds the full tree and each invocation emits identical bytes
    # that `MergeDigests` dedupes.
    path_arg = (
        ["--path", ",".join(target_sources.snapshot.files)] if protoc.dependency_inference else []
    )
    config_arg = ["--config", buf.config] if buf.config else []

    # `node_modules/.bin` holds the plugin binaries; `{chroot}` is substituted with the
    # sandbox root so the entry survives buf's own resolution.
    package_bin_dir = os.path.join(
        "{chroot}", installed_package.project_env.root_dir, "node_modules", ".bin"
    )

    template_path = resolved_template_path(target, buf, language_template)
    template_arg = ["--template", template_path] if template_path else []

    result = await execute_process_or_raise(
        **implicitly(
            Process(
                argv=[
                    downloaded_buf.exe,
                    "generate",
                    *config_arg,
                    *template_arg,
                    "--output",
                    output_dir,
                    *buf.gen_args,
                    *path_arg,
                ],
                input_digest=input_digest,
                immutable_input_digests=node_environment.immutable_digest(),
                append_only_caches=node_environment.append_only_caches,
                env=node_environment.to_env_dict({"PATH": package_bin_dir}),
                description=f"Generating TypeScript from Protobuf via buf for {target.address}.",
                level=LogLevel.DEBUG,
                output_directories=(output_dir,),
            )
        ),
    )

    # Strip the sandbox `output_dir` prefix; the template's `out:` paths land at exactly
    # the locations the user declared.
    normalized = await remove_prefix(RemovePrefix(result.output_digest, output_dir))
    snapshot = await digest_to_snapshot(normalized)
    return GeneratedSources(snapshot)


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *collect_rules(),
        UnionRule(GenerateSourcesRequest, GenerateTypeScriptFromProtobufRequest),
    )
